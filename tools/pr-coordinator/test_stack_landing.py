"""Replay GitHub's published native stack and merge-async response examples offline."""

import json
import re
import shutil
import subprocess
import unittest
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import dashboard
import landing
import runtime
import stack_landing
import train
import triage

TOOL = Path(__file__).resolve().parent
OUT = TOOL.parents[1] / "out"


class StackLanding(unittest.TestCase):
    def setUp(self):
        root = OUT / f"stack-test-{uuid4().hex}"
        self.addCleanup(shutil.rmtree, root)
        project = runtime.Project(data_dir=root, read_config=False)
        project.prepare()
        project.owner = "octocat"
        self.addCleanup(patch.stopall)
        for module in (landing, stack_landing, train, runtime):
            patch.object(module, "P", project).start()
        for module in (landing, stack_landing):
            patch.object(module, "gh", self.gh).start()
        patch.object(train, "sh", return_value=(0, "")).start()
        patch.object(stack_landing.time, "sleep", self.sleep).start()
        patch.object(
            stack_landing.time, "time", lambda: 1_795_804_200 + self.seconds
        ).start()
        patch.object(stack_landing.time, "monotonic", lambda: self.seconds).start()

        self.project = project
        self.fx = json.loads((TOOL / "fixtures/native-stack.json").read_text())
        self.stack = self.fx["stack"]
        self.pr, self.upper = self.stack["pull_requests"]
        self.head = self.pr["head"]["sha"]
        self.uuid = self.fx["pending"]["details"]["uuid"]
        self.target = 101
        self.final = "enqueued"
        self.membership_code = 200
        self.put_code = 202
        self.get_codes = [200, 200]
        self.retry_after = "7"
        self.ready = True
        self.queue_configured = True
        self.seconds = 0
        self.delays = []
        self.submissions = []
        self.polls = []
        self.terminal = None

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.seconds += seconds

    def review(self, pr):
        n = pr["number"]
        state = (
            "MERGED"
            if pr["merged_at"] or (n == self.target and self.terminal == "merged")
            else pr["state"].upper()
        )
        return {
            "id": str(n),
            "state": state,
            "isDraft": pr["draft"],
            "author": {"login": "octocat"},
            "isCrossRepository": False,
            "baseRefName": pr["base"]["ref"],
            "headRefName": pr["head"]["ref"],
            "headRefOid": pr["head"]["sha"],
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": None,
            "autoMergeRequest": None,
            "mergeQueueEntry": {"position": 1}
            if n == self.target and self.terminal == "enqueued"
            else None,
            "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }

    def gh(self, *args, **kwargs):
        if args[:2] == ("api", "graphql"):
            if "mergeQueue(branch:" in args[3]:
                row = {"mergeQueue": {"id": "queue"} if self.queue_configured else None}
            else:
                n = int(re.search(r"pullRequest\(number:(\d+)\)", args[3])[1])
                row = {
                    "pullRequest": self.review(
                        next(
                            pr
                            for pr in self.stack["pull_requests"]
                            if pr["number"] == n
                        )
                    )
                }
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"data": {"repository": row}}), ""
            )
        if args[:2] == ("pr", "checks"):
            return subprocess.CompletedProcess(
                args,
                not self.ready,
                json.dumps(
                    [{"name": "required", "bucket": "pass" if self.ready else "fail"}]
                ),
                "",
            )
        self.assertIn("--include", args)
        self.assertEqual(args[args.index("--hostname") + 1], "github.com")
        route = args[-1].removeprefix("repos/example/project")
        headers = ""
        if route.startswith("/stacks?pull_request="):
            code, body = self.membership_code, [self.stack]
            if code != 200:
                body = self.fx["unavailable"]
        elif route == "/stacks/42":
            code, body = 200, self.stack
        elif route == "":
            code, body = 200, {"allow_squash_merge": True}
        elif route == f"/pulls/{self.target}/merge-async":
            self.assertEqual(args[args.index("--method") + 1], "PUT")
            fields = dict(
                arg.split("=", 1)                 for flag, arg in pairwise(args) if flag == "-f"
            )
            self.submissions.append((route, fields))
            code, body = self.put_code, self.fx["pending"]
            headers = f"Retry-After: {self.retry_after}\r\n"
        elif route == f"/pulls/{self.target}/merge-async/{self.uuid}":
            self.polls.append(route)
            code = self.get_codes.pop(0)
            if code != 200:
                body = {"message": "Service unavailable"}
            elif self.get_codes:
                body = self.fx["pending"]
            else:
                self.terminal = self.final
                body = self.fx[self.final]
        else:
            self.fail(f"unexpected GitHub call: {args}")
        return subprocess.CompletedProcess(
            args,
            int(code >= 400),
            f"HTTP/2 {code} status\r\n{headers}\r\n{json.dumps(body)}",
            f"gh: HTTP {code}" if code >= 400 else "",
        )

    def record(self):
        [path] = (self.project.data / "logs").glob("stack-merge-*.json")
        return json.loads(path.read_text())

    def test_issue_35_queues_only_through_the_requested_boundary(self):
        self.assertTrue(train.enqueue_checked(101, self.head))
        # Contract: exact head, explicit queue action, no method or upper PR.
        self.assertEqual(
            self.submissions,
            [
                (
                    "/pulls/101/merge-async",
                    {"sha": self.head, "merge_action": "merge_queue"},
                )
            ],
        )
        self.assertEqual(self.delays, [7, 2])
        record = self.record()
        self.assertEqual((record["status"], record["uuid"]), ("enqueued", self.uuid))
        self.assertEqual(record["upper"], [102])

    def test_issue_35_does_not_infer_authorization_for_open_lower_members(self):
        self.target = 102
        self.upper["draft"] = False
        with self.assertRaisesRegex(RuntimeError, r"lower stack members first: #101"):
            train.enqueue_checked(102, self.upper["head"]["sha"])
        self.assertFalse(self.submissions)

    def test_issue_35_can_land_an_upper_member_after_the_lower_one_merged(self):
        self.target = 102
        self.pr.update(state="closed", merged_at="2026-11-27T18:00:00Z")
        self.upper["draft"] = False
        self.head = self.upper["head"]["sha"]
        self.fx["pending"]["details"]["expected_head_sha"] = self.head
        self.assertTrue(train.enqueue_checked(102, self.head))
        self.assertEqual(self.record()["prefix"], [101, 102])

    def test_issue_35_unknown_membership_never_enables_auto_merge(self):
        self.membership_code = 404
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            train.enqueue_checked(101, self.head)
        self.assertFalse(self.submissions)
        self.assertFalse(self.polls)

    def test_issue_35_resumes_matching_conflict_and_rejects_mismatches(self):
        self.put_code = 409
        for field, rejected in (
            ("uuid", "../not-an-operation"),
            ("expected_head_sha", "a" * 40),
            ("merge_action", "direct_merge"),
        ):
            with (
                self.subTest(field=field),
                patch.dict(self.fx["pending"]["details"], {field: rejected}),
            ):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    landing.enqueue_result(101, self.head)
                self.assertFalse(self.polls)
                # Each response is an independent replay against an empty journal.
                for path in (self.project.data / "logs").glob("*.json"):
                    path.unlink()
        self.assertTrue(train.enqueue_checked(101, self.head))
        self.assertEqual(self.record()["uuid"], self.uuid)

    def test_issue_35_times_out_without_polling_early_or_resubmitting(self):
        self.retry_after = "301"
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, f"timed out.*{self.uuid}"):
                landing.enqueue_result(101, self.head)
        self.assertEqual(len(self.submissions), 1)
        self.assertFalse(self.polls)
        self.assertEqual(self.record()["status"], "pending")

    def test_issue_35_records_server_failure_without_resubmitting(self):
        self.final = "failed"
        self.get_codes = [503, 200]
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, f"Merge conflict.*{self.uuid}"):
                landing.enqueue_result(101, self.head)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.record()["status"], "failed")

    def test_issue_35_direct_merge_uses_only_an_allowed_method(self):
        self.queue_configured = False
        self.project.settings["landing"] = {
            "stack_merge_action": "direct_merge",
            "stack_merge_method": "squash",
        }
        self.fx["pending"]["details"].update(
            merge_action="direct_merge", merge_method="squash"
        )
        self.final = "merged"
        self.assertTrue(train.enqueue_checked(101, self.head))
        self.assertEqual(self.submissions[0][1]["merge_method"], "squash")
        self.assertEqual(self.record()["status"], "merged")

    def test_issue_35_refuses_an_action_that_does_not_match_the_ultimate_base(self):
        self.queue_configured = False
        with self.assertRaisesRegex(RuntimeError, "does not match the queue"):
            train.enqueue_checked(101, self.head)
        self.assertFalse(self.submissions)

    def test_issue_35_dashboard_shows_draft_stack_position(self):
        d = self.review(self.upper)
        d.update(
            title="Add user API",
            reviews={"nodes": []},
            closingIssuesReferences={"nodes": []},
            commits={"nodes": [{"commit": {"statusCheckRollup": None}}]},
        )
        body = {"data": {"repository": {"pullRequest": d}}}
        with patch.object(triage, "gh", return_value=json.dumps(body)):
            row = triage.info(102)
        (self.project.data / "triage.jsonl").write_text(json.dumps(row) + "\n")
        state = self.project.data / "state.json"
        state.write_text(
            json.dumps({"prs": [{"number": 102, "stage": "todo", "title": d["title"]}]})
        )
        with (
            patch.object(dashboard, "STATE", state),
            patch.object(dashboard, "HERE", self.project.data),
            patch.object(dashboard, "P", self.project),
        ):
            self.assertIn("stack #42 (2/2)", dashboard.page())
