"""Exercise the CLI, migration and train against isolated config, Git and gh."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
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

    def argv(self, *args):
        return [
            sys.executable,
            str(TOOL / "pr-coord"),
            "--project",
            "gadgets",
            "--config",
            str(self.config),
            "--data-dir",
            str(self.data),
            *args,
        ]

    def command(self, *args, check=True, timeout=None):
        return subprocess.run(
            self.argv(*args),
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def background(self, *args):
        process = subprocess.Popen(
            self.argv(*args),
            cwd=self.root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def stop():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=15)

        self.addCleanup(stop)
        return process

    def fake_gh(self, body="exit 0\n"):
        fake_gh = self.root / "gh"
        fake_gh.write_text(f"#!/bin/sh\n{body}")
        fake_gh.chmod(0o755)
        self.env["PATH"] = f"{self.root}{os.pathsep}{self.env['PATH']}"

    def wait_until(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("background operation did not become ready")

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
        self.command(
            "ask",
            "agent question",
            "--pr",
            "#5",
            "--machine",
            "worker",
            "--default",
            "continue elsewhere",
            "--blocks",
            "publishing the release",
            timeout=5,
        )
        question = json.loads(self.command("agent-questions").stdout)[0]
        self.assertEqual(question["default"], "continue elsewhere")
        self.assertEqual(question["blocks"], "publishing the release")
        self.assertFalse(question.get("answer"))
        self.assertTrue(question["asked"])
        id_ = question["id"]
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

    def test_nearest_tests_uses_the_worktree_and_relays_selector_errors(self):
        missing = self.command("nearest-tests", "file.py", check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("tests.nearest_command", missing.stderr)

        worktree = self.root / "implementer"
        nested = worktree / "src"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)
        (worktree / "selector.py").write_text(
            "import json, sys\n"
            "if 'broken' in sys.argv[1:]: raise SystemExit(4)\n"
            "print(json.dumps(sys.argv[1:]))\n"
        )
        with self.config.open("a") as config:
            config.write(
                f"\n[tests]\nnearest_command = "
                f"{json.dumps([sys.executable, 'selector.py'])}\n"
            )
        for paths, code in (
            (["src/with spaces.py", "src/gone.py"], 0),
            (["broken"], 4),
        ):
            run = subprocess.run(
                self.argv("nearest-tests", *paths),
                cwd=nested,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(run.returncode, code, run.stderr)
            if code == 0:
                self.assertEqual(json.loads(run.stdout), paths)

    def test_review_rounds_ignore_duplicate_heads_and_escalate_once(self):
        from dashboard import review_panel
        from lifecycle_model import review_rounds, weekly_reviews

        def review(
            head, *, login="copilot-pull-request-reviewer[bot]", state="COMMENTED"
        ):
            return {
                "__typename": "PullRequestReview",
                "commit": {"oid": head},
                "author": {"login": login},
                "state": state,
                "submittedAt": "2026-11-24T10:00:00Z" if state != "PENDING" else None,
            }

        timeline = [
            review("a"),
            review("b"),
            review("a"),
            review("c", login="human-copilot-fan"),
            review("c", state="PENDING"),
        ]
        pattern = re.compile(r"copilot-pull-request-reviewer\[bot\]")
        self.data.mkdir()
        self.fake_gh()

        def collect(closed=False):
            count = review_rounds(timeline, pattern)
            rows = [{"pr": 26, "rounds": count, "merged": None}]
            if closed:
                rows.append({"pr": 28, "rounds": count, "merged": 1000000})
            (self.data / "lifecycle.json").write_text(
                json.dumps({"review_rounds": rows})
            )
            self.command("event-collect", "--once", timeout=5)
            return json.loads((self.data / "events_journal.json").read_text())["events"]

        self.assertEqual(collect(), [])
        timeline.append(review("c"))
        entries = collect(closed=True)
        self.assertEqual(len(entries), 1)
        self.assertIn("PR #26 has 3 Copilot review rounds", entries[0]["message"])
        timeline.append(review("d"))
        self.assertEqual(collect(), entries)
        weekly = weekly_reviews(
            [
                {"merged": 1000000, "rounds": review_rounds(timeline, pattern)},
                {"merged": 999999, "rounds": 0},
                {"merged": 100000, "rounds": 9},
                {"merged": None, "rounds": 9},
            ],
            1000000,
        )
        panel = review_panel(
            {
                "weekly_reviews": weekly,
                "review_rounds": [{"pr": 26, "rounds": 4, "merged": None}],
            }
        )
        self.assertIn("p90 4 rounds; 1/2 (50%)", panel)
        self.assertIn("PR #26</a>: 4 Copilot review rounds", panel)

    def test_dashboard_keeps_question_line_breaks(self):
        import dashboard

        questions = self.root / "rendered-questions.json"
        questions.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "question": "Intro\n- #1 <go>\n- #2 hold",
                        "default": "hold",
                        "asked": "2026-11-24T10:00:00Z",
                        "blocks": "<release>",
                    },
                    {"id": 2, "question": "Done?\nyes", "answer": "ok\nthanks"},
                ]
            )
        )
        with (
            mock.patch.object(dashboard, "QUESTIONS", questions),
            mock.patch.object(dashboard, "STATE", self.root / "missing-state.json"),
            mock.patch.object(dashboard.time, "time", return_value=1795518000),
        ):
            html = dashboard.page()
        self.assertIn("<div class=qtext>Intro\n- #1 &lt;go&gt;\n- #2 hold</div>", html)
        self.assertIn("Open 1h 0m", html)
        self.assertIn("Blocks: &lt;release&gt;", html)
        self.assertIn("<div class=qtext>Done?\nyes</div>", html)
        self.assertIn("white-space:pre-wrap", html)
        # An answer being typed must survive the periodic refresh.
        self.assertNotIn("http-equiv=refresh", html)
        self.assertIn("localStorage", html)
        self.assertIn("document.activeElement", html)

    def test_run_records_events_without_waiters_and_cursors_survive_restarts(self):
        import dashboard

        self.fake_gh()
        self.checkout.mkdir()
        self.env["GIT_CEILING_DIRECTORIES"] = str(self.root)
        with self.config.open("a") as config:
            config.write("\n[events]\ninterval = 1\n")
        project = Project("gadgets", config_file=self.config, data_dir=self.data)
        service = self.background("run", "--port", "0")

        def page():
            with mock.patch.object(dashboard, "P", project):
                return dashboard.page()

        def take(*args):
            result = self.command("events", "--wait", "--json", *args, timeout=5)
            return json.loads(result.stdout)

        self.command("ask", "first <alert>", "--machine", "worker")
        self.wait_until(lambda: "first &lt;alert&gt;" in page())
        self.assertIn("last consumed never · 1 pending", page())
        first = take()
        self.assertEqual(len(first), 1)
        self.assertIn("first <alert>", first[0]["message"])
        self.assertRegex(page(), r"last consumed \d{4}-\d{2}-\d{2}T[^<]+ · 0 pending")

        waiter = self.background("events", "--wait")

        def waiting():
            lock = self.data / "locks/events-wait-supervisor.lock"
            try:
                with lock.open() as file:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except FileNotFoundError:
                pass
            return False

        self.wait_until(waiting)
        duplicate = self.command("events", "--wait", check=False, timeout=5)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("already has a waiter", duplicate.stderr)
        self.assertEqual(duplicate.stdout, "")
        waiter.kill()
        self.assertEqual(waiter.communicate(timeout=5)[0], "")
        service.terminate()
        service.communicate(timeout=15)

        self.command("ask", "second alert", "--machine", "worker")
        self.background("run", "--port", "0")
        second = take()
        self.assertEqual(len(second), 1)
        self.assertIn("second alert", second[0]["message"])
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(take("--consumer", "audit"), first + second)

    def test_event_cutover_keeps_legacy_seen_and_known_checks_and_retries_failed_output(self):
        self.fake_gh(
            """case "$2" in
  repos/example/gadgets/commits/trunk) printf 'sha\\n' ;;
  *check-runs*) printf 'already seen\\tURL\\nknown check\\tURL\\nnew check\\tURL\\n' ;;
esac
"""
        )
        self.data.mkdir()
        seen = self.data / "events_seen.json"
        known = self.data / "events_known.json"
        seen.write_text('["main:sha:already seen"]')
        known.write_text('["known check"]')
        self.command("agent", "supervisor", "heartbeat")
        self.command("event-collect", "--once")
        # With no reader, writing the batch must fail without consuming it.
        reader, writer = os.pipe()
        os.close(reader)
        with os.fdopen(writer, "w") as output, subprocess.Popen(
            self.argv("events", "--wait"),
            cwd=self.root,
            env=self.env,
            stdout=output,
            stderr=subprocess.PIPE,
        ) as process:
            self.assertNotEqual(process.wait(timeout=5), 0)
            self.assertIn(b"Broken pipe", process.stderr.read())

        result = self.command("events", "1", timeout=5)
        self.assertIn("'new check'", result.stdout)
        self.assertNotIn("'known check'", result.stdout)
        self.assertNotIn("'already seen'", result.stdout)
        self.assertEqual(json.loads(seen.read_text()), ["main:sha:already seen"])
        self.assertEqual(json.loads(known.read_text()), ["known check"])

    def age_supervisor_heartbeat(self, *, minutes):
        path = self.data / "events_journal.json"
        journal = json.loads(path.read_text())
        journal["supervisor"]["last_heartbeat"] = (
            datetime.now(UTC) - timedelta(minutes=minutes)
        ).isoformat(timespec="seconds")
        path.write_text(json.dumps(journal))

    def test_supervisor_idle_waiter_and_dashboard_alert_once_until_heartbeat(self):
        import dashboard

        self.checkout.mkdir()
        self.env["GIT_CEILING_DIRECTORIES"] = str(self.root)
        with self.config.open("a") as config:
            config.write("""
[events]
interval = 1
supervisor_idle_minutes = 1
queue_search = "label:efficiency"
[hosts.worker]
display = "Worker"
process_probe = "printf 'load\\\\n'"
""")
        self.fake_gh("""
if [ "$1" = issue ]; then
  case " $* " in
    *' --repo example/gadgets '*' --search label:efficiency '*)
      printf '[{"number":42,"state":"OPEN","labels":[]}]\\n' ;;
    *) exit 1 ;;
  esac
fi
""")
        self.command("agent", "supervisor", "heartbeat")
        self.age_supervisor_heartbeat(minutes=2)
        self.command("hosts")
        self.background("run", "--port", "0")
        (alert,) = json.loads(
            self.command("events", "--wait", "--json", timeout=10).stdout
        )
        self.assertIn("SUPERVISOR-IDLE:", alert["message"])
        self.assertIn("Worker", alert["message"])
        self.assertIn("#42", alert["message"])

        project = Project("gadgets", config_file=self.config, data_dir=self.data)
        with mock.patch.object(dashboard, "P", project):
            self.assertIn("SUPERVISOR-IDLE:", dashboard.events_panel())
        # Reading the alert isn't a heartbeat; intervening batches have no repeat.
        self.command("ask", "another event", "--machine", "worker")
        (other,) = json.loads(
            self.command("events", "--wait", "--json", timeout=10).stdout
        )
        self.assertIn("another event", other["message"])

    def test_supervisor_idle_requires_threshold_reachable_capacity_and_eligible_issue(
        self,
    ):
        from runtime import save

        self.idle_project()
        self.command("agent", "supervisor", "heartbeat")
        state = {
            "hosts_observed_at": time.time(),
            "hosts": {"worker": {"reachable": True}},
        }
        save(self.data / "state.json", state)
        self.fake_gh("""
if [ "$1" = issue ]; then
  printf '[{"number":42,"state":"OPEN","labels":[]}]\\n'
fi
""")

        def collect_messages():
            self.command("event-collect", "--once")
            return [
                row["message"]
                for row in json.loads((self.data / "events_journal.json").read_text())[
                    "events"
                ]
            ]

        self.age_supervisor_heartbeat(minutes=29)
        self.assertEqual(collect_messages(), [])
        self.age_supervisor_heartbeat(minutes=30)
        state["hosts"]["worker"]["leases"] = [{"state": "held"}]
        save(self.data / "state.json", state)
        self.assertEqual(collect_messages(), [])
        state["hosts"]["worker"]["leases"] = []
        state["hosts"]["worker"]["reachable"] = False
        save(self.data / "state.json", state)
        self.assertEqual(collect_messages(), [])
        state["hosts"]["worker"]["reachable"] = True
        save(self.data / "state.json", state)
        self.fake_gh("""
if [ "$1" = issue ]; then
  printf '[{"number":1,"state":"OPEN","labels":[{"name":"check-later"}]},
    {"number":2,"state":"OPEN","labels":[{"name":"research-done"}]},
    {"number":3,"state":"OPEN","labels":[{"name":"hands-off"}]},
    {"number":4,"state":"OPEN","labels":[{"name":"Machine-Other"}]},
    {"number":5,"state":"CLOSED","labels":[]}]\\n'
fi
""")
        self.assertEqual(collect_messages(), [])
        self.fake_gh("exit 1\n")
        self.assertEqual(collect_messages(), [])
        self.fake_gh("""
if [ "$1" = issue ]; then
  printf '[{"number":42,"state":"OPEN","labels":[]}]\\n'
fi
""")
        self.assertIn("#42", collect_messages()[0])
        self.command("agent", "supervisor", "heartbeat")
        self.assertEqual(len(collect_messages()), 1)
        self.age_supervisor_heartbeat(minutes=31)
        self.assertEqual(len(collect_messages()), 2)

    def test_owner_resume_records_long_gap_without_reading_chat(self):
        import dashboard

        self.command("agent", "supervisor", "heartbeat", "--owner-resume")
        self.age_supervisor_heartbeat(minutes=31)
        self.command("agent", "supervisor", "heartbeat", "--owner-resume")
        self.command("agent", "supervisor", "heartbeat", "--owner-resume")
        (incident,) = json.loads(
            self.command("events", "--wait", "--json", timeout=5).stdout
        )
        self.assertIn("OWNER-RESUME:", incident["message"])
        self.assertIn("31 min", incident["message"])
        project = Project("gadgets", config_file=self.config, data_dir=self.data)
        with mock.patch.object(dashboard, "P", project):
            self.assertIn("Owner-resume incidents: 1", dashboard.events_panel())

    def test_event_wait_collects_if_the_service_watcher_is_missing(self):
        self.fake_gh()
        self.command("ask", "handoff without a watcher", "--machine", "worker")
        result = self.command("events", "--wait", "--json", timeout=5)
        self.assertIn("no collector is running", result.stderr)
        self.assertIn(
            "handoff without a watcher", json.loads(result.stdout)[0]["message"]
        )

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

    def test_azure_budget_advises_from_current_credit_without_network(self):
        import azure_budget
        import dashboard
        from events import idle_hosts

        with self.config.open("a") as config:
            config.write("""
[azure_budget]
subscription = "00000000-0000-0000-0000-000000000001"
monthly_credit_usd = 150
expected_burn_usd_per_day = 10
""")
        project = self.idle_project()
        self.data.mkdir()
        fixture = TOOL / "fixtures" / "azure-budget"
        periods = json.loads((fixture / "billing-periods.json").read_text())
        cost = json.loads((fixture / "cost-query.json").read_text())
        now = datetime(2026, 11, 27, 16, tzinfo=UTC)
        with mock.patch.object(
            azure_budget, "azure_request", side_effect=[periods, cost]
        ):
            azure_budget.refresh(project, azure_budget.settings(project), now)
        value = azure_budget.status(project, now=now)
        self.assertAlmostEqual(value["remaining_usd"], 95.88)
        self.assertEqual(value["reset_date"], "2026-12-06")
        self.assertAlmostEqual(value["use_it_score"], 95.88 / 90)
        observed_at = now.timestamp()
        state = {
            "hosts_observed_at": observed_at,
            "hosts": {"worker": {"reachable": True}},
            "host_idle": {"worker": {"since": observed_at - 3600, "id": "test"}},
        }
        with mock.patch.object(
            azure_budget, "azure_request", side_effect=AssertionError("not offline")
        ):
            _, message = idle_hosts(state, project, now=observed_at)[0]
            self.assertIn("$95.88", message)
            self.assertIn("prefer Spot", message)
            with (
                mock.patch.object(dashboard, "P", project),
                mock.patch.object(azure_budget, "datetime", wraps=datetime) as clock,
            ):
                clock.now.return_value = now
                self.assertIn("estimated $95.88 credit left", dashboard.page())
            self.assertFalse(
                azure_budget.suggestion(project, now=now + timedelta(days=2))
            )
            self.assertFalse(
                azure_budget.suggestion(project, now=now - timedelta(days=1))
            )

    def test_azure_budget_failed_refresh_keeps_cache_and_default_is_offline(self):
        with self.config.open("a") as config:
            config.write("""
[azure_budget]
subscription = "00000000-0000-0000-0000-000000000001"
monthly_credit_usd = 150
expected_burn_usd_per_day = 10
""")
        self.data.mkdir()
        now = datetime.now(UTC)
        cache = self.data / "azure-budget.json"
        original = json.dumps(
            {
                "subscription": "00000000-0000-0000-0000-000000000001",
                "monthly_credit_usd": 150,
                "remaining_usd": 100,
                "reset_date": (now + timedelta(days=4)).date().isoformat(),
                "fetched_at": now.isoformat(),
            }
        )
        cache.write_text(original)
        marker = self.root / "azure-called"
        fake = self.root / "az"
        fake.write_text("#!/bin/sh\ntouch azure-called\nexit 1\n")
        fake.chmod(0o755)
        self.env["PATH"] = f"{self.root}{os.pathsep}{self.env['PATH']}"
        value = json.loads(self.command("azure-budget", "--json").stdout)
        self.assertEqual(value["remaining_usd"], 100)
        self.assertFalse(marker.exists())
        failed = self.command("azure-budget", "--refresh", check=False)
        self.assertEqual(failed.returncode, 1)
        self.assertTrue(marker.exists())
        self.assertEqual(cache.read_text(), original)

    def test_idle_event_uses_continuous_reachable_observations(self):
        from events import idle_hosts
        from host_activity import update_idle

        project = self.idle_project()
        cases = [
            ("idle", {}, {"reachable": True}, True),
            ("unreachable", {}, {"reachable": False}, False),
            ("activity probe failed", {}, {"reachable": True, "activity_known": False}, False),
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
        self.fake_gh()
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

    def test_registered_detached_driver_prevents_idle_even_without_cpu_use(self):
        from collect_hosts import leases
        from events import idle_hosts
        from host_activity import update_idle

        driver = {"id": "L8", "agent": "driver", "label": "unattended",
                  "run_unit": "driver.service", "cores": 1, "mem": 512 * 1024**2}
        project = self.idle_project(threshold_override=1)
        for field, details in [("leases", {"missing_since": 123}), ("queue", {})]:
            with self.subTest(field=field):
                with mock.patch("collect_hosts.run", return_value=json.dumps({
                    "leases": [], "queue": [], field: [{**driver, **details}],
                    "busy_cores": 0,
                })):
                    observation = leases(["ssh", "worker-alias"])
                state = {"hosts": {"worker": observation}}
                for now in (0, 60, 120):
                    update_idle(state, project, observed_at=now)
                    self.assertFalse(idle_hosts(state, project, now=now))
                state["hosts"]["worker"] = {"reachable": True}
                update_idle(state, project, observed_at=180)
                update_idle(state, project, observed_at=240)
                self.assertTrue(idle_hosts(state, project, now=240))

    def test_certification_waits_when_the_controller_finds_the_host_busy(self):
        import contextlib

        import remote

        calls, states = [], []
        (self.root / "locks").mkdir()
        always_busy = False

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
            busy = always_busy or len(calls) == 1
            stdout.write("No available certification host\n" if busy else "ok\n")
            stdout.flush()
            return subprocess.CompletedProcess(argv, 1 if busy else 0)

        def fake_record(kind, *args):
            if kind == "cert-state":
                states.append(args[1])

        with (
            mock.patch.object(remote, "P", FakeProject()),
            mock.patch.object(
                remote, "locked", lambda *a, **k: contextlib.nullcontext()
            ),
            mock.patch.object(remote, "run", fake_run),
            mock.patch.object(remote, "record", fake_record),
            mock.patch.object(remote, "json_gh", lambda *a: {"headRefOid": "a" * 40}),
            mock.patch.object(remote, "cert_args", lambda *a: {}),
            mock.patch.object(remote, "agent_coord_status", return_value={}),
            mock.patch.object(remote.time, "sleep"),
        ):
            code = remote.certify(self.root, 7, "generic", "a" * 40, "b" * 40)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 2)
            self.assertNotIn("error", states)
            self.assertEqual(states[-1], "pass")

            # #27: a controller that reports busy repeatedly must not provision a third time.
            always_busy = True
            self.assertEqual(
                remote.certify(self.root, 7, "generic", "a" * 40, "b" * 40), 1
            )
            with self.assertRaisesRegex(
                RuntimeError, "third identical attempt refused"
            ) as refused:
                remote.certify(self.root, 7, "generic", "a" * 40, "b" * 40)
            self.assertEqual(len(calls), 3)
            logs = sorted((self.root / "logs").glob("retry-cert-*.log"))
            self.assertEqual(len(logs), 2)
            for log in logs:
                self.assertIn(str(log), str(refused.exception))
                self.assertIn("busy:", log.read_text())

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
            remote, "agent_coord_status", return_value={}
        ), mock.patch.object(
            remote.time, "sleep", side_effect=AssertionError("waited for an offline host")
        ):
            self.assertEqual(remote.certify(self.root, 7, "macos", "a" * 40, "b" * 40), 0)
        self.assertEqual(states[-1], "skipped")
        self.assertEqual(ran, [])

    def test_certification_filters_reservations_before_remote_provisioning(self):
        import remote
        import runtime

        sha = "a" * 40
        manual = {"locks": {"mac-alias": {"reason": "whole-genome run"}}}
        short = {"locks": {"mac-alias": {"reason": "remote certification"}}}
        long = {
            "leases": [
                {"locks": ["mac-alias"], "label": "remote certification", "expect": 7200}
            ]
        }
        worker = {
            "leases": [{"locks": ["worker-cert"], "label": "benchmark", "expect": 300}]
        }
        cases = [
            ("optional", [manual], {}, False, None),
            ("long", [long], {}, False, None),
            ("required", [manual, manual, {}], {}, True, "mac"),
            ("alternative", [manual], {}, False, "spare"),
            ("transient", [short, {}], {}, False, "mac"),
            ("worker", [{}], worker, False, None),
        ]
        for name, controller, worker_status, required, prepared in cases:
            with self.subTest(name):
                slots = ["mac", "spare"] if prepared == "spare" else ["mac"]
                self.config.write_text(f"""
[certification]
must_run = {json.dumps(["macos"] if required else [])}
remote_prepare = ["prepare"]
remote_command = ["certify"]
[certification.slots]
macos = {json.dumps(slots)}
[hosts.mac]
ssh = "mac-alias"
agent_coord = true
cert_worker_lock = "worker-cert"
[hosts.spare]
ssh = "spare"
""")
                project = Project("test", config_file=self.config, data_dir=self.data)
                project.prepare()
                snapshots = iter(controller)
                states, events = [], []

                def read_controller(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, json.dumps(next(snapshots)))

                def fake_remote(host, argv, **kwargs):
                    if argv[0] == "agent-coord":
                        return subprocess.CompletedProcess(argv, 0, json.dumps(worker_status))
                    events.append((host, argv[0]))

                def fake_run(argv, **kwargs):
                    events.append(("controller", argv[0]))
                    return subprocess.CompletedProcess(argv, 0)

                def fake_record(kind, *args):
                    if kind == "cert-state":
                        states.append(args[1])

                with (
                    mock.patch.object(remote, "P", project),
                    mock.patch.object(runtime, "P", project),
                    mock.patch.object(project, "reachable", return_value=True),
                    mock.patch.object(runtime.subprocess, "run", side_effect=read_controller),
                    mock.patch.object(remote, "remote", side_effect=fake_remote),
                    mock.patch.object(remote, "run", side_effect=fake_run),
                    mock.patch.object(remote, "json_gh", return_value={"headRefOid": sha}),
                    mock.patch.object(remote, "record", side_effect=fake_record),
                    mock.patch.object(
                        remote.time, "sleep", side_effect=lambda _: events.append(("sleep", ""))
                    ),
                ):
                    self.assertEqual(remote.certify(self.root, 7, "macos", sha, "b" * 40), 0)
                self.assertEqual(
                    events,
                    [("sleep", "")] * (len(controller) - 1)
                    + ([(prepared, "prepare"), ("controller", "certify")] if prepared else []),
                )
                self.assertEqual(
                    states, ["queued", "running", "pass"] if prepared else ["queued", "skipped"]
                )

    def test_optional_platform_errors_do_not_block_readiness(self):
        import drafts

        class Drafts:
            def get(self, section, key, default=None):
                return ["local-ci/generic"] if key == "required_statuses" else default

        ok = {"local-ci/generic": "success"}
        with mock.patch.object(drafts, "P", Drafts()):
            self.assertTrue(
                drafts.complete((True, {**ok, "local-ci/macos-arm64": "error"}, False))
            )
            self.assertFalse(
                drafts.complete(
                    (True, {**ok, "local-ci/macos-arm64": "failure"}, False)
                )
            )
            self.assertFalse(
                drafts.complete((True, {"local-ci/generic": "error"}, False))
            )
            self.assertFalse(
                drafts.complete(
                    (True, {**ok, "local-ci/windows-x64": "pending"}, False)
                )
            )

    def test_finish_draft_requests_missing_certification_once_per_head(self):
        import drafts

        self.draft_config()
        project = Project("gadgets", config_file=self.config, data_dir=self.data)
        project.prepare()
        head = "a" * 40
        with (
            mock.patch.object(drafts, "P", project),
            mock.patch("runtime.P", project),
            mock.patch.object(drafts, "owner_pr", return_value={"headRefOid": head}),
            mock.patch.object(drafts, "evidence", return_value=(True, {}, False)),
            mock.patch.object(drafts, "request_certification") as request,
            mock.patch.object(drafts.time, "sleep"),
        ):
            with self.assertRaisesRegex(ValueError, "no running certification"):
                drafts.watch(7)
            request.assert_called_once_with(7, head, [])
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
        with (
            (locks / f"cert-pr-7-{old}.lock").open("a") as held,
            mock.patch.object(drafts, "P", Data()),
            mock.patch("runtime.P", Data()),
        ):
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertTrue(drafts.running_certs(7, old))
            self.assertFalse(drafts.running_certs(7, new))

    def fake_draft(self, head, *, statuses, review_head=None):
        state = self.root / "ready"
        requests = self.root / "review-requests"
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
    print(json.dumps({{"number": 7, "headRefName": "feature", "headRefOid": {head!r},
        "baseRefOid": "base", "isCrossRepository": False}}))
elif args[:2] == ["pr", "ready"]:
    state.touch()
elif args[:2] == ["pr", "edit"] and "@copilot" in args:
    with Path({str(requests)!r}).open("a") as log:
        log.write({head!r} + "\\n")
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
                "idle_seconds = 0\npoll_seconds = 0\ncapacity_retry_seconds = 0\n"
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

    def test_watch_draft_requests_copilot_once_for_each_unreviewed_head(self):
        self.draft_config()
        requests = self.root / "review-requests"
        statuses = [
            {"context": "local-ci/generic", "state": "success"},
            {"context": "local-ci/macos", "state": "success"},
        ]
        for head in ("a" * 40, "b" * 40):
            self.fake_draft(head, statuses=statuses, review_head="c" * 40)
            for _ in range(2):
                result = self.command("watch-draft", "7", check=False, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertIn("exact-head review=False", result.stdout)
        self.assertEqual(requests.read_text().splitlines(), ["a" * 40, "b" * 40])

    def test_watch_draft_requests_only_the_missing_required_certification_target(self):
        import drafts

        self.draft_config()
        with self.config.open("a") as config:
            config.write("""
[certification.slots]
generic = ["worker"]
macos = ["worker"]
[hosts.worker]
platform = "macos-arm64"
""")
        project = Project("gadgets", config_file=self.config, data_dir=self.data)
        project.prepare()
        head = "a" * 40
        with (
            mock.patch("runtime.P", project),
            mock.patch.object(drafts, "P", project),
            mock.patch.object(drafts, "owner_pr", return_value={"headRefOid": head}),
            mock.patch.object(
                drafts,
                "evidence",
                return_value=(True, {"local-ci/generic": "success"}, False),
            ),
            mock.patch.object(drafts, "request_certification") as request,
        ):
            with self.assertRaisesRegex(ValueError, "local-ci/generic"):
                drafts.watch(7)
            request.assert_called_once_with(7, head, ["macos"])

    def test_finish_draft_recovers_missing_train_registration_for_ready_pr(self):
        _, head = self.create_git()
        self.git("switch", "trunk")
        self.fake_draft(head, statuses=[]).touch()
        result = self.command("finish-draft", "7")
        self.assertIn("prepared train worktree", result.stdout)
        self.assertIn("handed to the landing train", result.stdout)
        train = self.data / "train.json"
        row = json.loads(train.read_text())[0]
        self.assertEqual(row["head"], head)
        self.assertTrue(Path(row["worktree"]).is_dir())
        self.command("finish-draft", "7")
        self.command("train", "add", "7")
        self.assertEqual(json.loads(train.read_text()), [row])
        train.unlink()
        self.command("train", "add", "7")
        self.assertEqual(json.loads(train.read_text())[0]["worktree"], row["worktree"])

    def test_cert_pr_prepares_only_the_requested_published_head(self):
        _, head = self.create_git("feature.txt")
        self.git("switch", "trunk")
        self.fake_draft(head, statuses=[])
        coord = self.root / "agent-coord"
        coord.write_text("#!/bin/sh\nprintf '{}\\n'\n")
        coord.chmod(0o700)
        with self.config.open("a") as config:
            config.write(f"""
[certification]
default_targets = ["generic"]
local_command = [{json.dumps(sys.executable)}, "-c", "from pathlib import Path; assert Path('feature.txt').exists()"]
[certification.slots]
generic = ["local"]
[hosts.local]
""")
        stale = self.command("cert-pr", "7", "--head", "c" * 40, check=False)
        self.assertIn("head changed", stale.stderr)
        self.assertFalse((self.root / "wt-cert7").exists())
        result = self.command("cert-pr", "7", "--head", head, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prepared cert worktree", result.stdout)
        self.assertTrue((self.root / "wt-cert7" / "feature.txt").is_file())
        cert = json.loads((self.data / "state.json").read_text())["certs"][0]
        self.assertEqual(cert["status"], "pass")

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

    def test_rt_refuses_third_identical_failure_before_preparing_the_worker(self):
        self.create_git()
        self.fake_gh()
        calls = self.root / "remote-calls"
        with self.config.open("a") as config:
            config.write("""
[hosts.worker]
ssh = "worker"
rt = true
checkout = "~/src/gadgets"
[remote_tests]
prepare = ["prepare-worker"]
""")
        for executable, body in {
            "rsync": "exit 0",
            "ssh": f"""printf '%s\\n' "$*" >> {str(calls)!r}
case "$*" in *failing-command) exit 42 ;; *) exit 0 ;; esac""",
        }.items():
            path = self.root / executable
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o700)
        args = ("rt", "worker", str(self.checkout), "--")
        for _ in range(2):
            result = self.command(*args, "failing-command", check=False)
            self.assertEqual(result.returncode, 42, result.stderr)
        before = calls.read_text()
        self.assertEqual(before.count("prepare-worker"), 2)
        result = self.command(*args, "failing-command", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("third identical attempt refused", result.stderr)
        self.assertEqual(calls.read_text(), before)
        logs = sorted((self.data / "logs").glob("retry-rt-*.log"))
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertIn(str(log), result.stderr)
        self.command(*args, "different-command")
        self.assertIn("different-command", calls.read_text())

    def test_train_rechecks_transient_enqueue_rejection_and_parks_after_two(self):
        import landing
        import train

        project = Project("gadgets", config_file=self.config, data_dir=self.data)
        project.prepare()
        head = "a" * 40
        row = {
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": head,
            "baseRefName": "trunk",
            "author": {"login": "owner"},
            "isCrossRepository": False,
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "reviewDecision": None,
            "mergeQueueEntry": None,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        with (
            mock.patch("runtime.P", project),
            mock.patch.object(train, "P", project),
            mock.patch.object(landing, "P", project),
            mock.patch.object(landing, "review", return_value=row) as read,
            mock.patch.object(train, "review", return_value=row),
            mock.patch.object(
                landing, "checks", side_effect=[(False, False, []), (True, False, [])]
            ) as checks,
            mock.patch.object(
                landing, "gh", return_value=subprocess.CompletedProcess([], 0)
            ) as merge,
            mock.patch.object(train, "sh", return_value=(0, "")),
            mock.patch.object(train.time, "sleep") as delay,
        ):
            self.assertTrue(train.enqueue_checked(7, head))
            merge.assert_called_once()
            delay.assert_called_once()

            checks.side_effect = None
            checks.return_value = (False, False, [])
            with self.assertRaisesRegex(
                RuntimeError, "third identical attempt refused"
            ):
                train.enqueue_checked(8, head)
            reads = checks.call_count
            with self.assertRaisesRegex(RuntimeError, "Previous logs:"):
                train.enqueue_checked(8, head)
            self.assertEqual(checks.call_count, reads)

            checks.side_effect = [(False, False, []), (True, False, [])]
            read.side_effect = [row, dict(row, headRefOid="b" * 40)]
            self.assertFalse(train.enqueue_checked(9, head))
            self.assertEqual(merge.call_count, 1)

    def test_train_waits_for_github_to_report_its_own_push(self):
        import train

        reported = iter(["old", "old", "pushed"])
        with (
            mock.patch.object(
                train, "pr_info", lambda n: {"headRefOid": next(reported)}
            ),
            mock.patch.object(train.time, "sleep"),
        ):
            self.assertEqual(train.settled_head(7, "pushed"), "pushed")
        with (
            mock.patch.object(
                train, "pr_info", lambda n: {"headRefOid": "someone-else"}
            ),
            mock.patch.object(train.time, "sleep"),
        ):
            self.assertEqual(train.settled_head(7, "pushed"), "someone-else")

    def test_train_stops_after_two_identical_failures_across_handoffs(self):
        import train

        calls, asked = [], []
        project = Project("gadgets", config_file=self.config, data_dir=self.data)
        project.prepare()

        def flaky(entry):
            calls.append(entry["head"])
            if entry["head"] == "old":
                raise subprocess.CalledProcessError(1, ["gh", "pr", "diff"])

        with (
            mock.patch.object(train, "P", project),
            mock.patch("runtime.P", project),
            mock.patch.object(train, "land", flaky),
            mock.patch.object(train, "need_agent", lambda n, why: asked.append(why)),
            mock.patch.object(train, "sh", return_value=(0, "")),
            mock.patch.object(train.time, "sleep"),
            mock.patch.object(
                train, "pr_info", return_value={"headRefOid": "old"}
            ) as info,
        ):
            entry = {"pr": 7, "worktree": "wt"}
            train.land_safely(entry, attempts=1)
            train.land_safely(entry, attempts=1)
            train.land_safely(entry)
            self.assertEqual(calls, ["old", "old"])
            self.assertIn("third identical attempt refused", asked[-1])
            logs = sorted((self.data / "logs").glob("retry-train-land-*.log"))
            self.assertEqual(len(logs), 2)
            for log in logs:
                self.assertIn(str(log), asked[-1])
            info.return_value = {"headRefOid": "new"}
            train.land_safely(entry)
            self.assertEqual(calls[-1], "new")

    def test_remote_test_status_uses_a_command_the_cli_accepts(self):
        import remote

        calls = []
        with mock.patch.object(remote, "run", lambda argv, **k: calls.append(argv)):
            remote.record("run-state", "rt-1", "done")
        self.assertIn("run-state", calls[0])
        result = self.command(
            "run-state", "rt-1", "running", "--machine", "worker", check=False
        )
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
