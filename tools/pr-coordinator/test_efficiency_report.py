import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from efficiency_github import GitHub, evaluate, stamp
from efficiency_local import certification, json_lines, queue_metrics, session_metrics
from efficiency_report import (
    WEEK,
    bounds,
    dashboard_page,
    markdown,
    publish,
    report_data,
    week_of,
)
from runtime import Project

FIXTURES = Path(__file__).with_name("fixtures") / "efficiency"
START = bounds("2026-W48")


class WeeklyReportTest(unittest.TestCase):
    def test_postmortem_review_push_and_rerun_definitions(self):
        data = json.loads((FIXTURES / "github.json").read_text())
        certs, local, legacy = certification(
            FIXTURES, START, START + WEEK, ["No available certification host"]
        )
        metrics = evaluate(
            {int(key): row for key, row in data["prs"].items()},
            data["runs"],
            lambda run: data["attempts"][str(run["id"])],
            START,
            START + WEEK,
            certs,
        )
        self.assertEqual(metrics["review_rounds_p50"], 2)
        self.assertAlmostEqual(metrics["review_rounds_p90"], 3.6)
        self.assertEqual(metrics["review_prs_at_least_4"], 1)
        self.assertEqual(metrics["pushes_after_ready"], 1)
        self.assertEqual(metrics["rerun_only_passes"], 1)
        self.assertEqual(metrics["superseded_hosted_runs"], 1)
        self.assertEqual(metrics["superseded_certifications"], 1)
        self.assertEqual(local, {"cert_busy_refusals": 1, "cert_attempts": 2})
        self.assertEqual(legacy, 0)

    def test_lease_and_session_evidence_do_not_count_background_wait_twice(self):
        with (FIXTURES / "leases.jsonl").open() as file:
            queue = queue_metrics(
                [json_lines(file)], re.compile(r"project(?:-\d+)?"), START, START + WEEK
            )
        self.assertEqual(queue["lease_queue_hours"], 1.5)
        self.assertEqual(queue["lease_queue_hours_cancelled"], 0.5)
        metrics = session_metrics(
            [("supervisor", FIXTURES / "sessions.jsonl")],
            {"supervisor"},
            START,
            START + WEEK,
            1800,
        )
        self.assertEqual(metrics["supervisor_idle_hours_owner"], 2)
        self.assertEqual(metrics["supervisor_idle_hours_notification"], 1)
        self.assertEqual(metrics["blocking_question_hours"], 0.5)
        self.assertAlmostEqual(metrics["polling_hours"], 120 / 3600)
        self.assertEqual(metrics["polling_hours.bash.gh_watch"], 0)

    def test_report_marks_missing_data_and_partial_week(self):
        project = Project("sample", read_config=False)
        info = report_data(
            project,
            "2026-W48",
            START,
            START + 120,
            {"cert_busy_refusals": 0},
            ["No session evidence."],
            {"cert_busy_refusals": 3},
            [],
            {"cert_busy_refusals": 1},
        )
        self.assertTrue(info["partial_week"])
        self.assertEqual(info["previous_week"], "2026-W47")
        self.assertEqual(info["metrics"]["cert_busy_refusals"]["delta"], -3)
        self.assertIsNone(info["metrics"]["blocking_question_hours"]["value"])
        text = markdown(info)
        self.assertIn("busy/no-host refusals | 0 | 3 | -3 | 1 |", text)
        self.assertIn("Blocking question hours | — |", text)
        self.assertEqual(week_of(bounds("2020-W53") + WEEK), "2021-W01")

    def test_api_window_splits_at_cap_and_reads_remaining_pages(self):
        # Responses mirror Actions' 1,000-result search cap and 100-row pages.
        class RecordedAPI(GitHub):
            def api(self, path):
                query = parse_qs(urlsplit(path).query)
                begin, end = map(stamp, query["created"][0].split(".."))
                if (begin, end) == (START, START + 3):
                    return {"total_count": 1000, "workflow_runs": []}
                if (begin, end) == (START, START + 1):
                    page = int(query.get("page", ["1"])[0])
                    values = range(100) if page == 1 else [100]
                    return {
                        "total_count": 101,
                        "workflow_runs": [{"id": i} for i in values],
                    }
                if (begin, end) == (START + 2, START + 3):
                    return {"total_count": 1, "workflow_runs": [{"id": 101}]}
                raise AssertionError(query)

        api = RecordedAPI(Project(read_config=False))
        rows = api.window("actions/runs", "workflow_runs", "created", START, START + 4)
        self.assertEqual([row["id"] for row in rows], list(range(102)))

    def test_publication_mirrors_formats_and_imports_cloud_aggregates(self):
        with TemporaryDirectory() as path:
            root = Path(path)
            project = Project("sample", data_dir=root / "state", read_config=False)
            project.checkout = root
            project.settings["efficiency"] = {
                "repo_report_dir": "out/efficiency",
                "copilot_dir": str(root / "absent"),
                "targets": {"polling_hours": 1},
            }
            session_file = root / "session-metrics.json"
            session_file.write_text(
                '{"weeks":{"2026-W48":{"polling_hours":2.5},"2026-W47":{"polling_hours":4}}}'
            )
            # Only external collectors are replaced; publication uses the real filesystem.
            with (
                patch.object(
                    GitHub, "collect", return_value={"superseded_certifications": 0}
                ),
                patch("efficiency_report.leases", return_value=({}, [])),
            ):
                publish(project, "2026-W48", session_file=session_file, now=START + 120)
            for suffix in ("md", "json"):
                retained = project.data / "efficiency" / f"2026-W48.{suffix}"
                mirror = root / "out" / "efficiency" / retained.name
                self.assertEqual(retained.read_bytes(), mirror.read_bytes())
            report = json.loads(retained.read_text())
            self.assertEqual(
                report["metrics"]["polling_hours"],
                {"value": 2.5, "previous": 4, "delta": -1.5, "target_max": 1},
            )
            self.assertIn(
                "<td>2.5</td><td>4</td><td>-1.5</td><td>1</td>",
                dashboard_page(project.data),
            )
