"""Remote implementers: Windows command construction, validation, state and
stop safety, with the SSH runner mocked; POSIX launch kept unchanged."""

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


def encoded(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode() + "\n"


def running(name="a1", **extra):
    rec = {
        "name": name,
        "pid": 4242,
        "created": 638000000000000000,
        "live_created": 638000000000000000,
        "cmdline_match": True,
        "rc": None,
        "stopped": False,
        "started": "2026-01-01T00:00:00Z",
    }
    rec.update(extra)
    return rec


class FakeRemote:
    """Stands in for remote(): records calls and answers by script."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, host, argv, **kwargs):
        self.calls.append((host, argv, kwargs))
        if argv[0] == "powershell-script" and argv[1] == win.KILL:
            raise AssertionError("a remote kill was attempted outside kill_tree")
        out = self.replies.get(argv[1], "")
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(argv, 0, out, "")


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
            patcher = mock.patch.object(target, "P", self.project)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in (win, remote_agents):
            patcher = mock.patch.object(
                target, "record", lambda *a: self.records.append(a)
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text('Fix "it" & $(rm) %PATH% — café\n', encoding="utf-8")

    def run_main(self, command, *argv, remote=None, kill=None, stdin=None):
        remote = remote or FakeRemote()
        kills = []
        patches = [
            mock.patch.object(win, "remote", remote),
            mock.patch.object(remote_agents, "remote", remote),
            mock.patch.object(
                win,
                "kill_tree",
                kill or (lambda *a: kills.append(a) or {"killed": [1, 2]}),
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
        return code, out.getvalue(), remote, kills

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
        ):
            self.assertFalse(win.valid_name(bad), bad)

    def test_bad_names_never_reach_the_host(self):
        for bad in ("a;b", "CON", "x'y", "..\\x"):
            remote = FakeRemote()
            with self.assertRaises(SystemExit):
                self.run_main(
                    "remote-agent", "win", bad, str(self.prompt), remote=remote
                )
            self.assertEqual(remote.calls, [])

    def test_agent_root_defaults_to_checkout_and_rejects_unsafe_paths(self):
        self.assertEqual(win.agent_root("win"), "D:\\src\\project\\out\\agents")
        self.assertEqual(win.agent_root("winenv"), "~\\agents")
        for bad in (
            "agents",
            "D:\\a\\..\\b",
            "D:\\a&b",
            'D:\\a"b',
            "D:\\%x%",
            "\\\\server\\share",
        ):
            self.project.hosts["win"]["agent_root"] = bad
            with self.assertRaises(win.WindowsAgentError, msg=bad):
                win.agent_root("win")

    def test_agent_command_appends_prompt_flag_or_uses_prompt_file(self):
        self.assertEqual(win.agent_argv("win", "a1"), ["claude", "--model", "m", "-p"])
        self.assertEqual(
            win.agent_argv("winenv", "a1"),
            ["C:\\Tools\\copilot.cmd", "--prompt-file", "{prompt_file}"],
        )
        for bad in ("a&b", "50%", "x|y", 'q"', "a>b", "!x", "^x"):
            self.project.settings["remote_agents"]["command"] = ["claude", bad]
            with self.assertRaises(win.WindowsAgentError, msg=bad):
                win.agent_argv("win", "a1")

    def test_start_sends_prompt_only_as_a_file_on_stdin(self):
        remote = FakeRemote({win.START: encoded({"pid": 77, "created": 5})})
        code, out, remote, _ = self.run_main(
            "remote-agent", "win", "a1", str(self.prompt), remote=remote
        )
        self.assertEqual(code, 0)
        self.assertIn("pid 77", out)
        ((host, argv, kwargs),) = remote.calls
        self.assertEqual(host, "win")
        self.assertEqual(
            argv,
            ["powershell-script", win.START, "D:\\src\\project\\out\\agents", "a1", ""],
        )
        prompt = self.prompt.read_text(encoding="utf-8")
        self.assertFalse(any(prompt.strip() in arg for arg in argv))
        files = json.loads(base64.b64decode(kwargs["input"]))["files"]
        self.assertEqual(files["prompt.md"], prompt)
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
        for name in ("run.ps1", "launch.json", "exit.code", "pid.json"):
            runbook = self.root / name
            runbook.write_text("x")
            self.project.settings["remote_agents"]["runbook"] = str(runbook)
            remote = FakeRemote()
            with self.assertRaises(SystemExit):
                self.run_main(
                    "remote-agent", "win", "a1", str(self.prompt), remote=remote
                )
            self.assertEqual(remote.calls, [])

    def test_failed_launch_is_recorded(self):
        remote = FakeRemote({win.START: subprocess.TimeoutExpired("ssh", 90)})
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_main("remote-agent", "win", "a1", str(self.prompt), remote=remote)
        self.assertEqual(self.records[-1][:3], ("agent", "a1", "error"))

    def test_state_is_derived_from_recorded_facts(self):
        self.assertEqual(win.state(running()), "running")
        self.assertEqual(win.state(running(rc="3")), "exited 3")
        self.assertEqual(win.state(running(stopped=True)), "stopped")
        self.assertEqual(win.state(running(live_created=0)), "lost")
        self.assertEqual(
            win.state(running(pid=0, created=0, live_created=0)), "starting"
        )
        self.assertEqual(win.state({"name": "x"}), "starting")

    def test_list_prints_each_agent_and_the_environment_session(self):
        reply = encoded(
            {"agents": [running("a1"), running("a2", rc="0")], "env": "running"}
        )
        _, out, remote, _ = self.run_main(
            "remote-agent", "winenv", "--list", remote=FakeRemote({win.LIST: reply})
        )
        self.assertEqual(
            out.splitlines(),
            [
                "a1\trunning\t2026-01-01T00:00:00Z",
                "a2\texited 0\t2026-01-01T00:00:00Z",
                "environment session coordenv: running",
            ],
        )
        self.assertEqual(remote.calls[0][1][2:], ["~\\agents", "coordenv"])
        # PowerShell serializes a one-element array as an object.
        _, out, _, _ = self.run_main(
            "remote-agent",
            "win",
            "--list",
            remote=FakeRemote(
                {win.LIST: encoded({"agents": running("solo"), "env": ""})}
            ),
        )
        self.assertEqual(out.split("\t")[0], "solo")

    def test_log_decodes_utf8_from_the_host(self):
        reply = encoded({"text": "café — ok\nEXIT 0"})
        _, out, remote, _ = self.run_main(
            "remote-agent",
            "win",
            "a1",
            "--log",
            "--lines",
            "5",
            remote=FakeRemote({win.TAIL: reply}),
        )
        self.assertEqual(out, "café — ok\nEXIT 0\n")
        self.assertEqual(
            remote.calls[0][1][2:], ["D:\\src\\project\\out\\agents", "a1", "5"]
        )

    def test_verification_refuses_missing_zero_reused_or_foreign_pids(self):
        self.assertTrue(win.verified(running()))
        for bad in (
            {"pid": 0},
            {"pid": None},
            {"pid": -4},
            {"pid": "x"},
            {"created": 0},
            {"live_created": 0},
            {"live_created": 638000000000000001},  # pid reused by a newer process
            {"cmdline_match": False},  # pid now runs something else
            {"cmdline_match": "true"},
        ):
            self.assertFalse(win.verified(running(**bad)), bad)
        self.assertFalse(win.verified({"name": "a1"}))

    def test_stop_kills_only_a_verified_running_agent(self):
        reply = encoded({"agents": [running("a1"), running("a2")], "env": ""})
        code, out, _, kills = self.run_main(
            "remote-agent", "win", "a2", "--stop", remote=FakeRemote({win.LIST: reply})
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            kills,
            [
                (
                    "win",
                    "D:\\src\\project\\out\\agents",
                    "agent",
                    "a2",
                    4242,
                    638000000000000000,
                )
            ],
        )
        self.assertIn("2 processes", out)

    def test_stop_never_kills_an_unverified_or_finished_agent(self):
        for rec in (
            running(pid=0, created=0, live_created=0),
            running(live_created=0),
            running(live_created=1),
            running(cmdline_match=False),
            running(rc="3"),
            running(stopped=True),
        ):
            reply = encoded({"agents": [rec], "env": ""})
            with self.assertRaises(SystemExit):
                self.run_main(
                    "remote-agent",
                    "win",
                    "a1",
                    "--stop",
                    remote=FakeRemote({win.LIST: reply}),
                    kill=lambda *a: self.fail(f"kill_tree called for {rec}"),
                )
        with self.assertRaises(SystemExit):
            self.run_main(
                "remote-agent",
                "win",
                "nobody",
                "--stop",
                remote=FakeRemote({win.LIST: encoded({"agents": [], "env": ""})}),
                kill=lambda *a: self.fail("kill_tree called for a missing agent"),
            )

    def test_kill_tree_passes_the_verified_identity_to_the_host_check(self):
        remote = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, encoded({"killed": [9]}), ""
            )
        )
        with mock.patch.object(win, "remote", remote):
            self.assertEqual(
                win.kill_tree("win", "D:\\r", "agent", "a1", 9, 123), {"killed": [9]}
            )
        argv = remote.call_args.args[1]
        self.assertEqual(
            argv, ["powershell-script", win.KILL, "D:\\r", "agent", "a1", "9", "123"]
        )
        # The host script refuses a pid it cannot verify, and kills from the recorded root only.
        self.assertIn("refusing to stop", win.KILL)
        self.assertIn("$expectPid -le 0", win.KILL)
        self.assertNotIn("Get-Process -Name", win.KILL)
        self.assertNotIn("taskkill", win.KILL)

    def test_agent_env_pipes_the_credential_through_without_reading_it(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        remote = FakeRemote(
            {
                win.ENV_PREPARE: encoded({"running": False}),
                win.ENV: "environment session started\n",
            }
        )
        code, out, remote, _ = self.run_main(
            "agent-env", "winenv", remote=remote, stdin=stdin
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "environment session started")
        prepare, env = remote.calls
        self.assertEqual(prepare[1][1], win.ENV_PREPARE)
        self.assertEqual(base64.b64decode(prepare[2]["input"]).decode(), win.HOLDER)
        self.assertEqual(env[1][1:], [win.ENV, "~\\agents", "coordenv"])
        self.assertNotIn("input", env[2])  # ssh inherits stdin; Python never reads it
        stdin.read.assert_not_called()
        stdin.readline.assert_not_called()

    def test_agent_env_requires_a_session_and_piped_input(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        for host, stdin in (("winenv", tty), ("win", mock.Mock(isatty=lambda: False))):
            remote = FakeRemote()
            with self.assertRaises(SystemExit):
                self.run_main("agent-env", host, remote=remote, stdin=stdin)
            self.assertEqual(remote.calls, [])

    def test_agent_env_stop_kills_only_the_verified_holder(self):
        facts = encoded(running("holder"))
        _, out, _, kills = self.run_main(
            "agent-env",
            "winenv",
            "--stop",
            remote=FakeRemote({win.HOLDER_FACTS: facts}),
        )
        self.assertEqual(
            kills,
            [("winenv", "~\\agents", "holder", "coordenv", 4242, 638000000000000000)],
        )
        _, out, _, kills = self.run_main(
            "agent-env",
            "winenv",
            "--stop",
            remote=FakeRemote(
                {win.HOLDER_FACTS: encoded(running(cmdline_match=False))}
            ),
        )
        self.assertEqual(kills, [])
        self.assertIn("not running", out)

    def test_host_errors_are_readable(self):
        clixml = '#< CLIXML\r\n<Objs Version="1.1.0.1"><S S="Error">agent is already running_x000D__x000A_</S></Objs>'
        self.assertEqual(
            win.stderr_lines("** WARNING: banner\n" + clixml),
            ["agent is already running"],
        )
        failing = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", clixml))
        with mock.patch.object(win, "remote", failing), self.assertRaisesRegex(
            win.WindowsAgentError, "already running"
        ):
            win.call("win", win.LIST, "D:\\r", "")
        with self.assertRaises(win.WindowsAgentError):
            win.decode("not base64 json")

    def test_scripts_take_values_as_arguments_not_interpolated_code(self):
        for script in (
            win.START,
            win.LIST,
            win.TAIL,
            win.KILL,
            win.ENV,
            win.ENV_PREPARE,
            win.HOLDER_FACTS,
        ):
            self.assertIn("Resolve-Root $args[0]", script)
            self.assertNotIn("Invoke-Expression", script)
            self.assertNotIn("iex ", script)
        for script in (win.RUN, win.HOLDER):
            self.assertNotIn("Invoke-Expression", script)
            self.assertNotIn("$args", script)

    def test_posix_hosts_keep_the_tmux_launch(self):
        remote = FakeRemote()
        code, out, remote, _ = self.run_main(
            "remote-agent", "posix", "a1", str(self.prompt), remote=remote
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "started agent-a1 on posix")
        ((host, argv, kwargs),) = remote.calls
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
        for flag in ("--list", "--stop"):
            with self.assertRaises(SystemExit):
                self.run_main("remote-agent", "posix", "a1", flag)


if __name__ == "__main__":
    unittest.main()
