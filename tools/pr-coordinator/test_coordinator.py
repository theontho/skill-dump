"""Exercise the CLI, migration and train against isolated config, Git and gh."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
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

    def command(self, *args, check=True):
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
