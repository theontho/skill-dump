"""Small regression checks for coordinator test-inventory evidence."""

import unittest
from typing import Any

from tests_metrics import evidence_summary, jobs_summary, recommendations
from tests_policy import family, stage
from tests_reports import cases


class InventoryTest(unittest.TestCase):
    def test_stage_distinguishes_base_push_from_feature_branch(self) -> None:
        self.assertEqual(stage("checks.yml", "push", "main"), "main")
        self.assertEqual(stage("checks.yml", "push", "feature"), "other")
        self.assertEqual(stage("checks.yml", "pull_request", "feature"), "ready")

    def test_only_real_testcases_enter_pass_rate(self) -> None:
        report = b"""<testsuites><testsuite>
        <testcase classname="tests.foo.test_native.Example" name="test_a[one]" time="2.5"/>
        <testcase classname="tests.foo.test_native.Example" name="test_a[two]" time="NaN"><skipped/></testcase>
        <testcase classname="tests.test_native" name="test_b" time="1"><error message="No space left on device"/></testcase>
        <testcase classname="" name="tests/test_collection_failure.py"><error message="collection failed"/></testcase>
        </testsuite></testsuites>"""
        parsed = cases(report)
        self.assertEqual(
            parsed[0][:5],
            ["tests/foo/test_native.py", "Example::test_a[one]", "pass", 2.5, False],
        )
        self.assertEqual(parsed[1][2:5], ["skip", 0, False])
        self.assertEqual(parsed[2][2:5], ["fail", 1, True])
        self.assertEqual(len(parsed), 3)
        with self.assertRaises(ValueError):
            cases(b'<!DOCTYPE docs [<!ENTITY x "bad">]><docs/>')

    def test_shards_count_one_attempt_and_unchanged_recovery(self) -> None:
        def job(
            ident: int, label: str, state: str, minute: int, end: int
        ) -> dict[str, Any]:
            return {
                "id": ident,
                "name": f"windows / test-windows / {label}",
                "conclusion": state,
                "runner_name": "runner" if state != "skipped" else "",
                "started_at": f"2026-11-24T00:{minute:02d}:00Z",
                "completed_at": f"2026-11-24T00:{end:02d}:00Z",
            }

        run = {
            "id": 1,
            "path": ".github/workflows/pr_ci.yml",
            "event": "merge_group",
            "head_branch": "gh-readonly-queue/main/pr-1500",
            "head_sha": "abc",
            "created_at": "2026-11-24T00:00:00Z",
            "status": "completed",
            "conclusion": "success",
            "attempts": {
                "1": [
                    job(10, "all", "failure", 1, 5),
                    job(11, "shard-2", "success", 2, 6),
                ],
                "2": [
                    job(12, "all", "success", 20, 24),
                    job(13, "shard-2", "skipped", 20, 20),
                ],
            },
        }
        result, stats = jobs_summary({"runs": [run]}, fetch_annotations=False)
        (row,) = result
        self.assertEqual(row["name"], family("windows / test-windows / all"))
        self.assertEqual(row["outcomes"], {"failure": 1, "success": 1})
        self.assertEqual(row["signals"], {"flaky": 1})
        self.assertEqual((row["wall"], row["minutes"], row["hours"]), (4.5, 6, 0.2))
        self.assertEqual(stats["records"], 1)

    def test_nightly_move_requires_pass_history_and_overlap(self) -> None:
        tests = [
            {"premerge": 120, "outcomes": {"pass": 50}, "name": "measured overlap"},
            {
                "premerge": 90,
                "outcomes": {"pass": 20, "fail": 1},
                "name": "caught something",
            },
            {"premerge": 0, "outcomes": {"pass": 100}, "name": "no nightly overlap"},
        ]
        candidates = recommendations([], {"tests": tests})
        self.assertEqual(
            [test["name"] for test in candidates["move"]], ["measured overlap"]
        )
        self.assertEqual(candidates["move_hours"], 0.033)

    def test_testcase_classification_requires_correct_os_sha_and_pr(self) -> None:
        file = "tests/test_ci_changes.py"

        def report(
            ident: int, sha: str, tests: list[list[Any]], lane: str = "ubuntu"
        ) -> dict[str, Any]:
            return {
                "id": str(ident),
                "sha": sha,
                "pr": 1544,
                "lane": lane,
                "at": f"2026-11-24T00:0{ident}:00Z",
                "source": "GitHub Actions",
                "stage": "ready",
                "tests": [[file, *case] for case in tests],
            }

        evidence = evidence_summary(
            {
                "reports": [
                    report(
                        1,
                        "before",
                        [
                            ["test_flaky", "fail", 1, False, "message"],
                            ["test_fix", "fail", 1, False, "message"],
                            ["test_wrong_platform", "fail", 1, False, "message"],
                            ["test_environment", "fail", 1, True, "disk"],
                        ],
                    ),
                    report(2, "before", [["test_flaky", "pass", 2, False, ""]]),
                    report(3, "after", [["test_fix", "pass", 2, False, ""]]),
                    report(
                        4,
                        "before",
                        [["test_wrong_platform", "pass", 2, False, ""]],
                        "macos",
                    ),
                ]
            },
            [],
        )
        tests = {row["name"]: row for row in evidence["tests"]}
        self.assertEqual(tests["test_flaky"]["signals"], {"flaky": 1})
        self.assertEqual(tests["test_fix"]["pr_ids"], [1544])
        self.assertEqual(tests["test_wrong_platform"]["signals"], {"unknown": 1})
        self.assertEqual(tests["test_environment"]["signals"], {"infra": 1})


if __name__ == "__main__":
    unittest.main()
