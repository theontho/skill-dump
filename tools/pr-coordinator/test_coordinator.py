"""Exercise the CLI, migration and train against isolated config, Git and gh."""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from runtime import Project
from train import only_cancelled_jobs

TOOL = Path(__file__).resolve().parent
OUT = TOOL.parents[1] / "out"


class ProjectCLI(unittest.TestCase):
    def setUp(self):
        self.root = OUT / f"pr-coord-test-{uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.data = self.root / "state"
        self.checkout = self.root / "checkout"
        self.config = self.root / "project.toml"
        self.config.write_text(f"""
[github]
repo = "example/gadgets"
owner = "owner"
tracked_authors = ["example-bot"]
base_branch = "trunk"
[paths]
checkout = {json.dumps(str(self.checkout))}
worktree_root = {json.dumps(str(self.root))}
[lane]
generated = ['generated\\.txt']
[landing]
quick_tests = ["tests/removed.py"]
quick_gate = ["{sys.executable}", "-c", "raise SystemExit(90)"]
""")
        self.env = {
            **os.environ,
            "XDG_STATE_HOME": str(self.root / "xdg"),
            "XDG_CONFIG_HOME": str(self.root / "settings"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }

    def command(self, *args, check=True, timeout=None):
        return subprocess.run(
            [
                sys.executable,
                str(TOOL / "pr-coord"),
                "--project",
                "gadgets",
                "--config",
                str(self.config),
                "--data-dir",
                str(self.data),
                *args,
            ],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def test_config_selection_and_data_override(self):
        config = Project(
            "gadgets", config_file=self.config, data_dir=self.data, required=True
        )
        self.assertEqual(config.base_ref, "origin/trunk")
        self.assertEqual(config.authors, {"owner", "example-bot"})
        self.assertTrue(config.generated.fullmatch("generated.txt"))
        self.assertFalse(config.generated.fullmatch("other/generated.txt"))
        self.assertEqual(config.worktree(3).parent, self.root)
        self.assertEqual(
            json.loads(self.command("status").stdout)["data"], str(self.data)
        )
        with self.assertRaisesRegex(ValueError, "invalid project name"):
            Project("../gadgets")

    def test_migration_preserves_existing_state_and_does_not_import_scripts_or_locks(
        self,
    ):
        old = self.root / "old"
        (old / "locks").mkdir(parents=True)
        (old / "tests_inventory_runs").mkdir()
        for path, contents in {
            "state.json": '{"old": true}',
            "questions.json": "[]",
            "dashboard.py": "not data",
            "events_known.txt": "assigned check\n",
            "tests_inventory_runs/42-1.json": "[]",
            "locks/lane.lock": "",
            "locks/hold": "",
        }.items():
            (old / path).write_text(contents)
        (old / "train.json").symlink_to(self.config)
        self.data.mkdir()
        (self.data / "state.json").write_text('{"keep": true}')
        self.command("migrate", "--from", str(old))
        self.command("migrate", "--from", str(old))
        self.assertEqual(
            json.loads((self.data / "state.json").read_text()), {"keep": True}
        )
        self.assertTrue((self.data / "locks/hold").exists())
        self.assertTrue((self.data / "tests_inventory_runs/42-1.json").exists())
        self.assertEqual(
            json.loads((self.data / "events_known.json").read_text()),
            ["assigned check"],
        )
        for path in ("train.json", "dashboard.py", "locks/lane.lock"):
            self.assertFalse((self.data / path).exists(), path)

    def test_agent_and_owner_questions_are_independent(self):
        self.command("ask", "agent question", "--pr", "#5", "--machine", "worker")
        id_ = json.loads(self.command("agent-questions").stdout)[0]["id"]
        self.command("agent-questions", id_, "resolved")
        self.command("ask", "--owner", "owner question")
        self.command("answer", "1", "yes")
        self.assertEqual(
            json.loads((self.data / "questions.json").read_text())[0]["answer"], "yes"
        )
        self.assertEqual(
            json.loads((self.data / "agent_questions.json").read_text())[0]["answer"],
            "resolved",
        )

    def test_dashboard_keeps_question_line_breaks(self):
        import dashboard

        questions = self.root / "rendered-questions.json"
        questions.write_text(json.dumps([
            {"id": 1, "question": "Intro\n- #1 <go>\n- #2 hold", "default": "hold"},
            {"id": 2, "question": "Done?\nyes", "answer": "ok\nthanks"},
        ]))
        with mock.patch.object(dashboard, "QUESTIONS", questions), mock.patch.object(
            dashboard, "STATE", self.root / "missing-state.json"
        ):
            html = dashboard.page()
        self.assertIn("<div class=qtext>Intro\n- #1 &lt;go&gt;\n- #2 hold</div>", html)
        self.assertIn("<div class=qtext>Done?\nyes</div>", html)
        self.assertIn("white-space:pre-wrap", html)
        # An answer being typed must survive the periodic refresh.
        self.assertNotIn("http-equiv=refresh", html)
        self.assertIn("localStorage", html)
        self.assertIn("document.activeElement", html)

    def idle_project(self, *, threshold_override=None):
        with self.config.open("a") as config:
            config.write("""
[hosts.worker]
display = "Worker display"
ssh = "worker-alias"
[hosts.dedicated]
idle_alert = false
""")
            if threshold_override is not None:
                config.write(f"\n[events]\nidle_minutes = {threshold_override}\n")
        return Project("gadgets", config_file=self.config, data_dir=self.data)

    def test_idle_event_uses_continuous_reachable_observations(self):
        from events import idle_hosts
        from host_activity import update_idle

        project = self.idle_project()
        cases = [
            ("idle", {}, {"reachable": True}, True),
            ("unreachable", {}, {"reachable": False}, False),
            ("missing observation", {}, {}, False),
            (
                "agent key",
                {"agents": [{"machine": "worker", "status": "working"}]},
                {"reachable": True},
                False,
            ),
            (
                "agent display",
                {"agents": [{"machine": "Worker display", "status": "waiting"}]},
                {"reachable": True},
                False,
            ),
            (
                "agent alias",
                {"agents": [{"machine": "worker-alias", "status": "working"}]},
                {"reachable": True},
                False,
            ),
            (
                "held lease",
                {},
                {"reachable": True, "leases": [{"state": "held"}]},
                False,
            ),
            (
                "waiting lease",
                {},
                {"reachable": True, "leases": [{"state": "waiting"}]},
                True,
            ),
            (
                "certification",
                {"certs": [{"host": "worker", "status": "running"}]},
                {"reachable": True},
                False,
            ),
            (
                "remote tests",
                {"runs": [{"host": "worker", "status": "running"}]},
                {"reachable": True},
                False,
            ),
            (
                "heavy process",
                {},
                {"reachable": True, "leases": [{"state": "running"}]},
                False,
            ),
        ]
        for name, work, worker, expected in cases:
            with self.subTest(name=name):
                state = {
                    **work,
                    "hosts": {"worker": worker, "dedicated": {"reachable": True}},
                }
                for minute in range(46):
                    now = minute * 60
                    update_idle(state, project, observed_at=now)
                    matches = idle_hosts(state, project, now=now)
                    self.assertEqual(bool(matches), expected and minute == 45)
                if expected:
                    self.assertEqual(
                        matches[0][1],
                        "IDLE: Worker display has had no agent, lease or long run for 45 min",
                    )
                    self.assertFalse(idle_hosts(state, project, now=now + 3600))

    def test_idle_event_rearms_only_after_work_and_shares_dashboard_marker(self):
        import dashboard
        from coord import derive_machines
        from host_activity import update_idle

        project = self.idle_project(threshold_override=1)
        self.data.mkdir()
        path = self.data / "state.json"
        fake_gh = self.root / "gh"
        fake_gh.write_text("#!/bin/sh\nexit 0\n")
        fake_gh.chmod(0o755)
        self.env["PATH"] = f"{self.root}{os.pathsep}{self.env['PATH']}"
        start = int(time.time()) - 190
        state = {"hosts": {"worker": {"reachable": True}}, "prs": [], "log": []}

        def qualify(since):
            update_idle(state, project, observed_at=since)
            busy = update_idle(state, project, observed_at=since + 60)
            derive_machines(state, project.hosts, busy)
            path.write_text(json.dumps(state))

        qualify(start)
        with (
            mock.patch.object(dashboard, "STATE", path),
            mock.patch.object(dashboard, "P", project),
        ):
            self.assertIn("idle since ", dashboard.page())
        self.assertIn("IDLE: Worker display", self.command("events", timeout=5).stdout)
        state["hosts"]["worker"]["reachable"] = False
        update_idle(state, project, observed_at=start + 61)
        state["hosts"]["worker"]["reachable"] = True
        qualify(start + 63)
        self.command("ask", "next question", "--machine", "worker")
        result = self.command("events", timeout=5).stdout
        self.assertIn("AGENT QUESTION", result)
        self.assertNotIn("IDLE:", result)

        self.command("agent", "brief-worker", "working", "--machine", "worker-alias")
        self.command("agent", "brief-worker", "done")
        state = json.loads(path.read_text())
        qualify(start + 126)
        self.assertIn("IDLE: Worker display", self.command("events", timeout=5).stdout)

    def test_host_snapshot_reports_io_lease_as_work(self):
        from collect_hosts import leases

        with mock.patch(
            "collect_hosts.run",
            return_value="  L17 held    1-core cap, 0 used   0.5G     5 min  dev: download (pid 17)\n",
        ):
            snapshot = leases(None)
        self.assertTrue(snapshot["reachable"])
        self.assertEqual([(row["state"], row["label"]) for row in snapshot["leases"]], [("held", "dev: download")])

    def test_certification_waits_when_the_controller_finds_the_host_busy(self):
        import contextlib

        import remote

        calls, states = [], []

        class FakeProject:
            data = self.root
            hosts = {"worker": {"slots": 1}}

            def get(self, section, key, default=None):
                return {
                    "slots": {"generic": ["worker"]},
                    "busy_markers": ["No available certification host"],
                    "test_failure_markers": ["failed tests"],
                }.get(key, default)

            def command(self, section, key, *, default=(), **values):
                return ["certify"] if key == "local_command" else []

            def expand(self, argv, **values):
                return list(argv)

            def ssh(self, key):
                return None

            def reachable(self, key):
                return True

        def fake_run(argv, *, stdout=None, **kwargs):
            calls.append(argv)
            busy = len(calls) == 1
            stdout.write("No available certification host\n" if busy else "ok\n")
            stdout.flush()
            return subprocess.CompletedProcess(argv, 1 if busy else 0)

        def fake_record(kind, *args):
            if kind == "cert-state":
                states.append(args[1])

        with mock.patch.object(remote, "P", FakeProject()), mock.patch.object(
            remote, "locked", lambda *a, **k: contextlib.nullcontext()
        ), mock.patch.object(remote, "run", fake_run), mock.patch.object(
            remote, "record", fake_record
        ), mock.patch.object(
            remote, "json_gh", lambda *a: {"headRefOid": "a" * 40}
        ), mock.patch.object(
            remote, "cert_args", lambda *a: {}
        ), mock.patch.object(remote.time, "sleep"):
            code = remote.certify(self.root, 7, "generic", "a" * 40, "b" * 40)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("error", states)
        self.assertEqual(states[-1], "pass")

    def test_certification_skips_at_once_when_no_host_is_online(self):
        import contextlib

        import remote

        states, ran = [], []

        class OfflineProject:
            data = self.root
            hosts = {"laptop": {"slots": 1, "ssh": "laptop"}}

            def get(self, section, key, default=None):
                return {"slots": {"macos": ["laptop"]}, "must_run": ["generic"]}.get(
                    key, default
                )

            def reachable(self, key):
                return False

        with mock.patch.object(remote, "P", OfflineProject()), mock.patch.object(
            remote, "locked", lambda *a, **k: contextlib.nullcontext()
        ), mock.patch.object(
            remote, "record", lambda kind, *a: states.append(a[1]) if kind == "cert-state" else None
        ), mock.patch.object(remote, "run", lambda *a, **k: ran.append(a)), mock.patch.object(
            remote.time, "sleep", side_effect=AssertionError("waited for an offline host")
        ):
            self.assertEqual(remote.certify(self.root, 7, "macos", "a" * 40, "b" * 40), 0)
        self.assertEqual(states[-1], "skipped")
        self.assertEqual(ran, [])

    def test_optional_platform_errors_do_not_block_readiness(self):
        import drafts

        class Drafts:
            def get(self, section, key, default=None):
                return ["local-ci/generic"] if key == "required_statuses" else default

        ok = {"local-ci/generic": "success"}
        with mock.patch.object(drafts, "P", Drafts()):
            self.assertTrue(drafts.complete((True, {**ok, "local-ci/macos-arm64": "error"}, False)))
            self.assertFalse(drafts.complete((True, {**ok, "local-ci/macos-arm64": "failure"}, False)))
            self.assertFalse(drafts.complete((True, {"local-ci/generic": "error"}, False)))
            self.assertFalse(drafts.complete((True, {**ok, "local-ci/windows-x64": "pending"}, False)))

    def test_finish_draft_requests_missing_certification_once_per_head(self):
        import drafts

        class Drafts:
            def get(self, section, key, default=None):
                return 0 if key in {"poll_seconds", "idle_seconds"} else default

        head = "a" * 40
        with mock.patch.object(drafts, "P", Drafts()), mock.patch.object(
            drafts, "owner_pr", return_value={"headRefOid": head}
        ), mock.patch.object(
            drafts, "evidence", return_value=(True, {}, False)
        ), mock.patch.object(drafts, "request_certification") as request:
            with self.assertRaisesRegex(ValueError, "no running certification"):
                drafts.watch(7, certify=True)
            request.assert_called_once_with(7)
            request.reset_mock()
            with self.assertRaisesRegex(ValueError, "no running certification"):
                drafts.watch(7)
            request.assert_not_called()

    def test_certification_of_an_older_head_does_not_count_for_the_new_head(self):
        import fcntl

        import drafts

        locks = self.root / "locks"
        locks.mkdir()

        class Data:
            data = self.root

        old, new = "a" * 40, "b" * 40
        with (locks / f"cert-pr-7-{old}.lock").open("a") as held, mock.patch.object(
            drafts, "P", Data()
        ), mock.patch("runtime.P", Data()):
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertTrue(drafts.running_certs(7, old))
            self.assertFalse(drafts.running_certs(7, new))

    def fake_draft(self, head, *, statuses, review_head=None):
        state = self.root / "ready"
        review = {
            "state": "OPEN",
            "baseRefName": "trunk",
            "headRefOid": head,
            "isCrossRepository": False,
            "author": {"login": "owner"},
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "reviewDecision": None,
        }
        command = self.root / "gh"
        command.write_text(
            f"""#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
state = Path({str(state)!r})
if args[:2] == ["api", "user"]:
    print(json.dumps({{"login": "owner"}}))
elif args[:2] == ["api", "graphql"]:
    row = {review!r}
    row["isDraft"] = not state.exists()
    print(json.dumps({{"data": {{"repository": {{"pullRequest": row}}}}}}))
elif args[:2] == ["pr", "view"]:
    print(json.dumps({{"headRefName": "feature", "headRefOid": {head!r}}}))
elif args[:2] == ["pr", "ready"]:
    state.touch()
elif args[1].endswith("/reviews"):
    print(json.dumps([[{{"commit_id": {(review_head or head)!r},
        "state": "COMMENTED", "user": {{"login": "copilot-pull-request-reviewer[bot]", "type": "Bot"}}}}]]))
elif args[1].endswith("/statuses"):
    print(json.dumps([{statuses!r}]))
elif "/actions/workflows/" in args[1]:
    print(json.dumps({{"workflow_runs": [{{"head_sha": {head!r}, "head_branch": "feature",
        "head_repository": {{"full_name": "example/gadgets"}}, "pull_requests": [],
        "display_title": "PR area / ready_for_review / ready"}}]}}))
else:
    raise SystemExit(f"unexpected fake gh invocation: {{args}}")
"""
        )
        command.chmod(0o700)
        self.env["PATH"] = str(self.root) + os.pathsep + os.environ["PATH"]
        return state

    def draft_config(self):
        gate = self.root / "gate.py"
        gate.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "assert sys.argv[1] == '7'\n"
            "count = Path(sys.argv[0]).with_suffix('.count')\n"
            "seen = count.exists()\n"
            "count.touch()\n"
            "raise SystemExit(0 if seen else 3)\n"
        )
        with self.config.open("a") as f:
            f.write(
                f'\nci_capacity = [{json.dumps(sys.executable)}, {json.dumps(str(gate))}, "{{pr}}"]\n'
                '[drafts]\nrequired_statuses = ["local-ci/generic", "local-ci/macos"]\n'
                "idle_seconds = 0\ncapacity_retry_seconds = 0\n"
                'ready_workflow = "pull_request.yml"\nready_run_timeout = 0\n'
            )

    def test_finish_draft_retries_capacity_and_reuses_certification_worktree(self):
        _, head = self.create_git()
        self.git("switch", "trunk")
        cert = self.root / "wt-cert7"
        self.git("worktree", "add", str(cert), "feature")
        self.draft_config()
        state = self.fake_draft(
            head,
            statuses=[
                {"context": "local-ci/generic", "state": "success"},
                {"context": "local-ci/macos", "state": "success"},
                {"context": "local-ci/generic", "state": "failure"},  # superseded
            ],
        )
        result = self.command("finish-draft", "7")
        self.assertIn("handed to the landing train", result.stdout)
        self.assertTrue(state.exists())
        self.assertFalse((self.data / "ready-admission.json").exists())
        self.assertEqual(
            json.loads((self.data / "train.json").read_text())[0]["worktree"], str(cert)
        )

    def test_finish_draft_rejects_stale_review_and_missing_certification(self):
        self.draft_config()
        for stale in (False, True):
            statuses = [{"context": "local-ci/generic", "state": "success"}]
            if stale:
                statuses.append({"context": "local-ci/macos", "state": "success"})
            state = self.fake_draft(
                "a" * 40, statuses=statuses, review_head="b" * 40 if stale else None
            )
            result = self.command("finish-draft", "7", check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("ATTENTION", result.stdout)
            self.assertFalse(state.exists())
            self.assertFalse((self.data / "train.json").exists())

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.checkout,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def create_git(self, name="generated.txt"):
        remote = self.root / "remote.git"
        self.git("init", "--bare", "--initial-branch=trunk", str(remote), cwd=self.root)
        self.git("clone", str(remote), str(self.checkout), cwd=self.root)
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.checkout / name).write_text("initial")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.git("push", "origin", "trunk")
        self.git("switch", "-c", "feature")
        (self.checkout / name).write_text("changed on branch")
        self.git("commit", "-am", "changes in generated file")
        self.git("push", "origin", "feature")
        head = self.git("rev-parse", "HEAD")
        self.git("switch", "trunk")
        self.git("rm", name)
        self.git("commit", "-m", "remove stale generated output")
        self.git("push", "origin", "trunk")
        self.git("switch", "feature")
        return remote, head

    def fake_review(self, head, *, queued):
        path = self.root / "gh"
        review = dict(
            state="OPEN",
            isDraft=False,
            baseRefName="trunk",
            headRefOid=head,
            isCrossRepository=False,
            author={"login": "owner"},
            labels={"nodes": [], "pageInfo": {"hasNextPage": False}},
            mergeQueueEntry={"position": 1} if queued else None,
        )
        data = {"data": {"repository": {"pullRequest": review}}}
        path.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "assert sys.argv[1:3] == ['api', 'graphql'], sys.argv\n"
            'assert \'owner:"example",name:"gadgets"\' in sys.argv[-1], sys.argv\n'
            f"print({json.dumps(data)!r})\n"
        )
        path.chmod(0o700)
        self.env["PATH"] = str(self.root) + os.pathsep + os.environ["PATH"]

    def refresh(self):
        source = """import sys
from pathlib import Path
import runtime
runtime.P = runtime.Project('gadgets', config_file=sys.argv[1], data_dir=sys.argv[2], required=True)
runtime.P.prepare()
from train import refresh
print(refresh(7, Path(sys.argv[3]), 'feature'))
"""
        output = subprocess.run(
            [
                sys.executable,
                "-c",
                source,
                str(self.config),
                str(self.data),
                str(self.checkout),
            ],
            cwd=TOOL,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return output.stdout.strip()

    def test_train_never_pushes_enqueued_head_or_discards_unpushed_merge(self):
        remote, head = self.create_git()
        self.fake_review(head, queued=True)
        self.assertEqual(self.refresh(), "queued")
        merged = self.git("rev-parse", "HEAD")
        self.assertNotEqual(merged, head)
        self.assertEqual(
            self.git("--git-dir", str(remote), "rev-parse", "feature"), head
        )
        self.fake_review(head, queued=False)
        self.assertEqual(self.refresh(), "True")
        self.assertEqual(
            self.git("--git-dir", str(remote), "rev-parse", "feature"), merged
        )
        self.assertFalse((self.checkout / "generated.txt").exists())

    def test_busy_remote_quick_gate_falls_back_without_synchronizing(self):
        remote, head = self.create_git()
        self.fake_review(head, queued=False)
        ran = self.root / "local-quick-gate"
        reached = self.root / "remote-admission-checked"
        config = self.config.read_text()
        config = config[: config.index("quick_tests")]
        self.config.write_text(
            config
            + f"""
quick_host = "worker"
quick_gate = [{json.dumps(sys.executable)}, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", {json.dumps(str(ran))}]
[hosts.worker]
ssh = "worker"
rt = true
checkout = "~/src/gadgets"
rt_admission = ["agent-coord", "check", "--cores", "4", "--mem", "8G"]
[remote_tests]
prepare = ["must-not-run"]
"""
        )
        ssh = self.root / "ssh"
        ssh.write_text(
            f"""#!{sys.executable}
import sys
from pathlib import Path
if not sys.argv[-1].startswith("agent-coord check "):
    raise SystemExit("remote preparation or testing was attempted")
Path({str(reached)!r}).touch()
raise SystemExit(1)
"""
        )
        ssh.chmod(0o700)
        result = self.command(
            "rt",
            "worker",
            str(self.checkout),
            "--admit-now",
            "--",
            "false",
            check=False,
        )
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertTrue(reached.exists())
        self.assertFalse(ran.exists())
        reached.unlink()
        self.assertEqual(self.refresh(), "True")
        self.assertTrue(reached.exists())
        self.assertTrue(ran.exists())
        self.assertNotEqual(
            self.git("--git-dir", str(remote), "rev-parse", "feature"), head
        )

    def test_train_waits_for_github_to_report_its_own_push(self):
        import train

        reported = iter(["old", "old", "pushed"])
        with mock.patch.object(
            train, "pr_info", lambda n: {"headRefOid": next(reported)}
        ), mock.patch.object(train.time, "sleep"):
            self.assertEqual(train.settled_head(7, "pushed"), "pushed")
        with mock.patch.object(
            train, "pr_info", lambda n: {"headRefOid": "someone-else"}
        ), mock.patch.object(train.time, "sleep"):
            self.assertEqual(train.settled_head(7, "pushed"), "someone-else")

    def test_train_retries_a_transient_failure_before_asking_for_help(self):
        import contextlib

        import train

        calls, asked = [], []

        def flaky(entry):
            calls.append(entry["pr"])
            if len(calls) < 3:
                raise subprocess.CalledProcessError(1, ["gh", "pr", "diff"])

        patches = (
            mock.patch.object(train, "land", flaky),
            mock.patch.object(train, "locked", lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(train, "need_agent", lambda n, why: asked.append(why)),
            mock.patch.object(train, "sh", lambda *a, **k: (0, "")),
            mock.patch.object(train.time, "sleep"),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            train.land_safely({"pr": 7, "worktree": "wt"})
            self.assertEqual((len(calls), asked), (3, []))
            calls.clear()
            train.land_safely({"pr": 7, "worktree": "wt"}, attempts=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(asked), 1)

    def test_remote_test_status_uses_a_command_the_cli_accepts(self):
        import remote

        calls = []
        with mock.patch.object(remote, "run", lambda argv, **k: calls.append(argv)):
            remote.record("run-state", "rt-1", "done")
        self.assertIn("run-state", calls[0])
        result = self.command("run-state", "rt-1", "running", "--machine", "worker", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('record("run",', (TOOL / "remote.py").read_text())

    def test_train_retries_cancellations_only_not_timeouts_or_test_failures(self):
        jobs = [
            {"name": "required", "conclusion": "failure"},
            {"name": "one", "conclusion": "cancelled"},
        ]
        self.assertTrue(only_cancelled_jobs(jobs, {"required"}))
        for result in ("failure", "timed_out", "action_required", None):
            self.assertFalse(
                only_cancelled_jobs(
                    [*jobs, {"name": "tests", "conclusion": result}], {"required"}
                )
            )

    def test_queue_rebuild_retry_waits_for_hold_and_never_uses_a_different_head(self):
        state = self.root / "enqueue"
        gh = self.root / "gh"
        gh.write_text(
            f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
kind, action, *rest = sys.argv[1:]
head = os.environ["TEST_PR_HEAD"]
p = Path({str(state)!r})
if kind == "pr" and action == "view":
    data = {{"state": "MERGED" if p.exists() else "OPEN", "headRefOid": head}}
elif kind == "api" and action == "graphql":
    pull = {{"mergeQueueEntry": None, "isDraft": False, "baseRefName": "trunk",
        "state": "OPEN", "author": {{"login": "owner"}}, "isCrossRepository": False,
        "labels": {{"nodes": [], "pageInfo": {{"hasNextPage": False}}}},
        "reviewThreads": {{"nodes": [], "pageInfo": {{"hasNextPage": False}}}},
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": None,
        "timelineItems": {{"nodes": [{{"reason": "failed_checks", "createdAt": "2026-01-01T00:00:00Z"}}]}}}}
    data = {{"data": {{"repository": {{"pullRequest": dict(pull, headRefOid=head)}}}}}}
elif kind == "run" and action == "list":
    data = [{{"databaseId": 10, "headBranch": "gh-readonly-queue/trunk/pr-7-a"}}]
elif kind == "run" and action == "view":
    data = {{"jobs":[{{"name":"test", "conclusion":"cancelled"}}]}}
elif kind == "pr" and action == "checks":
    data = [{{"name":"required", "bucket":"pass"}}]
elif kind == "pr" and action == "merge":
    p.write_text(rest[rest.index("--match-head-commit") + 1])
    data = {{}}
else:
    raise SystemExit(f"unexpected fake gh invocation: {{sys.argv}}")
print(json.dumps(data))
"""
        )
        gh.chmod(0o700)
        source = """
import sys
from unittest.mock import patch
from pathlib import Path
import runtime
runtime.P = runtime.Project('gadgets', config_file=Path(sys.argv[1]), data_dir=Path(sys.argv[2]))
runtime.P.prepare()
import train
hold = runtime.P.data / "locks/hold"
hold.touch()
def clock(_):
    hold.unlink(missing_ok=True)
with patch.object(train.time, "sleep", side_effect=clock):
    print(train.wait_merged_or_removed(7, "expected", "2026-01-01T00:00:00Z"))
"""
        for head in ("expected", "changed"):
            result = subprocess.run(
                [sys.executable, "-c", source, str(self.config), str(self.data)],
                cwd=TOOL,
                env={
                    **self.env,
                    "TEST_PR_HEAD": head,
                    "PATH": f"{self.root}{os.pathsep}{self.env.get('PATH', os.defpath)}",
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            if head == "expected":
                self.assertEqual(state.read_text(), "expected")
                self.assertEqual(result.stdout.strip(), "True")
                state.unlink()
            else:
                self.assertFalse(state.exists())
                self.assertEqual(result.stdout.strip(), "False")
                rows = json.loads((self.data / "agent_questions.json").read_text())
                self.assertIn("could not safely enqueue", rows[0]["question"])

    def test_source_conflict_aborts_and_routes_to_coordinator(self):
        remote, head = self.create_git("source.txt")
        self.assertEqual(self.refresh(), "None")
        self.assertFalse((self.checkout / ".git/MERGE_HEAD").exists())
        self.assertEqual(
            self.git("--git-dir", str(remote), "rev-parse", "feature"), head
        )
        question = json.loads((self.data / "agent_questions.json").read_text())[0]
        self.assertIn("source.txt", question["question"])

    def test_collects_configured_local_junit_without_network_or_test_execution(self):
        worktree = self.root / "rt-pr-7"
        out = worktree / "custom-reports"
        out.mkdir(parents=True)
        (out / "suite.xml").write_text(
            '<testsuite><testcase classname="tests.test_example" name="test_one" time="1"/></testsuite>'
        )
        with self.config.open("a") as f:
            f.write(f"""
[hosts.local.junit]
rt_worktrees = [{json.dumps(str(worktree))}]
rt_xml = "custom-reports/*.xml"
""")
        self.command("reports", "--local-only")
        evidence = json.loads((self.data / "tests_inventory_reports.json").read_text())
        (report,) = evidence["reports"]
        self.assertEqual(
            (report["source"], report["lane"], report["sha"]),
            ("local", "remote test", ""),
        )
        self.assertEqual(
            report["tests"][0][:4], ["tests/test_example.py", "test_one", "pass", 1.0]
        )


if __name__ == "__main__":
    unittest.main()
