import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import sudo_session as session


REAL_POPEN = subprocess.Popen


class SessionTests(unittest.TestCase):
    @contextlib.contextmanager
    def broker(self, duration=60):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            runtime = Path(directory).resolve() / "runtime"
            errors = []

            def run_sudo(args, **kwargs):
                self.assertEqual(args[0], session.SUDO)
                self.assertIn(args[1:], (["-v"], ["-n", "-v"], ["-k"]))
                return subprocess.CompletedProcess(args, 0)

            def launch(args, **kwargs):
                self.assertEqual(args[:3], [session.SUDO, "-n", "--"])
                return REAL_POPEN(args[3:], **kwargs)

            def worker():
                try:
                    session.serve()
                except Exception as error:
                    errors.append(error)

            with (patch.object(session, "RUNTIME", runtime),
                  patch.object(session, "ADDRESS", runtime / "control.sock"),
                  patch.object(session, "DURATION", duration),
                  patch.object(session, "audit"),
                  patch.object(session, "remove_legacy_grant"),
                  patch.object(session.subprocess, "run", side_effect=run_sudo),
                  patch.object(session.subprocess, "Popen", side_effect=launch),
                  contextlib.redirect_stdout(io.StringIO())):
                thread = threading.Thread(target=worker)
                thread.start()
                try:
                    deadline = time.monotonic() + 3
                    while not errors:
                        if time.monotonic() >= deadline:
                            self.fail("Broker did not start")
                        try:
                            if session.request({"action": "status"})["code"] == 0:
                                break
                        except (FileNotFoundError, ConnectionRefusedError):
                            pass
                        time.sleep(0.01)
                    if errors:
                        raise errors[0]
                    yield runtime
                finally:
                    if session.ADDRESS.exists():
                        try:
                            session.request({"action": "stop"})
                        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
                            pass
                    thread.join(3)
                    self.assertFalse(thread.is_alive(), "Broker failed to stop")
                    if errors:
                        raise errors[0]

    def test_streams_arguments_working_directory_and_exit_status(self):
        with self.broker() as runtime:
            with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                stdin.write(b"private input")
                stdin.seek(0)
                code = (
                    "import os,sys; print(os.getcwd()); print(sys.argv[1]); "
                    "print(sys.stdin.read()); print('error output', file=sys.stderr); sys.exit(7)"
                )
                reply = session.request({
                    "action": "exec",
                    "args": [sys.executable, "-c", code, "$(not a shell command)"],
                    "cwd": str(runtime),
                }, [stdin.fileno(), stdout.fileno(), stderr.fileno()])
                self.assertEqual(reply["code"], 7)
                stdout.seek(0)
                stderr.seek(0)
                self.assertEqual(stdout.read().decode().splitlines(), [
                    str(runtime), "$(not a shell command)", "private input",
                ])
                self.assertEqual(stderr.read(), b"error output\n")
                self.assertEqual(session.request({"action": "status"})["code"], 0)

    def test_invalid_request_and_launch_error_do_not_stop_session(self):
        with self.broker():
            reply = session.request({"action": "exec", "args": "not an argument array"})
            self.assertEqual(reply["code"], 1)
            self.assertIn("Invalid command", reply["error"])
            with open(os.devnull, "r+b") as stream:
                reply = session.request({
                    "action": "exec", "args": ["true"], "cwd": "/nonexistent-sudo-session-test",
                }, [stream.fileno()] * 3)
            self.assertEqual(reply["code"], 1)
            self.assertIn("No such file", reply["error"])
            self.assertEqual(session.request({"action": "status"})["code"], 0)

    def test_expiry_closes_socket_and_invalidates_credential(self):
        with self.broker(duration=0.2):
            deadline = time.monotonic() + 3
            while session.ADDRESS.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(session.ADDRESS.exists())
            self.assertIn(
                unittest.mock.call([session.SUDO, "-k"], check=True),
                session.subprocess.run.call_args_list,
            )
            with self.assertRaises(FileNotFoundError):
                session.request({"action": "status"})

    def test_extension_sets_twelve_hours_remaining_and_preserves_longer_deadline(self):
        with self.broker(duration=0.2):
            reply = session.request({"action": "extend", "duration": session.parse_duration("12h")})
            self.assertEqual(reply["code"], 0)
            self.assertGreaterEqual(reply["remaining"], 43199)
            self.assertLessEqual(reply["remaining"], 43200)
            time.sleep(0.3)
            reply = session.request({"action": "extend", "duration": 60})
            self.assertEqual(reply["code"], 0)
            self.assertGreaterEqual(reply["remaining"], 43199)
            self.assertGreaterEqual(session.request({"action": "status"})["remaining"], 43199)
            self.assertTrue(any(
                "Extended authenticated terminal session" in call.args[0]
                for call in session.audit.call_args_list
            ))

    def test_extension_rejects_invalid_duration_and_revoked_credential(self):
        with self.broker():
            for duration in (0, -1, True, "12h", 12.5, sys.maxsize + 1):
                with self.subTest(duration=duration):
                    reply = session.request({"action": "extend", "duration": duration})
                    self.assertEqual(reply["code"], 1)
                    self.assertIn("Invalid extension duration", reply["error"])
            with patch.object(
                session.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1),
            ):
                reply = session.request({"action": "extend", "duration": 43200})
            self.assertEqual(reply["code"], 1)
            self.assertIn("no longer valid", reply["error"])

    def test_duration_units_validation_and_startup_propagation(self):
        for text, seconds in (("90s", 90), ("30m", 1800), ("12h", 43200), ("1d", 86400), ("60", 60)):
            with self.subTest(text=text):
                self.assertEqual(session.parse_duration(text), seconds)
        for text in ("0", "0h", "-1h", "1.5h", "12hours", "1h30m", "forever"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                session.parse_duration(text)
        with (patch.object(sys, "argv", ["sudo-session", "start", "--ssh", "--duration", "12h"]),
              patch.object(session, "start", return_value=0) as start):
            self.assertEqual(session.main(), 0)
            start.assert_called_once_with(43200)
        with (patch.object(sys, "argv", ["sudo-session", "start", "-d", "24h"]),
              patch.object(session, "start", return_value=0) as start):
            self.assertEqual(session.main(), 0)
            start.assert_called_once_with(86400)
        with (patch.object(session.sys.stdin, "isatty", return_value=True),
              patch.object(session.sys.stdout, "isatty", return_value=True),
              patch.object(session, "prepare_runtime"),
              patch.object(session, "request", side_effect=FileNotFoundError),
              patch.object(session.subprocess, "call", return_value=0) as launch):
            self.assertEqual(session.start(43200), 0)
            self.assertEqual(
                session.shlex.split(launch.call_args.args[0][-1]),
                [sys.executable, str(session.ROOT), "_serve", "--duration", "43200s"],
            )

    def test_extend_cli_handles_stopped_and_old_sessions_explicitly(self):
        with patch.object(sys, "argv", ["sudo-session", "extend", "--duration", "12h"]):
            with patch.object(session, "request", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(RuntimeError, "No active sudo session to extend"):
                    session.main()
            with patch.object(session, "request", return_value={"code": 1, "error": "Unknown session action"}):
                with self.assertRaisesRegex(RuntimeError, "predates extension support"):
                    session.main()

    def test_terminal_start_preserves_pending_approval_and_waits_for_broker(self):
        with (patch.object(session, "pending", return_value=True),
              patch.object(session, "request", side_effect=[
                  FileNotFoundError(), FileNotFoundError(), {"code": 0, "remaining": 3600},
              ]),
              patch.object(session.time, "sleep"),
              patch.object(session.subprocess, "Popen") as launch):
            self.assertEqual(session.start_in_terminal(3600), 0)
            launch.assert_not_called()
    def test_macos_terminal_launch_and_pending_status(self):
        with (patch.object(session.sys, "platform", "darwin"),
              patch.dict(os.environ, {"AGENT_SESSION_TERMINAL_WAIT": "0"}, clear=True),
              patch.object(session, "pending", return_value=False),
              patch.object(session, "request", side_effect=FileNotFoundError),
              patch.object(session.subprocess, "run") as launch):
            self.assertEqual(session.start_in_terminal(43200), 2)
            command = launch.call_args.args[0]
            self.assertEqual(command[0], "osascript")
            self.assertEqual(
                session.shlex.split(command[-1]),
                ["env", sys.executable, str(session.ROOT), "start", "--duration", "43200s"],
            )
        with (patch.object(sys, "argv", ["sudo-session", "status"]),
              patch.object(session, "request", side_effect=FileNotFoundError),
              patch.object(session, "pending", return_value=True)):
            self.assertEqual(session.main(), 2)

    def test_macos_does_not_invoke_linux_legacy_grant_cleanup(self):
        with (patch.object(session.sys, "platform", "darwin"),
              patch.object(session.subprocess, "run") as run):
            session.remove_legacy_grant()
            run.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "legacy packet sockets are Linux-only")
    def test_running_linux_packet_session_remains_usable_after_upgrade(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            address = Path(directory) / "control.sock"
            with session.socket.socket(session.socket.AF_UNIX, session.socket.SOCK_SEQPACKET) as server:
                server.bind(str(address))
                server.listen(1)
                requests = []

                def legacy():
                    connection, _ = server.accept()
                    with connection:
                        requests.append(session.json.loads(connection.recv(65536)))
                        connection.send(session.json.dumps({"code": 0, "remaining": 60}).encode())

                thread = threading.Thread(target=legacy, daemon=True)
                thread.start()
                with patch.object(session, "ADDRESS", address):
                    self.assertEqual(session.request({"action": "status"}), {"code": 0, "remaining": 60})
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(requests, [{"action": "status"}])

    def test_stop_remains_responsive_during_command_and_allows_it_to_finish(self):
        with self.broker():
            replies = []
            with open(os.devnull, "r+b") as stream:
                def run():
                    replies.append(session.request({
                        "action": "exec", "args": [sys.executable, "-c", "import time; time.sleep(0.5)"],
                        "cwd": os.getcwd(),
                    }, [stream.fileno()] * 3))

                client = threading.Thread(target=run)
                client.start()
                deadline = time.monotonic() + 3
                while not session.subprocess.Popen.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(session.subprocess.Popen.called)
                self.assertEqual(session.request({"action": "status"})["code"], 0)
                self.assertEqual(session.request({"action": "stop"})["code"], 0)
                self.assertTrue(client.is_alive())
                client.join(3)
                self.assertFalse(client.is_alive())
                self.assertEqual(replies, [{"code": 0}])

    def test_legacy_grant_is_removed_before_fresh_authentication(self):
        with (patch.object(session.sys, "platform", "linux"),
              patch.object(session, "audit"),
              patch.object(session.subprocess, "run") as run):
            run.return_value = subprocess.CompletedProcess([], 0)
            session.remove_legacy_grant()
            self.assertEqual(run.call_args_list, [
                unittest.mock.call([
                    "systemctl", "is-active", "--quiet",
                    f"sudo-session-expire-{os.getuid()}.timer",
                ]),
                unittest.mock.call([
                    session.SUDO, "-n", "--", "/usr/bin/rm", "-f",
                    f"/etc/sudoers.d/90-sudo-session-{os.getuid()}",
                ], check=True),
                unittest.mock.call([session.SUDO, "-k"], check=True),
                unittest.mock.call([session.SUDO, "-v"]),
            ])


if __name__ == "__main__":
    unittest.main()
