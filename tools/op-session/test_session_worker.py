"""Exercise the real credential worker in isolated PTYs with a fake op binary."""

import errno
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.temporary = Path(self.directory.name)
        self.socket = f"agent-worker-test-{os.getpid()}-{time.monotonic_ns()}"
        self.addCleanup(self.stop_server)
        self.bin = self.temporary / "bin"
        self.bin.mkdir()
        self.log = self.temporary / "op-calls.jsonl"
        self.fake_op = self.bin / "op"
        self.fake_op.write_text("""#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
tty = os.ttyname(0) if os.isatty(0) else None
args = sys.argv[1:]
root = Path(os.environ["TEST_ROOT"])
with (root/"op-calls.jsonl").open("a") as f:
    f.write(json.dumps({"args":args,"tty":tty,
                        "app":os.environ.get("OP_BIOMETRIC_UNLOCK_ENABLED","true")})+"\\n")
cli_only = os.environ.get("OP_BIOMETRIC_UNLOCK_ENABLED") == "false"
if args[:2] == ["account","list"] and cli_only and not (root/"cli-account").exists():
    print("[]")
elif args[:2] == ["account","add"]:
    (root/"cli-account").touch()
elif args[:2] == ["account","list"]:
    if not cli_only and os.environ.get("TEST_APP_MARKER") and not Path(os.environ["TEST_APP_MARKER"]).exists():
        print("cannot connect to 1Password app", file=sys.stderr)
        sys.exit(1)
    print('[{"url":"example.1password.test"}]')
elif args[:1] == ["signin"]:
    time.sleep(float(os.environ.get("TEST_SIGNIN_DELAY","0")))
    (root/"authorized-tty").write_text(tty or "")
elif args[:1] == ["whoami"]:
    if not tty or tty != (root/"authorized-tty").read_text() or (root/"revoked").exists():
        sys.exit(77)
elif args[:2] == ["vault","list"]:
    if not tty or tty != (root/"authorized-tty").read_text():
        sys.exit(77)
    print(json.dumps({"tty":tty,"vaults":[]}))
else:
    sys.exit(64)
""")
        self.fake_op.chmod(0o755)
        sleep = self.bin / "sleep"
        sleep.write_text("""#!/usr/bin/env python3
import sys,time
seconds=float(sys.argv[1])
time.sleep(0.2 if seconds==45 else seconds)
""")
        sleep.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        TERM="xterm-256color", TEST_ROOT=str(self.temporary),
                        AGENT_SESSION_STATE_HOME=str(self.temporary / "state"),
                        AGENT_SESSION_TMUX_SOCKET=self.socket)
        self.worker = Path(os.environ.get("TEST_SESSION_WORKER", ROOT / "session-worker"))
        self.helper = Path(os.environ.get("TEST_SESSION_HELPER", ROOT / "op-session"))
        self.env["AGENT_SESSION_WORKER"] = str(self.worker)
        self.started_pid = None
        self.terminal = None
        self.addCleanup(self.close_terminal)

    def stop_server(self):
        subprocess.run(["tmux", "-L", self.socket, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close_terminal(self):
        if self.started_pid is not None:
            try:
                pid, _ = os.waitpid(self.started_pid, os.WNOHANG)
                if not pid:
                    os.kill(self.started_pid, signal.SIGTERM)
                    os.waitpid(self.started_pid, 0)
            except ProcessLookupError:
                pass
            self.started_pid = None
        if self.terminal is not None:
            os.close(self.terminal)
            self.terminal = None

    def begin_start(self, *arguments):
        pid, terminal = pty.fork()
        if pid == 0:
            os.execve(str(self.helper), [str(self.helper), "start", *arguments], self.env)
        self.started_pid = pid
        self.terminal = terminal

    def finish_start(self):
        output = bytearray()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not select.select([self.terminal], [], [], 0.1)[0]:
                continue
            try:
                data = os.read(self.terminal, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            output.extend(data)
        else:
            self.fail("Interactive worker startup timed out")
        _, status = os.waitpid(self.started_pid, 0)
        self.started_pid = None
        self.assertEqual(os.waitstatus_to_exitcode(status), 0,
                         output.decode(errors="replace"))

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(message)

    def run_helper(self, *arguments):
        return subprocess.run([str(self.helper), *arguments], env=self.env,
                              text=True, capture_output=True, timeout=15)

    def test_keepalive_reuses_authorized_tty_and_grant_across_fresh_shells(self):
        self.begin_start()
        self.finish_start()
        expiry = self.temporary / "state/op/expires"
        original_expiry = expiry.read_text()
        self.wait_for(lambda: sum(c["args"] == ["whoami"] for c in self.calls()) >= 2,
                      "Keepalive did not execute")
        for _ in range(2):
            result = self.run_helper("vault", "list")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        result = self.run_helper("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(expiry.read_text(), original_expiry)
        # The helper first probes desktop-app reachability from its own terminal.
        calls = self.calls()[1:]
        self.assertEqual(self.calls()[0]["args"][:2], ["account", "list"])
        self.assertEqual(sum(c["args"][:1] == ["signin"] for c in calls), 1)
        self.assertEqual(len({c["tty"] for c in calls}), 1, calls)
        self.assertIsNotNone(calls[0]["tty"])

    def test_status_does_not_cancel_pending_authorization(self):
        self.env["TEST_SIGNIN_DELAY"] = "1"
        self.begin_start()
        self.wait_for(lambda: any(c["args"][:1] == ["signin"] for c in self.calls()),
                      "Sign-in did not start")
        status = self.run_helper("status")
        self.assertEqual(status.returncode, 2, status.stdout + status.stderr)
        self.assertIn("in progress", status.stdout)
        self.finish_start()
        self.assertEqual(self.run_helper("vault", "list").returncode, 0)

    def test_revocation_is_reported_without_starting_another_signin(self):
        self.begin_start()
        self.finish_start()
        (self.temporary / "revoked").touch()
        self.wait_for(lambda: self.run_helper("status").returncode == 1,
                      "Revoked grant was not stopped")
        result = self.run_helper("status")
        self.assertIn("keepalive", result.stdout)
        self.assertNotIn("authenticated\n", result.stdout)
        self.assertEqual(sum(c["args"][:1] == ["signin"] for c in self.calls()), 1)

    def test_expiry_stops_the_worker(self):
        short_worker = self.temporary / "short-worker"
        short_worker.write_text("#!/bin/bash\nexec " + shlex.quote(str(self.worker)) +
                                ' "$1" "$2" "$3" 2 "$5"\n')
        short_worker.chmod(0o755)
        self.env["AGENT_SESSION_WORKER"] = str(short_worker)
        self.begin_start()
        self.finish_start()
        self.wait_for(lambda: self.run_helper("status").returncode == 1,
                      "Expired grant remained usable")
        self.assertNotEqual(self.run_helper("vault", "list").returncode, 0)

    def test_symlink_install_and_legacy_exec_preserve_arguments_cwd_and_status(self):
        installed = self.bin / "op-session"
        installed.symlink_to(self.helper)
        self.helper = installed
        self.begin_start()
        self.finish_start()
        result = self.run_helper(
            "exec", "bash", "-c", 'printf "%s\\n" "$PWD" "$1"; exit 7',
            "test", "$(not a shell command)",
        )
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            str(Path.cwd().resolve()), "$(not a shell command)",
        ])
        self.assertEqual(self.run_helper("run", "--", "vault", "list").returncode, 0)

    def test_extend_and_start_duration_lengthen_but_never_shorten(self):
        self.begin_start()
        self.finish_start()
        expiry = self.temporary / "state/op/expires"
        original = int(expiry.read_text())
        result = self.run_helper("extend", "-d", "1d")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extended = int(expiry.read_text())
        self.assertGreaterEqual(extended - original, 86400 - 3600 - 5)
        self.assertEqual(self.run_helper("start", "--duration", "30m").returncode, 0)
        self.assertEqual(int(expiry.read_text()), extended)
        self.assertEqual(self.run_helper("start").returncode, 0)
        self.assertEqual(int(expiry.read_text()), extended)
        self.assertEqual(self.run_helper("start", "-d", "2d").returncode, 0)
        self.assertGreater(int(expiry.read_text()), extended)
        self.wait_for(lambda: sum(c["args"] == ["whoami"] for c in self.calls()) >= 2,
                      "Keepalive stopped after extension")
        self.assertEqual(self.run_helper("status").returncode, 0)
        self.assertEqual(sum(c["args"][:1] == ["signin"] for c in self.calls()), 1)
        for bad in ("0", "1.5h", "forever"):
            self.assertEqual(self.run_helper("extend", "-d", bad).returncode, 64)

    def test_extend_without_session_falls_back_to_start(self):
        result = self.run_helper("extend", "-d", "12h")
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        self.assertIn("starting one", result.stderr)
        self.assertIn("start --terminal --duration 43200s", result.stderr)

    def test_start_launches_unreachable_desktop_app(self):
        marker = self.temporary / "app-running"
        self.env["TEST_APP_MARKER"] = str(marker)
        self.env["AGENT_SESSION_OP_APP"] = f"touch {marker}"
        self.begin_start("-d", "90m")
        self.finish_start()
        self.assertTrue(marker.exists())
        self.assertEqual(self.run_helper("vault", "list").returncode, 0)

    def test_ssh_start_uses_cli_password_signin_and_adds_account_once(self):
        self.env["SSH_CONNECTION"] = "192.0.2.1 50000 192.0.2.2 22"
        self.env["TEST_APP_MARKER"] = str(self.temporary / "never")
        self.begin_start()
        self.finish_start()
        self.assertEqual(self.run_helper("vault", "list").returncode, 0)
        worker_calls = [c for c in self.calls() if c["app"] == "false"]
        self.assertEqual(sum(c["args"][:2] == ["account", "add"] for c in worker_calls), 1)
        self.assertEqual(sum(c["args"][:1] == ["signin"] for c in worker_calls), 1)
        self.assertIn(["vault", "list"], [c["args"] for c in worker_calls])
        self.assertFalse((self.temporary / "never").exists())

    def test_noninteractive_start_explains_terminal_flow(self):
        result = self.run_helper("start")
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
        self.assertIn("start --terminal", result.stderr)
        self.assertFalse(any(c["args"][:1] == ["signin"] for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
