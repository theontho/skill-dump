"""Remote implementers: Windows command construction, validation, state and
stop safety, with the SSH transport mocked; POSIX launch kept unchanged.
No test can reach SSH or a real kill: transport and kill_tree are replaced."""

import base64
import io
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

import remote
import remote_agents
import remote_agents_windows as win
from runtime import Project

OUT = Path(__file__).resolve().parents[2] / "out"
CONFIG = r"""
[github]
repo = "example/project"
owner = "owner"
[remote_agents]
command = ["claude", "--model", "m"]
report = ["report.exe", "--project", "p"]
[hosts.win]
ssh = "win-alias"
windows = true
remote_agents = true
checkout = 'D:\src\project'
[hosts.winenv]
ssh = "winenv-alias"
windows = true
remote_agents = true
checkout = 'D:\src\project'
agent_root = '~\agents'
agent_env_session = "coordenv"
agent_command = ["C:\\Tools\\copilot.cmd", "--prompt-file", "{prompt_file}"]
[hosts.posix]
ssh = "posix-alias"
remote_agents = true
checkout = "~/src/project"
"""
CREATED = 133800000000000000
REAL_TRANSPORT = win.transport


def ok(value):
    return "PRCOORD-OK " + base64.b64encode(json.dumps(value).encode()).decode() + "\n"


def error(message):
    return "PRCOORD-ERROR " + base64.b64encode(message.encode()).decode() + "\n"


def running(name="a1", **extra):
    rec = {
        "name": name,
        "pid": 4242,
        "created": CREATED,
        "live_created": CREATED,
        "cmdline_match": True,
        "owner_match": True,
        "rc": None,
        "stopped": False,
        "failed": None,
        "started": "2026-01-01T00:00:00Z",
    }
    rec.update(extra)
    return rec


class FakeTransport:
    """Stands in for the SSH transport: decodes each header, records the
    script and its arguments, and answers by script."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, host, line, *, passthrough=False, timeout=60):
        head = json.loads(base64.b64decode(line))
        if head["script"] == win.KILL:
            raise AssertionError("a remote kill was attempted outside kill_tree")
        self.calls.append((host, head["script"], head["args"], passthrough))
        out = self.replies.get(head["script"], ok({}))
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess([], 0, out.encode(), b"")

    def args(self, script):
        return [a for _, s, a, _ in self.calls if s == script]


class WindowsAgents(unittest.TestCase):
    def setUp(self):
        self.root = OUT / f"pr-coord-agents-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        config = self.root / "project.toml"
        config.write_text(CONFIG)
        self.project = Project("agents", config_file=config, data_dir=self.root)
        self.records = []
        for target in (win, remote_agents):
            for attr, value in (
                ("P", self.project),
                ("record", lambda *a: self.records.append(a)),
            ):
                patcher = mock.patch.object(target, attr, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        # Nothing in this class may reach the real SSH runners.
        for target, attr in ((win, "transport"), (remote_agents, "remote")):
            patcher = mock.patch.object(
                target, attr, side_effect=AssertionError("unmocked SSH call")
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text(
            '\ufeffFix "it" & $(rm) %PATH% — café\n', encoding="utf-8"
        )

    def run_main(self, command, *argv, transport=None, kill=None, stdin=None):
        transport = transport or FakeTransport()
        kills = []
        patches = [
            mock.patch.object(win, "transport", transport),
            mock.patch.object(
                win,
                "kill_tree",
                kill or (lambda *a: kills.append(a) or {"killed": a[4]}),
            ),
            mock.patch.object(sys, "argv", [f"pr-coord {command}", *argv]),
        ]
        if stdin is not None:
            patches.append(mock.patch.object(sys, "stdin", stdin))
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            for p in patches:
                p.start()
            try:
                code = remote_agents.main(command)
            finally:
                for p in reversed(patches):
                    p.stop()
        return code, out.getvalue(), transport, kills

    # Names and paths

    def test_names_are_restricted_to_safe_characters(self):
        for good in ("a1", "issue-12", "A_b-3", "x" * 64):
            self.assertTrue(win.valid_name(good), good)
        for bad in (
            "",
            "-a",
            ".a",
            "a.b",
            "a b",
            "a;b",
            "a'b",
            'a"b',
            "a$b",
            "..",
            "a/b",
            "a\\b",
            "CON",
            "nul",
            "com1",
            "x" * 65,
            "a\nb",
            "a1\n",
            "a\u2019b",
        ):
            self.assertFalse(win.valid_name(bad), bad)

    def test_bad_names_never_reach_the_host(self):
        for bad in ("a;b", "CON", "x'y", "..\\x", "a\u2019b"):
            transport = FakeTransport()
            with self.assertRaises(SystemExit):
                self.run_main(
                    "remote-agent", "win", bad, str(self.prompt), transport=transport
                )
            self.assertEqual(transport.calls, [])

    def test_roots_and_checkouts_are_conservative_ascii_paths(self):
        self.assertEqual(win.agent_root("win"), "D:\\src\\project\\out\\agents")
        self.assertEqual(win.agent_root("winenv"), "~\\agents")
        self.project.hosts["win"]["agent_root"] = "D:\\dev\\scratch\\a b-1_x.y"
        self.assertEqual(win.agent_root("win"), "D:\\dev\\scratch\\a b-1_x.y")
        for bad in (
            "D:\\a\u2019; Stop-Computer; #",  # the reviewer's example
            "D:\\a\u2018b",
            "D:\\a'b",
            "D:\\a;b",
            "D:\\a#b",
            "D:\\a$b",
            "D:\\caf\u00e9",
            "agents",
            "D:\\a\\..\\b",
            "D:\\a&b",
            'D:\\a"b',
            "D:\\%x%",
            "\\\\server\\share",
            "D:\\a\nb",
        ):
            self.project.hosts["win"]["agent_root"] = bad
            with self.assertRaises(win.WindowsAgentError, msg=bad):
                win.agent_root("win")
        del self.project.hosts["win"]["agent_root"]
        self.project.hosts["win"]["checkout"] = "D:\\src\u2019; Stop-Computer; #"
        with self.assertRaises(win.WindowsAgentError):
            win.agent_root("win")

    def test_values_travel_as_data_not_script_text(self):
        transport = FakeTransport({win.START: ok({"pid": 1})})
        self.project.hosts["win"]["agent_root"] = "D:\\dev\\agents"
        self.run_main(
            "remote-agent", "win", "a1", str(self.prompt), transport=transport
        )
        ((_, script, args, _),) = transport.calls
        self.assertEqual(script, win.START)  # the fixed script, nothing spliced in
        self.assertEqual(args["root"], "D:\\dev\\agents")
        self.assertNotIn("D:\\dev\\agents", script)
        # The SSH command line is the fixed bootstrap only.
        self.assertLess(len(win.BOOT_ENCODED), 3000)
        for script in (
            win.START,
            win.LIST,
            win.TAIL,
            win.KILL,
            win.ENV,
            win.ENV_PREPARE,
            win.HOLDER_FACTS,
            win.RUN,
            win.HOLDER,
        ):
            self.assertNotIn("Invoke-Expression", script)
            self.assertNotIn("iex ", script)
            # Host-side patterns anchor with \z, never $ (which allows a final newline).
            for pattern in ("[A-Za-z0-9_-]{0,63}$'", "+$'"):
                self.assertNotIn(pattern, script)

    def test_remote_ps_quoting_doubles_unicode_single_quotes(self):
        seen = []

        class Win:
            hosts = {"w": {"windows": True}}
            checkout = "/tmp"

            def ssh(self, key):
                return ["ssh", "w"]

        def fake_run(argv, **kwargs):
            seen.append(base64.b64decode(argv[-1]).decode("utf-16-le"))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(remote, "P", Win()), mock.patch.object(
            remote, "run", fake_run
        ):
            remote.remote("w", ["tool", "x\u2019; Stop-Computer; #", "a\u2018b'c"])
        self.assertIn("'x\u2019\u2019; Stop-Computer; #'", seen[0])
        self.assertIn("'a\u2018\u2018b''c'", seen[0])

    # Commands

    def test_agent_and_reporter_argv_reject_cmd_metacharacters(self):
        self.assertEqual(win.agent_argv("win", "a1"), ["claude", "--model", "m", "-p"])
        self.assertEqual(
            win.agent_argv("winenv", "a1"),
            ["C:\\Tools\\copilot.cmd", "--prompt-file", "{prompt_file}"],
        )
        for bad in ("a&b", "50%", "x|y", 'q"', "a>b", "!x", "^x"):
            self.project.settings["remote_agents"]["command"] = ["claude", bad]
            with self.assertRaises(win.WindowsAgentError, msg=bad):
                win.agent_argv("win", "a1")
            self.project.settings["remote_agents"]["report"] = ["report.cmd", bad]
            with self.assertRaises(win.WindowsAgentError, msg=bad):
                win.reporter_argv("win", "a1")

    def test_start_sends_prompt_only_as_a_file_without_bom(self):
        transport = FakeTransport({win.START: ok({"pid": 77, "created": 5})})
        code, out, transport, _ = self.run_main(
            "remote-agent", "win", "a1", str(self.prompt), transport=transport
        )
        self.assertEqual(code, 0)
        self.assertIn("pid 77", out)
        ((host, script, args, passthrough),) = transport.calls
        self.assertEqual((host, script, passthrough), ("win", win.START, False))
        self.assertEqual(
            {k: args[k] for k in ("root", "name", "session")},
            {"root": "D:\\src\\project\\out\\agents", "name": "a1", "session": ""},
        )
        files = args["files"]
        self.assertEqual(files["prompt.md"], 'Fix "it" & $(rm) %PATH% — café\n')
        self.assertEqual(files["run.ps1"], win.RUN)
        launch = json.loads(files["launch.json"])
        self.assertEqual(launch["cwd"], "D:\\src\\project")
        self.assertEqual(launch["command"], ["claude", "--model", "m", "-p"])
        self.assertEqual(
            launch["working"][:5], ["report.exe", "--project", "p", "agent", "a1"]
        )
        self.assertEqual(
            launch["done"],
            ["report.exe", "--project", "p", "agent", "a1", "done", "--task"],
        )
        self.assertEqual(self.records[0][:3], ("agent", "a1", "starting"))

    def test_runbook_name_may_not_replace_launch_or_state_files(self):
        for name in (
            "run.ps1",
            "launch.json",
            "exit.code",
            "pid.json",
            "run.lock",
            "output.1.log",
        ):
            runbook = self.root / name
            runbook.write_text("x")
            self.project.settings["remote_agents"]["runbook"] = str(runbook)
            transport = FakeTransport()
            with self.assertRaises(SystemExit):
                self.run_main(
                    "remote-agent", "win", "a1", str(self.prompt), transport=transport
                )
            self.assertEqual(transport.calls, [])

    def test_failed_launch_is_recorded_and_host_errors_are_shown(self):
        transport = FakeTransport({win.START: error("agent is already running")})
        with self.assertRaises(SystemExit):
            self.run_main(
                "remote-agent", "win", "a1", str(self.prompt), transport=transport
            )
        self.assertEqual(self.records[-1][:3], ("agent", "a1", "error"))
        transport = FakeTransport({win.START: subprocess.TimeoutExpired("ssh", 90)})
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_main(
                "remote-agent", "win", "a2", str(self.prompt), transport=transport
            )

    def test_replies_are_parsed_from_the_marked_line(self):
        def answer(stdout, stderr=b""):
            return mock.Mock(
                return_value=subprocess.CompletedProcess([], 1, stdout, stderr)
            )

        with mock.patch.object(
            win, "transport", answer(b"noise\n" + ok({"x": "caf\u00e9"}).encode())
        ):
            self.assertEqual(win.call("win", win.LIST), {"x": "caf\u00e9"})
        with mock.patch.object(
            win, "transport", answer(error("refusing to stop").encode())
        ):
            with self.assertRaisesRegex(win.WindowsAgentError, "refusing to stop"):
                win.call("win", win.LIST)
        clixml = '#< CLIXML\r\n<Objs><S S="Error">boom_x000D__x000A_</S></Objs>'
        with mock.patch.object(
            win, "transport", answer(b"", ("** banner\n" + clixml).encode())
        ):
            with self.assertRaisesRegex(win.WindowsAgentError, "boom"):
                win.call("win", win.LIST)
        with mock.patch.object(win, "transport", answer(b"PRCOORD-OK not-base64\n")):
            with self.assertRaises(win.WindowsAgentError):
                win.call("win", win.LIST)

    # State

    def test_state_is_derived_from_recorded_facts(self):
        self.assertEqual(win.state(running()), "running")
        self.assertEqual(win.state(running(rc="3")), "exited 3")
        self.assertEqual(win.state(running(stopped=True)), "stopped")
        self.assertEqual(win.state(running(live_created=0)), "lost")
        self.assertEqual(win.state(running(owner_match=False)), "lost")
        self.assertEqual(
            win.state(running(pid=0, created=0, live_created=0)), "starting"
        )
        self.assertEqual(
            win.state(
                running(pid=0, created=0, live_created=0, failed="did not start")
            ),
            "failed",
        )
        self.assertEqual(
            win.state({"name": "x", "untrusted": "is a junction or link"}),
            "untrusted (is a junction or link)",
        )
        self.assertEqual(win.state({"name": "x"}), "starting")

    def test_list_prints_each_agent_and_the_environment_session(self):
        reply = ok(
            {
                "agents": [
                    running("a1"),
                    running("a2", rc="0"),
                    {"name": "evil", "untrusted": "is owned by another account"},
                ],
                "env": "running",
            }
        )
        _, out, transport, _ = self.run_main(
            "remote-agent",
            "winenv",
            "--list",
            transport=FakeTransport({win.LIST: reply}),
        )
        self.assertEqual(
            out.splitlines(),
            [
                "a1\trunning\t2026-01-01T00:00:00Z",
                "a2\texited 0\t2026-01-01T00:00:00Z",
                "evil\tuntrusted (is owned by another account)\t-",
                "environment session coordenv: running",
            ],
        )
        self.assertEqual(
            transport.args(win.LIST), [{"root": "~\\agents", "session": "coordenv"}]
        )
        # PowerShell serializes a one-element array as an object.
        _, out, _, _ = self.run_main(
            "remote-agent",
            "win",
            "--list",
            transport=FakeTransport(
                {win.LIST: ok({"agents": running("solo"), "env": ""})}
            ),
        )
        self.assertEqual(out.split("\t")[0], "solo")

    def test_log_decodes_utf8_and_bounds_lines(self):
        reply = ok({"text": "café — ok\nEXIT 0"})
        _, out, transport, _ = self.run_main(
            "remote-agent",
            "win",
            "a1",
            "--log",
            "--lines",
            "5",
            transport=FakeTransport({win.TAIL: reply}),
        )
        self.assertEqual(out, "café — ok\nEXIT 0\n")
        self.assertEqual(transport.args(win.TAIL)[0]["lines"], 5)
        for lines in ("0", "10001"):
            with self.assertRaises(SystemExit):
                self.run_main("remote-agent", "win", "a1", "--log", "--lines", lines)

    # Stop safety

    def test_verification_refuses_missing_zero_reused_foreign_or_other_users_pids(self):
        self.assertTrue(win.verified(running()))
        for bad in (
            {"pid": 0},
            {"pid": None},
            {"pid": -4},
            {"pid": "x"},
            {"created": 0},
            {"live_created": 0},
            {"live_created": CREATED + 1},  # pid reused by a newer process
            {"cmdline_match": False},  # pid now runs something else
            {"cmdline_match": "true"},
            {"owner_match": False},  # another account's process
            {"owner_match": None},
        ):
            self.assertFalse(win.verified(running(**bad)), bad)
        self.assertFalse(win.verified({"name": "a1"}))

    def test_stop_kills_only_a_verified_running_agent(self):
        reply = ok({"agents": [running("a1"), running("a2", pid=99)], "env": ""})
        code, out, _, kills = self.run_main(
            "remote-agent",
            "win",
            "a2",
            "--stop",
            transport=FakeTransport({win.LIST: reply}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            kills,
            [("win", "D:\\src\\project\\out\\agents", "agent", "a2", 99, CREATED)],
        )
        self.assertEqual(self.records[-1][:3], ("agent", "a2", "done"))

    def test_stop_never_kills_an_unverified_finished_or_untrusted_agent(self):
        for rec in (
            running(pid=0, created=0, live_created=0),
            running(live_created=0),
            running(live_created=1),
            running(cmdline_match=False),
            running(owner_match=False),
            running(rc="3"),
            running(stopped=True),
            {"name": "a1", "untrusted": "is a junction or link"},
        ):
            with self.assertRaises(SystemExit):
                self.run_main(
                    "remote-agent",
                    "win",
                    "a1",
                    "--stop",
                    transport=FakeTransport(
                        {win.LIST: ok({"agents": [rec], "env": ""})}
                    ),
                    kill=lambda *a: self.fail(f"kill_tree called for {rec}"),
                )
        with self.assertRaises(SystemExit):
            self.run_main(
                "remote-agent",
                "win",
                "nobody",
                "--stop",
                transport=FakeTransport({win.LIST: ok({"agents": [], "env": ""})}),
                kill=lambda *a: self.fail("kill_tree called for a missing agent"),
            )

    def test_kill_tree_sends_the_verified_identity_as_data(self):
        seen = []

        def transport(host, line, **kwargs):
            seen.append(json.loads(base64.b64decode(line)))
            return subprocess.CompletedProcess([], 0, ok({"killed": 9}).encode(), b"")

        with mock.patch.object(win, "transport", transport):
            self.assertEqual(
                win.kill_tree("win", "D:\\r", "agent", "a1", 9, 123), {"killed": 9}
            )
        self.assertEqual(seen[0]["script"], win.KILL)
        self.assertEqual(
            seen[0]["args"],
            {"root": "D:\\r", "kind": "agent", "name": "a1", "pid": 9, "created": 123},
        )

    def test_host_kill_checks_and_terminates_through_one_handle(self):
        kill = win.KILL.split(win.HELPERS, 1)[1]  # the kill script's own body
        self.assertIn("$expectPid -le 0", kill)
        self.assertIn("[PrCoord.Proc]::Open($expectPid)", kill)
        # identity checked on the handle, then terminated through that handle
        check = kill.index("$proc.Created() -ne $expectCreated")
        self.assertIn("$proc.User() -ne $me", kill)
        self.assertGreater(kill.index("$proc.Terminate()"), check)
        for forbidden in (
            "Stop-Process",
            "taskkill",
            "Get-Process -Name",
            "Win32_Process",
        ):
            self.assertNotIn(forbidden, kill)
        # The wrapper contains itself and its tree in a kill-on-close job first.
        self.assertLess(
            win.RUN.index("[PrCoord.Job]::ContainSelf()"), win.RUN.index("pid.json")
        )
        self.assertIn("0x2000", win.NATIVE)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    def test_only_the_kill_script_opens_processes_with_terminate_rights(self):
        bodies = {
            name: getattr(win, name).split(win.HELPERS, 1)[-1]
            for name in (
                "START",
                "LIST",
                "TAIL",
                "ENV",
                "ENV_PREPARE",
                "HOLDER_FACTS",
                "KILL",
                "HOLDER",
            )
        }
        for name, body in bodies.items():
            if name != "KILL":
                self.assertNotIn("[PrCoord.Proc]::Open(", body, name)
        self.assertNotIn("[PrCoord.Proc]::Open(", win.HELPERS)
        self.assertIn("$proc = [PrCoord.Proc]::Query($id)", win.HELPERS)  # Get-Facts
        self.assertIn(
            "public static Proc Query(int pid) { return OpenWith(pid, 0x1000 | 0x00100000); }",
            win.NATIVE,
        )

    def test_holder_judges_clients_by_their_token_not_their_pid(self):
        holder = win.HOLDER.split(win.HELPERS, 1)[1]
        self.assertNotIn("Pipe]::Client(", holder)
        self.assertIn(
            "[PrCoord.Pipe]::ClientIs($server.SafePipeHandle, $me, 0x3000)", holder
        )
        # ImpersonateNamedPipeClient needs a message read first; nothing acts before the check.
        read = holder.index("$line = $r.ReadLine()")
        check = holder.index("if (-not (Test-Client $server))")
        self.assertLess(read, check)
        self.assertLess(check, holder.index("SetEnvironmentVariable"))
        self.assertIn("RevertToSelf()) Environment.FailFast", win.NATIVE)

    def test_root_may_not_sit_under_a_link(self):
        self.assertIn("function Assert-NoLinkAbove", win.HELPERS)
        root = win.HELPERS[win.HELPERS.index("function Open-Root") :]
        self.assertLess(
            root.index("Assert-NoLinkAbove $root"), root.index("CreateDirectory")
        )

        helpers = win.HELPERS
        for why in (
            "is a junction or link",
            "is owned by another account",
            "grants access to another account",
            "lacks the High integrity label",
        ):
            self.assertIn(why, helpers)
        self.assertIn("S:(ML;OICI;NWNR;;;HI)", win.NATIVE)
        self.assertIn("'start.lock'", win.START)
        self.assertIn("'run.lock'", win.START)
        self.assertIn("'run.lock'", win.RUN)
        self.assertIn("output.1.log", win.START)  # previous log kept
        self.assertIn("ClientIs($server.SafePipeHandle, $me, 0x3000)", win.HOLDER)
        self.assertIn("[PrCoord.Pipe]::Remote(", win.HOLDER)  # SMB clients refused
        self.assertIn("$server.User() -ne $me", win.HELPERS)  # pipe server's user
        self.assertIn("pipe name unavailable", win.HOLDER)
        self.assertIn("Get-Command -CommandType Application", win.RUN)
        # The credential is never a function or cmdlet parameter on the host.
        env = win.ENV.split(win.HELPERS, 1)[1]
        self.assertIn("$w.WriteLine($token)", env)
        for line in env.splitlines():
            if "$token" in line:
                self.assertRegex(
                    line.strip(), r"^(\$token = |if \(-not \$token|\$w\.Write)"
                )

    # Credentials

    def test_agent_env_pipes_the_credential_without_putting_it_in_argv(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        transport = FakeTransport(
            {
                win.ENV_PREPARE: ok({"running": False}),
                win.ENV: ok("environment session started"),
            }
        )
        code, out, transport, _ = self.run_main(
            "agent-env", "winenv", transport=transport, stdin=stdin
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "environment session started")
        (_, s1, a1, p1), (_, s2, a2, p2) = transport.calls
        self.assertEqual((s1, p1), (win.ENV_PREPARE, False))
        self.assertEqual(a1["holder"], win.HOLDER)
        self.assertEqual(
            (s2, a2, p2), (win.ENV, {"root": "~\\agents", "session": "coordenv"}, True)
        )
        stdin.read.assert_not_called()
        stdin.readline.assert_not_called()

    def test_transport_appends_only_the_first_stdin_line_for_passthrough(self):
        runs = []

        def fake_run(argv, **kwargs):
            runs.append((argv, kwargs["input"]))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(b"tok3n_value\nextra\n")
        with mock.patch.object(win.subprocess, "run", fake_run), mock.patch.object(
            sys, "stdin", stdin
        ):
            REAL_TRANSPORT("winenv", "HEADER", passthrough=True)
            REAL_TRANSPORT("winenv", "HEADER")
        (argv, data), (argv2, data2) = runs
        self.assertEqual(data, b"HEADER\ntok3n_value\n")
        self.assertEqual(data2, b"HEADER\n")
        self.assertEqual(argv[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertNotIn("tok3n_value", " ".join(argv))
        self.assertEqual(argv[-2:], ["-EncodedCommand", win.BOOT_ENCODED])

    def test_second_agent_env_says_the_holder_is_already_running(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        transport = FakeTransport({win.ENV_PREPARE: ok({"running": True})})
        _, out, transport, _ = self.run_main(
            "agent-env", "winenv", transport=transport, stdin=stdin
        )
        self.assertIn("already running", out)
        self.assertIn("--stop", out)
        self.assertEqual([s for _, s, _, _ in transport.calls], [win.ENV_PREPARE])

    def test_agent_env_requires_a_session_and_piped_input(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        for host, stdin in (("winenv", tty), ("win", mock.Mock(isatty=lambda: False))):
            transport = FakeTransport()
            with self.assertRaises(SystemExit):
                self.run_main("agent-env", host, transport=transport, stdin=stdin)
            self.assertEqual(transport.calls, [])

    def test_agent_env_stop_kills_only_the_verified_holder(self):
        _, out, _, kills = self.run_main(
            "agent-env",
            "winenv",
            "--stop",
            transport=FakeTransport({win.HOLDER_FACTS: ok(running("holder"))}),
        )
        self.assertEqual(
            kills, [("winenv", "~\\agents", "holder", "coordenv", 4242, CREATED)]
        )
        for facts in (
            running(cmdline_match=False),
            running(owner_match=False),
            {"pid": 0},
        ):
            _, out, _, kills = self.run_main(
                "agent-env",
                "winenv",
                "--stop",
                transport=FakeTransport({win.HOLDER_FACTS: ok(facts)}),
            )
            self.assertEqual(kills, [])
            self.assertIn("not running", out)

    # POSIX

    def test_posix_hosts_keep_the_tmux_launch(self):
        calls = []

        def fake_remote(host, argv, **kwargs):
            calls.append((host, argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(remote_agents, "remote", fake_remote):
            code, out, transport, _ = self.run_main(
                "remote-agent", "posix", "a1", str(self.prompt)
            )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "started agent-a1 on posix")
        self.assertEqual(transport.calls, [])
        ((host, argv, kwargs),) = calls
        self.assertEqual(
            argv,
            [
                "bash",
                "-c",
                remote_agents.START,
                "remote-agent",
                "~/src/project",
                "a1",
                "",
            ],
        )
        files = json.loads(kwargs["input"])
        self.assertIn('claude --model m -p "$(cat "$dir/prompt.md")"', files["run.sh"])
        for flags in (("--list",), ("--stop",), ("--lines", "5")):
            with self.assertRaises(SystemExit):
                self.run_main("remote-agent", "posix", "a1", *flags)


if __name__ == "__main__":
    unittest.main()
