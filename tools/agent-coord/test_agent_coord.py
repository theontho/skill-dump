"""Tests for agent-coord: python3 -m unittest -v test_agent_coord"""

import json
import math
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if not sys.platform.startswith("linux"):
    raise unittest.SkipTest("the agent-coord service in this checkout requires Linux")

import agent_coord as ac

GIB = 1024**3


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name, "state.json")
        self.vms: list[dict] = []
        self.busy = 0.0
        self.available = 64 * GIB
        self.c = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self):
        return ac.Coordinator(
            self.state,
            measure=lambda: {"vms": list(self.vms), "busy": self.busy,
                             "available": self.available},
            cap=lambda: (8.0, 16 * GIB),
            # No real CPU readings (the test process's own CPU time would leak
            # into admission): tests that need lease use inject it.
            cpu_seconds=lambda pid: None, scope_memory=lambda pid: None,
        )

    def ask(self, cores, mem=GIB, agent="a", pid=None, exclusive=False, locks=(),
            **extra):
        return self.c.handle({
            "op": "acquire", "agent": agent, "pid": pid or os.getpid(),
            "cores": cores, "mem": mem, "label": f"{agent} job",
            "exclusive": exclusive, "locks": list(locks), **extra,
        })  # fmt: skip

    def poll(self, reply):
        return self.c.handle({"op": "poll", "id": reply["id"]})["state"]

    def release(self, reply, **extra):
        return self.c.handle({"op": "release", "id": reply["id"], **extra})

    def history(self):
        return ac.read_history(0, Path(self.tmp.name, "history.jsonl"))

    def age(self, reply, seconds, key="asked"):
        for x in self.c.state["queue"] + self.c.state["leases"]:
            if x["id"] == reply["id"]:
                x[key] -= seconds

    def test_round2_window_starts_when_request_becomes_head(self):
        with patch.object(ac.time, "time", return_value=100000) as clock:
            self.ask(4, mem=8 * GIB, expect=7200)
            first = self.ask(8, mem=12 * GIB, agent="first")
            head = self.ask(6, mem=10 * GIB, agent="head")
            clock.return_value += ac.BACKFILL_PASS_WINDOW + 60
            self.release(first)
            self.assertEqual(self.ask(2, mem=4 * GIB, expect=36000)["state"], "granted")
            self.assertEqual(self.c.state["queue"][0]["id"], head["id"])
            self.assertEqual(self.c.state["queue"][0]["head_since"], clock.return_value)

    def test_round2_tiny_passes_after_capacity_window(self):
        with patch.object(ac.time, "time", return_value=100000) as clock:
            self.ask(4, mem=8 * GIB, expect=7200)
            self.ask(6, mem=10 * GIB, agent="head")
            clock.return_value += ac.BACKFILL_PASS_WINDOW + 1
            self.assertEqual(self.ask(1, expect=36000)["state"], "granted")
            self.assertEqual(self.ask(2, expect=1)["state"], "waiting")

    def test_round2_tiny_passes_during_overrun(self):
        holder = self.ask(4, mem=8 * GIB, expect=600)
        self.ask(8, mem=14 * GIB, agent="head")
        medium = self.ask(3, mem=4 * GIB, agent="medium", expect=60)
        self.age(medium, 61, "granted")
        self.release(holder)
        self.assertEqual(self.ask(2, expect=1)["state"], "waiting")
        self.assertEqual(self.ask(1, expect=36000)["state"], "granted")

    def test_round2_underused_ending_claim_cannot_credit_remaining_holder(self):
        self.configure(cores=12, mem=58)
        a = self.ask(8, agent="A", expect=3600)
        b = self.ask(4, agent="B", expect=36000)
        self.age(a, 200, "granted")
        self.age(b, 70, "granted")
        self.busy = self.c.busy = 6  # A uses 2; B uses 4.
        head = self.ask(8, agent="head")
        self.assertEqual(self.ask(4, mem=2 * GIB, expect=36000)["state"], "waiting")
        self.busy = self.c.busy = 4
        self.release(a)
        self.assertEqual(self.poll(head), "granted")

    def test_round2_explicit_holder_expect_without_history(self):
        with patch.object(ac.time, "time", return_value=100000):
            self.ask(4, mem=8 * GIB, expect=7200)
            self.ask(8, mem=12 * GIB, agent="head")
            self.assertEqual(self.ask(3, mem=4 * GIB, expect=1800)["state"], "granted")
            self.assertEqual(self.c.state["queue"][0]["projected_start"], 107200)

    def test_round2_holder_fallback_uses_history_median(self):
        x = {"id": "old", "agent": "other", "label": "other job", "cores": 1, "mem": GIB}
        for run in (200, 300, 400):
            self.c._log("ended", x, run=run, code=0)
        with patch.object(ac.time, "time", return_value=100000):
            self.assertEqual(self.c._holder_end({**x, "agent": "new", "label": "new job",
                                               "granted": 100000}), 100300)

    def test_round2_past_estimate_holders_can_end_out_of_order(self):
        with patch.object(ac.time, "time", return_value=100000):
            a = self.ask(1, mem=2 * GIB, agent="A", expect=36000)
            b = self.ask(1, mem=8 * GIB, agent="B", expect=600)
            self.age(a, 40000, "granted")
            self.age(b, 1200, "granted")
            head = self.ask(1, mem=12 * GIB, agent="head")
            self.assertEqual(self.ask(1, mem=4 * GIB, expect=36000)["state"], "waiting")
            self.release(b)
            self.assertEqual(self.poll(head), "granted")

    def test_round2_explicit_holders_end_out_of_order(self):
        with patch.object(ac.time, "time", return_value=100000):
            a = self.ask(1, mem=2 * GIB, agent="A", expect=36000)
            b = self.ask(1, mem=8 * GIB, agent="B", expect=600)
            for holder in (a, b):
                self.age(holder, 300, "granted")
            head = self.ask(1, mem=12 * GIB, agent="head")
            self.assertEqual(self.ask(1, mem=4 * GIB, expect=36000)["state"], "waiting")
            self.release(b)
            self.assertEqual(self.poll(head), "granted")

    def test_round2_ticket_reason_uses_current_capacity_without_history_flood(self):
        self.ask(6)
        head = self.ask(4, agent="head")
        self.ask(1, mem=GIB // 2, agent="tiny")
        count = len(self.history())
        for _ in range(3):
            reply = self.c.handle({"op": "poll", "id": head["id"]})
            self.assertIn("1.0 of 8 cores free", reply["reason"])
        self.assertEqual(len(self.history()), count)

    def test_round2_status_reason_uses_current_capacity(self):
        self.ask(6)
        self.ask(4, agent="head")
        self.ask(1, mem=GIB // 2, agent="tiny")
        status = self.c.handle({"op": "status"})
        self.assertIn("1.0 of 8 cores free", status["queue"][0]["reason"])

    def test_round2_missing_backfill_reason_does_not_break_tick(self):
        self.ask(6)
        self.ask(7, agent="head")
        self.ask(2, agent="queued", expect=36000)
        with patch.object(self.c, "_may_pass", return_value=False):
            self.c.tick()
        self.assertIn("backfill held", self.c._ticket(self.c.state["queue"][-1]["id"])["reason"])

    def test_round2_granted_head_clears_projection_keys(self):
        holder = self.ask(6, expect=600)
        head = self.ask(4, agent="head")
        self.assertIn("projected_start", self.c.state["queue"][0])
        self.release(holder)
        lease = next(x for x in self.c.state["leases"] if x["id"] == head["id"])
        for key in ("projected_start", "projected_ending", "head_since"):
            self.assertNotIn(key, lease)

    def test_grants_what_fits_and_queues_the_rest(self):
        self.assertEqual(self.ask(6)["state"], "granted")
        waiting = self.ask(4, agent="b")
        self.assertEqual(waiting["state"], "waiting")
        self.assertIn("2.0 of 8 cores free", waiting["reason"])

    def test_release_lets_the_next_in_line_start(self):
        first = self.ask(6)
        second = self.ask(4, agent="b")
        self.c.handle({"op": "release", "id": first["id"]})
        self.assertEqual(self.c.handle({"op": "poll", "id": second["id"]})["state"],
                         "granted")  # fmt: skip

    def test_the_line_is_first_come_first_served(self):
        self.ask(5)
        big = self.ask(7, agent="b")
        medium = self.ask(2, agent="c")  # fits now, but must not overtake b
        self.assertEqual(medium["state"], "waiting")
        self.assertIn("1 request(s) ahead", medium["reason"])
        self.assertEqual(big["state"], "waiting")

    def test_small_requests_backfill_past_a_blocked_head(self):
        self.ask(6)
        self.assertEqual(self.ask(4, agent="b")["state"], "waiting")
        self.assertEqual(self.ask(1, mem=GIB // 2, agent="c")["state"], "granted")

    def test_medium_backfill_finishes_before_big_head(self):
        self.ask(4, mem=8 * GIB, expect=600)
        head = self.ask(8, mem=12 * GIB, agent="big")
        medium = self.ask(3, mem=4 * GIB, agent="medium", expect=60)
        self.assertEqual(medium["state"], "granted")
        lease = self.c.state["leases"][-1]
        self.assertEqual(lease["backfill_head"], head["id"])
        self.assertIn("expected end", lease["backfill_reason"])
        self.assertIn("backfilled", self.history()[-1]["backfill_reason"])

    def test_medium_backfill_leaves_head_capacity(self):
        self.ask(4, mem=8 * GIB, expect=600)
        self.ask(6, mem=10 * GIB, agent="big")
        medium = self.ask(2, mem=4 * GIB, agent="medium", expect=1200)
        self.assertEqual(medium["state"], "granted")
        self.assertIn("leaves capacity", self.c.state["leases"][-1]["backfill_reason"])

    def test_backfill_that_would_delay_head_is_held_and_logged(self):
        self.ask(4, mem=8 * GIB, expect=60)
        self.ask(8, mem=12 * GIB, agent="big")
        medium = self.ask(3, mem=4 * GIB, agent="medium", expect=600)
        self.assertEqual(medium["state"], "waiting")
        self.assertIn("would delay", medium["reason"])
        self.assertIn("would delay", self.history()[-1]["reason"])

    def test_overrun_freezes_backfill_and_tells_agent_once(self):
        holder = self.ask(4, mem=8 * GIB, expect=600)
        head = self.ask(8, mem=14 * GIB, agent="big")
        medium = self.ask(3, mem=4 * GIB, agent="medium", expect=60)
        self.age(medium, 61, "granted")
        self.release(holder)
        later = self.ask(2, mem=GIB, agent="later", expect=1)
        self.assertEqual(later["state"], "waiting")
        self.assertIn("overrun", later["reason"])
        self.c._admit()
        messages = [m for m in self.c.state["messages"] if "backfill overrun" in m["text"]]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["to"], "medium")
        self.assertEqual(len([e for e in self.history() if e["event"] == "overrun"]), 1)
        self.release(medium)
        self.assertEqual(self.poll(head), "granted")

    def test_multiple_backfills_preserve_head_reservation(self):
        self.ask(4, mem=8 * GIB, expect=60)
        self.ask(6, mem=12 * GIB, agent="big")
        first = self.ask(2, mem=3 * GIB, agent="first", expect=600)
        self.assertEqual(first["state"], "granted")
        # Each fits beside the holder, but together they would delay the head.
        self.assertEqual(self.ask(2, mem=3 * GIB, agent="second", expect=600)["state"], "waiting")

    def test_overdue_backfill_that_leaves_room_does_not_freeze_line(self):
        holder = self.ask(4, mem=8 * GIB, expect=600)
        self.ask(6, mem=12 * GIB, agent="big")
        medium = self.ask(1, mem=GIB, agent="medium", expect=60)
        self.age(medium, 61, "granted")
        self.assertEqual(self.ask(1, mem=GIB, agent="tiny", expect=1)["state"], "granted")
        self.assertFalse(any(e["event"] == "overrun" for e in self.history()))
        self.release(holder)

    def test_estimate_uses_recent_successful_samples_only(self):
        lease = {"id": "L0", "agent": "a", "label": "a job", "cores": 1, "mem": GIB}
        for run in [9999, *range(1, 21)]:
            self.c._log("ended", lease, run=run, code=0)
        self.c._log("ended", lease, run=99999, code=1)
        self.assertEqual(self.c._estimate("a", "a job"), (18, "history"))

    def test_backfill_preserves_headroom_and_disk_rules(self):
        self.ask(4, mem=8 * GIB, expect=60)
        self.ask(6, mem=10 * GIB, agent="big", headroom=2)
        self.assertEqual(self.ask(2, agent="medium", expect=600)["state"], "waiting")
        self.assertEqual(self.ask(1, agent="disk", expect=1, disk={"/": GIB})["state"], "waiting")

    def test_expect_defaults_learn_from_same_agent_and_label(self):
        self.assertEqual(self.c._estimate("a", "a job"), (ac.DEFAULT_EXPECT, "default"))
        first = self.ask(1, expect=90)
        self.age(first, 75, "granted")
        self.release(first, code=0)
        second = self.ask(1)
        lease = self.c.state["leases"][-1]
        self.assertEqual(lease["expect_source"], "history")
        self.assertGreaterEqual(lease["expect"], 75)
        self.assertLess(lease["expect"], 76)
        self.assertEqual(self.c._estimate("other", "a job")[1], "history")
        self.assertEqual(self.c._estimate("a", "other job")[1], "default")
        self.release(second)
        self.assertIn("error", self.ask(1, expect=0))
        self.assertIn("error", self.ask(1, expect=float("inf")))

    def configure(self, cores=8, mem=16, footprint=None):
        self.c.capacity = lambda: (cores, mem * GIB)
        if footprint:
            self.c.footprint = footprint
        self.c.tick()

    def test_review_sliding_start_and_early_end(self):
        with patch.object(ac.time, "time", return_value=100000) as clock:
            a = self.ask(1, mem=5 * GIB, agent="A", expect=1800)
            self.ask(1, mem=5 * GIB, agent="B", expect=8 * 3600)
            head = self.ask(1, mem=10 * GIB, agent="big")
            clock.return_value += 1860
            r = self.ask(1, mem=6 * GIB, agent="R", expect=7 * 3600)
            self.assertEqual(r["state"], "waiting")
            self.release(a)
            self.assertEqual(self.poll(head), "granted")

    def test_review_tiny_without_projection(self):
        self.busy = self.c.busy = 6
        self.ask(4, agent="head")
        self.assertEqual(self.ask(1, mem=GIB // 2, expect=5)["state"], "granted")

    def test_review_old_and_overdue_holders_still_leave_capacity(self):
        for old in (False, True):
            with self.subTest(old=old):
                self.c.state["leases"].clear()
                self.c.state["queue"].clear()
                self.busy = self.c.busy = 0
                a = self.ask(6, mem=4 * GIB, expect=1800)
                if old:
                    self.c.state["leases"][0].pop("expect")
                    self.c._save()
                    self.c = self.make()
                self.age(a, 1900, "granted")
                self.busy = self.c.busy = 6
                self.ask(4, agent="head")
                self.assertEqual(self.ask(1, mem=GIB // 2, expect=5)["state"], "granted")
                # Release the tiny job so the medium job fits measured CPU.
                self.release(self.c.state["leases"][-1])
                self.assertEqual(self.ask(2, mem=GIB, expect=120)["state"], "granted")

    def test_review_available_bound_multiple_backfills(self):
        self.available = 30 * GIB
        self.configure(cores=12, mem=58, footprint=lambda pid: 20 * GIB)
        a = self.ask(1, mem=20 * GIB, expect=600)
        head = self.ask(1, mem=40 * GIB, agent="big")
        replies = [self.ask(1, mem=6 * GIB, agent=f"m{i}", expect=7200)
                   for i in range(3)]
        self.assertEqual([r["state"] for r in replies], ["granted", "waiting", "waiting"])
        self.available = self.c.available = 44 * GIB
        self.release(a)
        self.assertEqual(self.poll(head), "granted")

    def test_review_ends_before_start_respects_actual_admission(self):
        self._assert_same_pass_available_limit(with_head=True)

    def test_review_in_order_grants_respect_actual_admission(self):
        self._assert_same_pass_available_limit(with_head=False)

    def _assert_same_pass_available_limit(self, with_head):
        self.available = 42 * GIB
        holder_pid, candidate_pid = os.getpid(), os.getppid()
        self.configure(cores=12, mem=58,
                       footprint=lambda pid: 20 * GIB if pid == holder_pid else 0)
        self.ask(1, mem=20 * GIB, pid=holder_pid, expect=600)
        self.available = 22 * GIB
        self.c.tick()
        if with_head:
            self.assertEqual(self.ask(1, mem=30 * GIB, agent="big")["state"], "waiting")
        # Queue all candidates before one pass, with no new availability sample.
        with patch.object(self.c, "_admit", return_value=False):
            replies = [self.ask(1, mem=8 * GIB, pid=candidate_pid,
                                agent=f"m{i}", expect=300) for i in range(4)]
        with patch.object(self.c, "footprint", wraps=self.c.footprint) as footprint:
            self.c._admit()
            self.assertLessEqual(footprint.call_count, 2)
        self.assertEqual([self.c._ticket(r["id"])["state"] for r in replies],
                         ["granted", "granted", "waiting", "waiting"])
        granted = [x for x in self.c.state["leases"] if x["pid"] == candidate_pid]
        self.assertLessEqual(sum(x["mem"] for x in granted), 18 * GIB)
        if with_head:
            self.assertTrue(all("expected end before" in x["backfill_reason"] for x in granted))
        else:
            self.assertEqual(self.c._why(self.c.state["queue"][0])[0], "available")

    def test_review_available_reserves_only_unmeasured_warming_memory(self):
        with patch.object(ac.time, "time", return_value=100000) as clock:
            self.available = 22 * GIB
            self.configure(cores=12, mem=58, footprint=lambda pid: 3 * GIB)
            self.ask(1, mem=8 * GIB)
            # Of 18G usable, only the unmeasured 5G is reserved: 13G still fits.
            self.assertEqual(self.ask(1, mem=13 * GIB)["state"], "granted")
            self.assertEqual(self.ask(1, mem=9 * GIB)["state"], "waiting")
            # Underused claims stop reducing availability when warmup ends.
            clock.return_value += ac.WARMUP
            self.c._admit()
            self.assertEqual(len(self.c.state["queue"]), 0)

    def test_review_measured_cpu_projection(self):
        self.configure(cores=12, mem=58)
        for agent, run in (("A", 7200), ("B", 300)):
            self.c._log("ended", {"id": "old", "agent": agent, "label": f"{agent} job",
                                  "cores": 1, "mem": GIB}, run=run, code=0)
        a = self.ask(8, mem=4 * GIB, agent="A", expect=7200)
        b = self.ask(1, mem=30 * GIB, agent="B", expect=300)
        for r in (a, b):
            self.age(r, 120, "granted")
        self.busy = self.c.busy = 3
        head = self.ask(6, mem=40 * GIB, agent="big")
        self.assertEqual(self.ask(1, mem=20 * GIB, expect=3600)["state"], "waiting")
        self.assertEqual(self.c.state["queue"][0]["projected_ending"], [b["id"]])
        self.release(b)
        self.assertEqual(self.poll(head), "granted")

    def test_review_queued_lock_order(self):
        self.ask(4, mem=2 * GIB, expect=600)
        self.ask(6, mem=2 * GIB, agent="big")
        self.ask(5, agent="g1", locks=["gpu"], expect=600)
        later = self.ask(1, agent="g2", locks=["gpu"], expect=36000)
        self.assertEqual(later["state"], "waiting")
        self.assertIn("lock", later["reason"])

    def test_review_history_volume_and_no_tick_writes(self):
        for cpu in (False, True):
            with self.subTest(cpu=cpu):
                self.c.state["leases"].clear()
                self.c.state["queue"].clear()
                self.available = 12 * GIB if not cpu else 64 * GIB
                self.busy = self.c.busy = 0
                self.c.tick()
                a = self.ask(4, mem=4 * GIB, expect=600)
                if cpu:
                    self.age(a, 100, "granted")
                    self.busy = self.c.busy = 5
                self.ask(6, agent="big")
                self.ask(4 if cpu else 2, mem=GIB if cpu else 9 * GIB, agent="m", expect=60)
                self.c.tick()
                n = len(self.history())
                with patch.object(self.c, "_save", wraps=self.c._save) as save:
                    for i in range(50):
                        if cpu:
                            self.busy = 5 + (i % 7) / 10
                        else:
                            self.available = int((12 + (i % 7) / 10) * GIB)
                        self.c.tick()
                    self.assertEqual(len(self.history()), n)
                    save.assert_not_called()

    def test_review_footprints_cached_per_pass(self):
        self.available = 40 * GIB
        self.configure(cores=64, mem=200)
        for i in range(12):
            self.ask(1, mem=2 * GIB, agent=f"l{i}", expect=600+i)
        self.ask(1, mem=60 * GIB, agent="big")
        for i in range(8):
            self.ask(1, mem=3 * GIB, agent=f"q{i}", expect=7200)
        with patch.object(self.c, "footprint", return_value=GIB) as footprint:
            self.c._admit()
            self.assertLessEqual(footprint.call_count, len(self.c.state["leases"]))

    def test_review_estimate_filters_percentile_fallback_and_cache(self):
        x = {"id": "L0", "agent": "a", "label": "work", "cores": 1, "mem": GIB}
        for run in [*range(1, 20), 90000]:
            self.c._log("ended", x, run=run, code=0)
        for extra in ({}, {"why": "process exited"}, {"held": True, "code": 0}, {"code": 1}):
            self.c._log("ended", x, run=99999, **extra)
        with patch.object(ac, "read_history", wraps=ac.read_history) as read:
            self.assertEqual(self.c._estimate("a", "work"), (18, "history"))
            self.assertEqual(self.c._estimate("other", "work"), (18, "history"))
            for _ in range(5):
                self.c._estimate("a", "work")
            self.assertLessEqual(read.call_count, 1)

    def test_review_check_uses_learned_estimate_with_queue(self):
        self.ask(4, mem=8 * GIB, expect=600)
        self.ask(8, mem=12 * GIB, agent="head")
        x = {"id": "L0", "agent": "probe", "label": "probe job", "cores": 3, "mem": 4 * GIB}
        self.c._log("ended", x, run=30, code=0)
        req = {"op": "check", "agent": "probe", "label": "probe job", "cores": 3, "mem": 4 * GIB}
        self.assertTrue(self.c.handle(req)["ok"])
        self.assertEqual(self.ask(3, mem=4 * GIB, agent="probe")["state"], "granted")

    def test_review_expect_rejects_boolean_and_nonnumeric(self):
        for value in (True, False, "10m", [], {}, float("nan"), float("inf"), 0):
            with self.subTest(value=value):
                self.assertIn("error", self.ask(1, expect=value))
                self.assertIn("error", self.c.handle({"op": "check", "expect": value}))

    def test_review_capacity_backfill_cutoff(self):
        self.ask(4, mem=8 * GIB, expect=600)
        head = self.ask(6, mem=10 * GIB, agent="head")
        self.age(head, ac.BACKFILL_PASS_WINDOW + 1, "head_since")
        self.assertEqual(self.ask(2, mem=4 * GIB, expect=7200)["state"], "waiting")
        self.assertEqual(self.ask(1, expect=1)["state"], "granted")

    def test_review_projection_is_pinned_before_aging_cutoff(self):
        with patch.object(ac.time, "time", return_value=100000) as clock:
            a = self.ask(1, mem=5 * GIB, agent="A", expect=60)
            self.ask(1, mem=5 * GIB, agent="B", expect=8 * 3600)
            head = self.ask(1, mem=10 * GIB, agent="big")
            queued = self.c.state["queue"][0]
            initial = queued.get("projected_start")
            clock.return_value += 61
            self.assertEqual(self.ask(1, mem=6 * GIB, expect=7 * 3600)["state"], "waiting")
            self.assertLessEqual(queued["projected_start"], initial)
            self.release(a)
            self.assertEqual(self.poll(head), "granted")

    def test_review_holder_ends_early_and_head_starts_immediately(self):
        with patch.object(ac.time, "time", return_value=100000) as clock:
            holder = self.ask(4, mem=8 * GIB, expect=1800)
            head = self.ask(6, mem=10 * GIB, agent="head")
            self.assertEqual(self.ask(2, mem=4 * GIB, expect=7200)["state"], "granted")
            clock.return_value += 30
            self.release(holder)
            self.assertEqual(self.poll(head), "granted")

    def test_review_projection_preserves_outside_load_and_headroom(self):
        holder = self.ask(4, expect=600)
        self.age(holder, 120, "granted")
        self.busy = self.c.busy = 6
        self.vms = self.c.vms = [{"pid": 1, "cores": 2, "mem": GIB}]
        head = {"cores": 7, "mem": GIB}
        self.assertFalse(self.c._projected_fit(head, self.c.state["leases"]))
        self.busy = self.c.busy = 0
        self.vms = self.c.vms = []
        head = {"cores": 2, "mem": GIB, "headroom": 4}
        self.assertFalse(self.c._projected_fit(head, [], {"cores": 1, "mem": GIB}))

    def test_review_combined_exclusive_lock_stuck_and_backfilled_lease(self):
        holder = self.ask(4, mem=8 * GIB, expect=600)
        head = self.ask(8, mem=12 * GIB, agent="head")
        backfill = self.ask(3, mem=4 * GIB, agent="backfill", expect=30)
        self.assertEqual(backfill["state"], "granted")
        self.release(head)
        perf = self.ask(1, agent="perf", exclusive=True)
        self.age(perf, ac.EXCLUSIVE_PASS_WINDOW + 1)
        self.assertEqual(self.ask(1, expect=1)["state"], "waiting")
        self.release(perf)
        # A held GPU lock still gates a scoped exclusive beside a backfill.
        self.c.handle({"op": "lock", "agent": "owner", "name": "gpu"})
        gpu = self.ask(1, agent="gpu", exclusive_locks=["gpu"])
        self.assertEqual(gpu["state"], "waiting")
        self.release(holder)
        self.c.handle({"op": "unlock", "agent": "owner", "name": "gpu"})
        self.assertEqual(self.poll(gpu), "granted")
        self.assertEqual(self.poll(backfill), "granted")
        self.available = 5 * GIB
        self.c.footprint = lambda pid: 0
        # These underused jobs are already past startup when the head is stuck.
        for lease in self.c.state["leases"]:
            self.age(lease, ac.WARMUP, "granted")
        self.c.tick()
        stuck = self.ask(1, mem=10 * GIB, agent="stuck", locks=["nas"])
        self.age(stuck, ac.STUCK_AFTER + 1)
        self.assertEqual(self.ask(1, mem=GIB // 2, locks=["nas"])["state"], "granted")

    def test_review_unchanged_poll_does_not_rewrite_state(self):
        holder = self.ask(1)
        self.c.handle({"op": "poll", "id": holder["id"]})
        with patch.object(Path, "write_text", wraps=Path.write_text) as write:
            self.c.handle({"op": "poll", "id": holder["id"]})
            write.assert_not_called()

    def test_review_check_cli_routes_agent_and_label(self):
        with patch.object(ac, "call", return_value={"ok": True}) as call:
            self.assertEqual(ac.main(["--as", "agent", "check", "--cores", "3",
                                      "--mem", "4G", "--label", "compile"]), 0)
            self.assertEqual(call.call_args.args[0]["agent"], "agent")
            self.assertEqual(call.call_args.args[0]["label"], "compile")

    def test_review_authoritative_stale_available_sample(self):
        self.available = 42 * GIB
        self.configure(cores=12, mem=58, footprint=lambda pid: 20 * GIB)
        self.ask(1, mem=20 * GIB, expect=600)
        self.available = 22 * GIB
        self.c.tick()
        self.ask(1, mem=30 * GIB, agent="big")
        # Longer than the explicit holder estimate: exercise leaves-capacity.
        replies = [self.ask(1, mem=8 * GIB, agent=f"m{i}", expect=7200)
                   for i in range(4)]
        self.assertEqual([r["state"] for r in replies], ["granted", "waiting", "waiting", "waiting"])

    def test_review_holder_median_and_candidate_percentile_are_distinct(self):
        x = {"id": "old", "agent": "a", "label": "a job", "cores": 1, "mem": GIB}
        for run in range(10, 210, 10):
            self.c._log("ended", x, run=run, code=0)
        with patch.object(ac.time, "time", return_value=100000):
            self.assertEqual(self.c._estimate("a", "a job"), (180, "history"))
            self.assertEqual(self.c._holder_end({**x, "granted": 100000, "expect": 180}), 100105)
            self.assertEqual(self.c._holder_end({**x, "granted": 99000}), 100060)

    def test_review_held_run_never_teaches_an_estimate(self):
        held = self.ask(1, running=True)
        self.age(held, 120, "granted")
        self.release(held, code=0)
        self.assertEqual(self.c._estimate("a", "a job"), (ac.DEFAULT_EXPECT, "default"))

    def test_expect_duration_parser(self):
        self.assertEqual(ac.parse_expect("10m"), 600)
        self.assertEqual(ac.parse_expect("1.5h"), 5400)
        for value in ("0", "-1m", "nan", "forever"):
            with self.assertRaises(ac.argparse.ArgumentTypeError):
                ac.parse_expect(value)

    def test_cpu_jobs_pass_a_head_waiting_for_a_lock(self):
        self.ask(2, mem=4 * GIB, agent="gpu1", locks=["gpu"])  # holds the GPU
        head = self.ask(2, mem=4 * GIB, agent="gpu2", locks=["gpu"])
        self.assertEqual(head["state"], "waiting")
        # Needs no GPU and leaves room for the head: starts now.
        self.assertEqual(self.ask(3, mem=4 * GIB, agent="cpu")["state"], "granted")
        # Wants the same lock: stays behind the head.
        self.assertEqual(self.ask(1, mem=GIB // 2, agent="gpu3", locks=["gpu"])["state"],
                         "waiting")  # fmt: skip
        # The GPU holder's share frees with the lock; 3 more cores would
        # still leave the head short once it does: waits.
        self.assertEqual(self.ask(4, mem=GIB, agent="cpu2")["state"], "waiting")

    def test_head_waiting_for_capacity_with_a_free_lock_is_not_overtaken(self):
        self.ask(6)
        self.assertEqual(self.ask(4, agent="gpu", locks=["gpu"])["state"], "waiting")
        self.assertEqual(self.ask(2, agent="c")["state"], "waiting")

    def stuck_head(self, available, mem=11 * GIB, locks=("gpu",), waited=ac.STUCK_AFTER + 1):
        """A head that has waited, short of actually available memory."""
        self.available = available
        self.c.tick()
        head = self.ask(4, mem=mem, agent="big", locks=locks)
        self.assertEqual(head["state"], "waiting")
        self.c.state["queue"][0]["asked"] -= waited
        return head

    def test_stuck_head_that_no_lease_end_would_help_is_passed(self):
        # Today's stall: a big GPU lease, short of memory with nothing else running,
        # held a smaller GPU job behind it while the GPU sat idle.
        self.stuck_head(10 * GIB)
        self.assertEqual(self.ask(2, mem=2 * GIB, agent="c", locks=["gpu"])["state"], "granted")

    def test_stuck_head_is_told_once(self):
        head = self.stuck_head(10 * GIB)
        self.c.tick()
        self.c.tick()
        inbox = self.c.handle({"op": "inbox", "agent": "big"})["messages"]
        told = [m for m in inbox if head["id"] in m["text"]]
        self.assertEqual(len(told), 1, told)
        self.assertIn("even if every lease ended", told[0]["text"])

    def test_head_short_of_memory_keeps_its_place_at_first(self):
        self.stuck_head(10 * GIB, waited=0)
        self.assertEqual(self.ask(2, mem=2 * GIB, agent="c")["state"], "waiting")

    def test_head_that_a_lease_end_would_free_is_not_stuck(self):
        self.ask(2, mem=4 * GIB, agent="holder")  # really holds its 4 GiB
        self.c.footprint = lambda pid: 4 * GIB
        self.stuck_head(13 * GIB)  # 9 usable now, 13 once the lease ends: 11 fits then
        self.assertEqual(self.ask(1, mem=2 * GIB, agent="c")["state"], "waiting")

    def test_lease_counts_only_what_it_really_holds(self):
        self.ask(2, mem=4 * GIB, agent="holder")  # asked for 4 GiB, holds almost nothing
        self.c.footprint = lambda pid: GIB // 8
        self.stuck_head(13 * GIB)
        self.assertEqual(self.ask(1, mem=2 * GIB, agent="c")["state"], "granted")

    def test_lease_never_counts_for_more_than_its_size(self):
        self.ask(2, mem=GIB, agent="holder")
        self.c.footprint = lambda pid: 8 * GIB
        self.stuck_head(13 * GIB)  # 9 usable + at most 1 from the lease: 11 stays out of reach
        self.assertEqual(self.ask(1, mem=2 * GIB, agent="c")["state"], "granted")

    def test_stuck_head_takes_its_turn_again_once_it_would_fit(self):
        self.stuck_head(10 * GIB)
        self.available = 15 * GIB + GIB // 2
        self.c.tick()
        leases = [x["label"] for x in self.c.state["leases"]]
        self.assertEqual(leases, ["big job"])

    def test_line_behind_a_stuck_head_stays_in_order(self):
        self.ask(5, mem=GIB, agent="holder")
        self.stuck_head(10 * GIB, locks=())
        self.busy = 5.0
        self.c.tick()
        # "second" fits nowhere yet; a later request may not jump it just because the head is stuck.
        self.assertEqual(self.ask(5, mem=GIB, agent="second")["state"], "waiting")
        self.assertEqual(self.ask(2, mem=2 * GIB, agent="third")["state"], "waiting")

    def test_stuck_head_does_not_let_requests_past_a_waiting_exclusive(self):
        self.ask(1, agent="holder")
        self.assertEqual(self.ask(1, agent="whole", exclusive=True)["state"], "waiting")
        self.c.state["queue"][0]["asked"] -= ac.EXCLUSIVE_PASS_WINDOW + 1
        self.available = 10 * GIB
        self.c.tick()
        big = self.ask(4, mem=11 * GIB, agent="big")
        self.assertEqual(big["state"], "waiting")
        self.c.state["queue"][1]["asked"] -= ac.STUCK_AFTER + 1
        self.assertEqual(self.ask(1, mem=GIB // 2, agent="c")["state"], "waiting")

    def test_exclusive_request_short_of_memory_is_never_stuck(self):
        self.available = 10 * GIB
        self.c.tick()
        self.ask(1, agent="holder")
        self.assertEqual(self.ask(1, mem=11 * GIB, agent="whole", exclusive=True)["state"], "waiting")
        self.c.state["queue"][0]["asked"] -= ac.STUCK_AFTER + 1
        self.assertFalse(self.c._stuck(self.c.state["queue"][0]))

    def test_small_requests_pass_a_waiting_exclusive_head(self):
        self.ask(4)
        perf = self.ask(2, agent="perf", exclusive=True)
        self.assertEqual(perf["state"], "waiting")
        small = self.ask(ac.SMALL_CORES, mem=ac.SMALL_MEM, agent="small")
        self.assertEqual(small["state"], "granted")
        # Bigger than small: stays behind the exclusive request.
        self.assertEqual(self.ask(ac.SMALL_CORES + 1, agent="c")["state"], "waiting")
        self.assertEqual(self.ask(1, mem=ac.SMALL_MEM + GIB, agent="d")["state"], "waiting")
        # Other exclusive requests never overtake.
        self.assertEqual(self.ask(1, agent="perf2", exclusive=True)["state"], "waiting")
        # The exclusive one waits for the small one too, then starts.
        self.release(small)
        self.release(self.c.state["leases"][0])
        self.assertEqual(self.poll(perf), "granted")

    def test_small_requests_stop_passing_an_exclusive_head_after_the_window(self):
        self.ask(4)
        perf = self.ask(2, agent="perf", exclusive=True)
        self.age(perf, ac.EXCLUSIVE_PASS_WINDOW + 1)
        reply = self.ask(1, mem=GIB // 2, agent="c")
        self.assertEqual(reply["state"], "waiting")
        self.assertIn("1 request(s) ahead", reply["reason"])
        self.assertFalse(self.c.handle({"op": "check", "cores": 1, "mem": GIB})["ok"])

    def test_small_requests_sharing_the_exclusive_heads_lock_wait(self):
        self.ask(4)
        self.ask(2, agent="perf", exclusive=True, locks=["gpu"])
        self.assertEqual(self.ask(1, agent="c", locks=["gpu"])["state"], "waiting")
        self.assertEqual(self.ask(1, agent="d", locks=["nas"])["state"], "granted")

    def test_check_knows_small_requests_may_pass_an_exclusive_head(self):
        self.ask(4)
        self.ask(2, agent="perf", exclusive=True)
        self.assertTrue(self.c.handle({"op": "check", "cores": 2, "mem": GIB})["ok"])
        self.assertFalse(self.c.handle({"op": "check", "cores": 3, "mem": GIB})["ok"])

    def test_only_small_requests_pass_an_exclusive_request_deeper_in_line(self):
        self.ask(2, mem=4 * GIB, agent="gpu1", locks=["gpu"])
        self.assertEqual(self.ask(2, agent="gpu2", locks=["gpu"])["state"], "waiting")
        perf = self.ask(1, agent="perf", exclusive=True)
        # Would pass the lock-waiting head, but not the exclusive one behind it.
        self.assertEqual(self.ask(3, agent="cpu")["state"], "waiting")
        self.assertEqual(self.ask(1, agent="tiny")["state"], "granted")
        self.age(perf, ac.EXCLUSIVE_PASS_WINDOW + 1)
        self.assertEqual(self.ask(1, agent="tiny2")["state"], "waiting")

    def test_exclusive_lock_waits_for_the_lock_not_for_cpu_leases(self):
        cpu = self.ask(3, agent="cpu")
        gpu = self.ask(1, agent="gpu1", locks=["gpu"])
        timed = self.ask(2, agent="perf", exclusive_locks=["gpu"])
        self.assertEqual(timed["state"], "waiting")
        self.assertIn("gpu is held by gpu1", timed["reason"])
        self.release(gpu)
        self.assertEqual(self.poll(timed), "granted")  # beside the CPU lease
        self.assertEqual(self.poll(cpu), "granted")
        # The lock stays its own; CPU-only work still runs beside it.
        self.assertEqual(self.ask(1, agent="gpu2", locks=["gpu"])["state"], "waiting")
        self.assertIn("error", self.c.handle(
            {"op": "lock", "agent": "x", "name": "gpu", "reason": "r"}))
        self.assertEqual(self.ask(1, agent="cpu2")["state"], "granted")
        status = self.c.handle({"op": "status"})
        self.assertEqual([x["exclusive_locks"] for x in status["leases"]
                          if x.get("exclusive_locks")], [["gpu"]])  # fmt: skip

    def test_exclusive_lock_cores_are_guaranteed_after_warmup(self):
        timed = self.ask(4, agent="perf", exclusive_locks=["gpu"])
        plain = self.ask(2, agent="plain", locks=["nas"])
        for lease in (timed, plain):
            self.age(lease, ac.WARMUP + 1, key="granted")
        self.busy = self.c.busy = 1.0  # both measured at a core in total
        # Measured: 1 busy + perf's 4 guaranteed; plain counts as measured.
        self.assertEqual(self.ask(3, agent="c")["state"], "granted")
        self.assertEqual(self.ask(1, agent="d")["state"], "waiting")  # c warms up

    def test_reserved_lease_cores_count_in_full(self):
        reserved = self.ask(4, agent="handover", reserve=True, expect=600)
        plain = self.ask(2, agent="plain")
        for lease in (reserved, plain):
            self.age(lease, ac.WARMUP + 1, key="granted")
        self.assertEqual(self.c._reserved_cpu(self.c.state["leases"]), 4)
        self.assertEqual(self.c._counted_once(), {reserved["id"]})
        self.busy = self.c.busy = 0.0  # neither uses any CPU
        # 0 measured + 4 reserved: 4 cores left, not 8.
        self.assertEqual(self.ask(4, agent="c")["state"], "granted")
        self.assertEqual(self.ask(1, agent="d")["state"], "waiting")
        status = self.c.handle({"op": "status"})
        self.assertEqual([x["id"] for x in status["leases"] if x.get("reserve")], [reserved["id"]])

    def test_idle_reserved_exclusive_lock_is_never_borrowed(self):
        self.configure(cores=8, mem=16)
        reserved = self.ask(6, agent="handover", exclusive_locks=["gpu"], reserve=True, expect=600)
        self.age(reserved, ac.WARMUP + 1, key="granted")
        with patch.object(self.c, "_exclusive_idle", return_value=True):
            # An ordinary idle exclusive-lock lease of 6 cores would lend them.
            self.assertEqual(self.ask(2, agent="small1")["state"], "granted")
            self.assertEqual(self.ask(1, agent="small2")["state"], "waiting")
        lease = self.c.state["leases"][0]
        lease["exclusive_usage"] = {"gpu": {"sample_at": 0, "idle_since": 0,
                                            "observed": 0, "active": 0, "busy": False}}
        lease["granted"] = 0
        with patch.object(self.c, "_post") as post:
            self.c._monitor_exclusive(ac.EXCLUSIVE_IDLE_AFTER + 1e6, 0.0)
        post.assert_not_called()
        self.assertFalse(lease.get("exclusive_warnings"))

    def test_reserve_needs_expect_and_refuses_io(self):
        self.assertIn("--expect", self.ask(1, reserve=True)["error"])
        self.assertIn("--io", self.ask(1, reserve=True, io=True, expect=60)["error"])
        for argv in (["run", "--reserve", "--cores=1", "--mem=1G", "--label=x", "--", "true"],
                     ["hold", "--reserve", "--io", "--expect=1m", "--label=x", "--pid=1"],
                     ["hold", "--reserve", "--expect=1m", "--label=x", "--pid=1",
                      "--owner-pid=1"]):
            with patch.object(ac, "call") as call, patch("sys.stderr"), \
                    self.assertRaises(SystemExit):
                ac.main(["--as", "a", *argv])
            call.assert_not_called()

    def measure_leases(self, usage, busy=None, trees=None):
        """Tick until the smoothed measurements settle, with each lease pid
        using usage[pid] cores and the host busy (default: their sum). No
        process is signalled: CPU time and trees are looked up in dicts."""
        cpu = {pid: 0.0 for pid in usage}
        self.c.cpu_seconds = cpu.get
        self.c.tree_pids = lambda pid: (trees or {}).get(pid, {pid})
        self.busy = self.c.busy = sum(usage.values()) if busy is None else busy
        with patch.object(ac.time, "monotonic", return_value=1000) as clock:
            self.c.tick()
            for _ in range(80):
                clock.return_value += 4
                for pid, cores in usage.items():
                    cpu[pid] += cores * 4
                self.c.tick()

    def free_cores(self):
        return self.c.handle({"op": "status"})["free"]["cores"]

    def fits(self, cores):
        return self.c.handle({"op": "check", "cores": cores, "mem": GIB})["ok"]

    def timed(self, size=8, headroom=1, pid=None, **extra):
        self.configure(cores=12, mem=58)
        lease = self.ask(size, agent="perf", exclusive_locks=["gpu"], headroom=headroom,
                         pid=pid or os.getpid(), **extra)
        self.age(lease, ac.WARMUP + 1, "granted")
        return lease

    def admissible(self):
        """The largest request (in tenths of a core) a check admits."""
        return max([0.0] + [n / 10 for n in range(1, 121) if self.fits(n / 10)])

    def assertAdmissible(self, expected):
        # Smoothed measurements settle a hair off whole cores: within a step.
        got = self.admissible()
        self.assertTrue(expected - 0.1 - 1e-9 <= got <= expected, (got, expected))

    def test_exclusive_lock_lease_is_not_counted_twice(self):
        # The stall: an 8-core GPU timing run (headroom 1) measured at 8.5 of
        # 12 cores was counted at 8.5 measured + 8 reserved = 16.5, so nothing
        # (not even 2 cores) was admitted beside it for the whole run. Its use
        # up to its size comes off the measured load; the 0.5 above it is
        # other work and still counts.
        self.timed()
        self.measure_leases({os.getpid(): 8.5})
        self.assertAlmostEqual(self.free_cores(), 3.5, places=1)  # 12 - 8 - 0.5, not -4.5
        self.assertAdmissible(2.5)  # less 1 of headroom
        waiting = self.ask(4, agent="cpu4")
        self.assertEqual(waiting["state"], "waiting")
        self.release(waiting)
        self.assertEqual(self.ask(2, agent="cpu2")["state"], "granted")

    def test_use_above_an_exclusive_lock_leases_size_counts_as_load(self):
        self.timed()
        self.measure_leases({os.getpid(): 9.5})  # 1.5 above its quota: noise or other work
        self.assertAdmissible(1.5)

    def test_uncapped_held_exclusive_lock_lease_above_its_size(self):
        self.timed(running=True)  # hold: no scope caps it
        self.measure_leases({os.getpid(): 11.0})
        self.assertAdmissible(0.0)

    def test_exclusive_lock_lease_below_its_size_keeps_its_cores(self):
        self.timed(headroom=0)
        self.measure_leases({os.getpid(): 3.0})
        self.assertAlmostEqual(self.free_cores(), 4.0, places=1)  # its unused 5 stay reserved
        self.assertAdmissible(4.0)

    def test_two_exclusive_lock_leases_count_once_each(self):
        self.configure(cores=12, mem=58)
        gpu = self.ask(4, agent="gpu", exclusive_locks=["gpu"], pid=os.getpid())
        nas = self.ask(3, agent="nas", exclusive_locks=["nas"], pid=os.getppid())
        for lease in (gpu, nas):
            self.age(lease, ac.WARMUP + 1, "granted")
        self.measure_leases({os.getpid(): 4.0, os.getppid(): 3.0})
        self.assertAlmostEqual(self.free_cores(), 5.0, places=1)
        self.assertAdmissible(5.0)

    def test_exclusive_lock_lease_plus_unleased_load(self):
        self.timed(headroom=0)
        self.measure_leases({os.getpid(): 8.0}, busy=10.0)  # 2 cores of other work
        self.assertAlmostEqual(self.free_cores(), 2.0, places=1)
        self.assertAdmissible(2.0)

    def test_ordinary_lease_inside_an_exclusive_lock_leases_tree(self):
        # agent-coord run inside a timing run: the inner job is in its own
        # scope (outside the outer quota) but in the outer process tree, so
        # the outer tree measures both. Only the outer lease's size comes
        # off; the inner job's 2 cores stay counted.
        outer = os.getpid()
        self.timed(headroom=0, pid=outer)
        inner = self.ask(2, agent="inner", pid=os.getppid())
        self.age(inner, ac.WARMUP + 1, "granted")
        self.measure_leases({outer: 10.0, os.getppid(): 2.0}, busy=10.0,
                            trees={outer: {outer, os.getppid()}})
        self.assertAlmostEqual(self.free_cores(), 2.0, places=1)
        self.assertAdmissible(2.0)

    def test_nested_exclusive_lock_leases_are_measured_once(self):
        # Two timing leases, the inner one in the outer's tree (whose
        # measurement includes it): neither is subtracted twice.
        outer = os.getpid()
        self.timed(size=6, headroom=0, pid=outer)
        inner = self.ask(2, agent="perf", exclusive_locks=["nas"], pid=os.getppid())
        self.age(inner, ac.WARMUP + 1, "granted")
        self.measure_leases({outer: 6.0, os.getppid(): 2.0}, busy=7.0,
                            trees={outer: {outer, os.getppid()}})
        # 6 + 2 reserved; 7 busy less the outer tree's 6: 1 of other work.
        self.assertAlmostEqual(self.free_cores(), 3.0, places=1)

    def test_a_ramping_exclusive_lock_lease_is_not_over_subtracted(self):
        # Its own use is smoothed from zero like the host figure, so while it
        # ramps up it never takes more out of the busy figure than that holds.
        self.timed(headroom=0)
        self.measure_leases({os.getpid(): 2.0})
        with patch.object(ac.time, "monotonic", return_value=9000) as clock:
            cpu = {os.getpid(): 0.0}
            self.c.cpu_seconds = cpu.get
            self.c.tick()
            clock.return_value += 4
            cpu[os.getpid()] += 8 * 4  # jumps to 8
            self.busy = 8.0
            self.c.tick()
        self.assertLessEqual(self.c._own_cores[self.c.state["leases"][0]["id"]], self.c.busy + 1e-9)
        self.assertLessEqual(self.free_cores(), 4.0 + 0.01)


    def test_exclusive_lock_and_exclusive_together_are_refused(self):
        self.assertIn("error", self.ask(1, exclusive=True, exclusive_locks=["gpu"]))

    def test_headroom_keeps_cores_unclaimed_while_it_runs(self):
        timed = self.ask(2, agent="perf", exclusive_locks=["gpu"], headroom=4)
        self.assertEqual(timed["state"], "granted")
        refused = self.ask(3, agent="c")  # 2 + 3 + 4 > 8
        self.assertEqual(refused["state"], "waiting")
        self.assertIn("kept free for perf's lease", refused["reason"])
        self.release(refused)
        self.assertEqual(self.ask(2, agent="d")["state"], "granted")  # 2 + 2 + 4 = 8
        self.release(timed)
        self.assertEqual(self.ask(3, agent="e")["state"], "granted")

    def test_headroom_waits_to_start_until_it_is_free(self):
        first = self.ask(5)
        timed = self.ask(2, agent="perf", exclusive_locks=["gpu"], headroom=2)
        self.assertEqual(timed["state"], "waiting")
        self.assertIn("kept free for this request", timed["reason"])
        self.release(first)
        self.assertEqual(self.poll(timed), "granted")

    def test_headroom_beyond_the_computer_is_refused(self):
        self.assertIn("error", self.ask(6, headroom=4))

    def test_state_from_an_older_version_still_loads(self):
        self.state.write_text(json.dumps({"leases": [{
            "id": "L7", "agent": "old", "pid": os.getpid(), "cores": 2.0,
            "mem": GIB, "label": "old job", "exclusive": False, "asked": 0,
            "granted": 0, "cwd": "/"}], "queue": [], "next": 8}))  # fmt: skip
        self.c = self.make()
        self.assertEqual(self.ask(6)["state"], "granted")
        self.assertEqual(self.c.handle({"op": "release", "id": "L7", "code": 0}),
                         {"released": True})  # fmt: skip
        self.assertEqual(self.history()[-1]["event"], "ended")

    def test_history_records_each_lease_event(self):
        first = self.ask(6)
        second = self.ask(4, agent="b")
        self.release(first, code=0)
        self.assertEqual(self.poll(second), "granted")
        self.release(second, code=3)
        events = [(e["event"], e["id"]) for e in self.history()]
        self.assertEqual(events, [
            ("requested", first["id"]), ("started", first["id"]),
            ("requested", second["id"]), ("waiting", second["id"]),
            ("ended", first["id"]), ("started", second["id"]),
            ("ended", second["id"])])  # fmt: skip
        log = self.history()
        self.assertEqual(log[3]["kind"], "cores")
        self.assertIn("cores free", log[3]["reason"])
        self.assertEqual(log[4]["code"], 0)
        self.assertEqual(log[-1]["code"], 3)
        for key in ("wait", "run"):
            self.assertIn(key, log[-1])
        self.assertEqual(log[0]["agent"], "a")
        self.assertEqual(log[0]["label"], "a job")
        self.assertTrue(all("cwd" not in e and "pid" not in e for e in log))

    def test_history_logs_waiting_only_when_the_kind_changes(self):
        self.ask(1, agent="gpu1", locks=["gpu"])
        hog = self.ask(5, agent="hog")
        head = self.ask(4, agent="gpu2", locks=["gpu"])
        for _ in range(5):
            self.c.tick()
        self.release(self.c.state["leases"][0])  # gpu free; still short of cores
        for _ in range(5):
            self.c.tick()
        kinds = [e["kind"] for e in self.history()
                 if e["event"] == "waiting" and e["id"] == head["id"]]  # fmt: skip
        self.assertEqual(kinds, ["lock:gpu", "cores"])
        self.release(hog)
        self.assertEqual(self.poll(head), "granted")

    def test_history_records_cancelled_requests(self):
        self.ask(8)
        waiting = self.ask(2, agent="b")
        self.release(waiting, why="max-wait")
        self.ask(2, agent="c", pid=dead_pid())
        cancelled = [e for e in self.history() if e["event"] == "cancelled"]
        self.assertEqual([e["agent"] for e in cancelled], ["b", "c"])
        self.assertEqual(cancelled[0]["why"], "max-wait")
        self.assertEqual(cancelled[1]["why"], "process exited")
        self.assertIn("wait", cancelled[0])

    def test_history_rotates_by_size(self):
        old_max, old_keep = ac.HISTORY_MAX_BYTES, ac.HISTORY_KEEP
        ac.HISTORY_MAX_BYTES, ac.HISTORY_KEEP = 2000, 2
        try:
            for _ in range(40):
                self.release(self.ask(1))
        finally:
            ac.HISTORY_MAX_BYTES, ac.HISTORY_KEEP = old_max, old_keep
        path = Path(self.tmp.name, "history.jsonl")
        files = ac.history_files(path)
        self.assertEqual([f.name for f in files],
                         ["history.jsonl.2", "history.jsonl.1", "history.jsonl"])  # fmt: skip
        self.assertFalse(path.with_name("history.jsonl.3").exists())
        self.assertTrue(all(f.stat().st_size < 2000 + 500 for f in files))
        times = [e["time"] for e in ac.read_history(0, path)]
        self.assertEqual(times, sorted(times))
        self.assertEqual(ac.read_history(0, path)[-1]["event"], "ended")

    def test_memory_is_a_limit_too(self):
        self.ask(1, mem=10 * GIB)
        self.assertEqual(self.ask(1, mem=8 * GIB, agent="b")["state"], "waiting")

    def test_exclusive_waits_for_others_then_blocks_new_ones(self):
        other = self.ask(1)
        timed = self.ask(2, agent="perf", exclusive=True)
        self.assertEqual(timed["state"], "waiting")
        self.c.handle({"op": "release", "id": other["id"]})
        self.assertEqual(self.c.handle({"op": "poll", "id": timed["id"]})["state"],
                         "granted")  # fmt: skip
        later = self.ask(1, agent="c")
        self.assertEqual(later["state"], "waiting")
        self.assertIn("perf holds an exclusive lease", later["reason"])

    def test_running_vms_count_at_measured_use(self):
        self.vms = [{"pid": 1, "name": "win", "cores": 5.5, "mem": 10 * GIB}]
        self.busy = 5.5
        for _ in range(30):
            self.c.tick()
        self.assertEqual(self.ask(4)["state"], "waiting")
        self.assertEqual(self.ask(1, mem=5 * GIB, agent="b")["state"], "waiting")

    def test_unleased_load_counts(self):
        self.busy = 6.0
        for _ in range(30):  # let the smoothing settle
            self.c.tick()
        self.assertEqual(self.ask(4)["state"], "waiting")
        self.assertEqual(self.ask(2, agent="b")["state"], "waiting")  # in line

    def test_leases_count_at_measured_use_after_warmup(self):
        first = self.ask(6)
        self.assertEqual(self.ask(4, agent="b")["state"], "waiting")  # warming up
        self.c.handle({"op": "release", "id": self.c.state["queue"][0]["id"]})
        self.busy = 2.0  # it uses 2 of its 6 cores
        for x in self.c.state["leases"]:
            if x["id"] == first["id"]:
                x["granted"] -= ac.WARMUP + 1
        for _ in range(30):
            self.c.tick()
        self.assertEqual(self.ask(4, agent="b")["state"], "granted")
        self.assertEqual(self.ask(4, agent="c")["state"], "waiting")  # warming up

    def test_memory_counts_at_lease_size_however_little_is_used(self):
        self.ask(1, mem=12 * GIB)
        self.assertEqual(self.ask(1, mem=6 * GIB, agent="b")["state"], "waiting")

    def test_memory_actually_available_is_a_limit(self):
        self.available = 6 * GIB
        self.c.tick()
        self.assertEqual(self.ask(1, mem=3 * GIB)["state"], "waiting")

    def test_holding_running_work_is_immediate_and_counts(self):
        self.ask(8)
        held = self.c.handle({"op": "acquire", "agent": "vm", "pid": os.getpid(),
                              "cores": 6, "mem": GIB, "label": "vm",
                              "running": True})  # fmt: skip
        self.assertEqual(held["state"], "granted")
        self.assertEqual(self.ask(1, agent="b")["state"], "waiting")

    def test_a_held_vm_is_not_counted_twice(self):
        self.vms = [{"pid": os.getpid(), "name": "win", "cores": 5.0, "mem": 10 * GIB}]
        self.c.tick()
        self.c.handle({"op": "acquire", "agent": "vm", "pid": os.getpid(),
                       "cores": 6, "mem": 10 * GIB, "label": "vm",
                       "running": True})  # fmt: skip
        self.assertEqual(self.ask(2, mem=GIB, agent="b")["state"], "granted")

    def test_disk_reservations_add_up_per_disk(self):
        self.c.disk_free = lambda mount: 200 * GIB
        first = self.c.handle({"op": "acquire", "agent": "a", "pid": os.getpid(),
                               "cores": 1, "mem": GIB, "label": "x",
                               "disk": {"/": 150 * GIB}})  # fmt: skip
        self.assertEqual(first["state"], "granted")
        second = self.c.handle({"op": "acquire", "agent": "b", "pid": os.getpid(),
                                "cores": 1, "mem": GIB, "label": "y",
                                "disk": {"/": 50 * GIB}})  # fmt: skip
        self.assertEqual(second["state"], "waiting")
        self.assertIn(f"of disk free on {ac.mount_of('/')}", second["reason"])

    def test_named_locks(self):
        self.assertIn("locked", self.c.handle(
            {"op": "lock", "agent": "a", "name": "windows-vm", "reason": "timing"}))
        taken = self.c.handle({"op": "lock", "agent": "b", "name": "windows-vm",
                               "reason": "mine"})  # fmt: skip
        self.assertIn("held by a (timing)", taken["error"])
        lease = self.c.handle({"op": "acquire", "agent": "b", "pid": os.getpid(),
                               "cores": 1, "mem": GIB, "label": "vm job",
                               "locks": ["windows-vm"]})  # fmt: skip
        self.assertEqual(lease["state"], "waiting")
        self.assertIn("error", self.c.handle({"op": "unlock", "agent": "b",
                                              "name": "windows-vm"}))  # fmt: skip
        self.c.handle({"op": "unlock", "agent": "a", "name": "windows-vm"})
        self.assertEqual(self.c.handle({"op": "poll", "id": lease["id"]})["state"],
                         "granted")  # fmt: skip
        # Now held through b's lease:
        self.assertIn("error", self.c.handle(
            {"op": "lock", "agent": "c", "name": "windows-vm", "reason": "x"}))

    def test_locks_end_with_their_pid_or_ttl(self):
        self.c.handle({"op": "lock", "agent": "a", "name": "gpu", "reason": "r",
                       "pid": dead_pid()})  # fmt: skip
        self.c.handle({"op": "lock", "agent": "a", "name": "nas", "reason": "r",
                       "ttl": -1})  # fmt: skip
        self.c.tick()
        self.assertEqual(self.c.state["locks"], {})

    def test_a_dead_holder_frees_its_lease(self):
        self.ask(8, pid=dead_pid())
        self.assertEqual(self.ask(8, agent="b")["state"], "granted")

    def test_pidfile_hold_survives_driver_restart_but_expires_without_replacement(self):
        pidfile = Path(self.tmp.name, "driver.pid")
        pidfile.write_text(str(os.getpid()))
        lease = self.ask(1, running=True, pidfile=str(pidfile))
        self.c.tick()
        pidfile.unlink()
        clock = time.time()
        with patch.object(ac.time, "monotonic", return_value=time.monotonic() + 10):
            self.c.tick()
        reply = self.c.handle({"op": "poll", "id": lease["id"]})
        self.assertEqual(reply, {"state": "granted", "restarting": True})
        self.c = self.make()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            pidfile.write_text(str(proc.pid))
            self.c.tick()
            status = self.c.handle({"op": "status"})
            self.assertEqual([(x["id"], x["pid"]) for x in status["leases"]],
                             [(lease["id"], proc.pid)])
            self.assertEqual(self.poll(lease), "granted")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
        with patch.object(ac.time, "monotonic", return_value=time.monotonic() + 10):
            self.c.tick()
        with patch.object(ac.time, "time", return_value=clock + ac.RESTART_GRACE + 5):
            self.assertEqual(self.poll(lease), "gone")
        events = [x for x in self.history() if x["event"] == "ended"]
        self.assertEqual([x["why"] for x in events], ["registration inactive"])

    def test_unit_hold_switches_pid_and_accepts_pidfile_fallback(self):
        pidfile = Path(self.tmp.name, "driver.pid")
        pidfile.write_text(str(os.getpid()))
        lease = self.ask(1, running=True, unit="driver.service", pidfile=str(pidfile))
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with patch.object(ac, "unit_pid", return_value=proc.pid):
                self.c.tick()
            self.assertEqual(self.c.handle({"op": "status"})["leases"][0]["pid"], proc.pid)
            with (patch.object(ac, "unit_pid", return_value=0),
                  patch.object(ac.time, "monotonic", return_value=time.monotonic() + 10)):
                self.c.tick()
            self.assertEqual(self.c.handle({"op": "status"})["leases"][0]["pid"], os.getpid())
            self.assertEqual(self.poll(lease), "granted")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_phase_releases_gpu_without_releasing_driver_lease(self):
        driver = self.ask(3, agent="driver")
        phase = {"op": "phase", "id": driver["id"], "name": "gpu", "agent": "driver"}
        self.assertIn("error", self.c.handle({**phase, "agent": "other"}))
        self.assertEqual(self.c.handle(phase)["locked"], "gpu")
        competing = self.ask(2, agent="gpu", locks=["gpu"])
        cpu = self.ask(1, agent="cpu")
        self.assertEqual(self.poll(competing), "waiting")
        self.assertEqual(self.poll(cpu), "granted")
        self.assertEqual(self.c.handle({**phase, "release": True})["released"], "gpu")
        self.assertEqual(self.poll(competing), "granted")
        self.assertEqual(self.poll(driver), "granted")
        event = next(x for x in self.history() if x["event"] == "exclusive-ended")
        self.assertEqual(event["resource"], "gpu")
        self.assertEqual(event["observed_seconds"], 0)

    def test_exclusive_idle_and_overrun_warn_and_allow_only_unrelated_small_work(self):
        lease = self.ask(8, mem=0, exclusive_locks=["gpu"], expect=600)
        stamp = time.time()
        for seconds in range(0, 901, 30):
            with (patch.object(ac.time, "monotonic", return_value=seconds),
                  patch.object(ac.time, "time", return_value=stamp + seconds)):
                self.c.gpu_sample = lambda: 0
                self.c.tick()
                if seconds == 870:
                    self.assertFalse(self.c.handle({"op": "check", "cores": 1, "mem": GIB})["ok"])
        with patch.object(ac.time, "time", return_value=stamp + 900):
            status = self.c.handle({"op": "status"})
            self.assertEqual(set(status["leases"][0]["exclusive_warnings"]), {"overrun", "idle:gpu"})
            inbox = self.c.handle({"op": "inbox", "agent": "a"})["messages"]
            self.assertEqual(len(inbox), 2)
            self.assertFalse(self.c.handle({"op": "check", "cores": 1, "mem": GIB,
                                            "locks": ["gpu"]})["ok"])
            self.assertFalse(self.c.handle({"op": "check", "cores": 3, "mem": GIB})["ok"])
            self.assertEqual(self.poll(self.ask(1, agent="small")), "granted")
        self.c.gpu_sample = lambda: None
        with (patch.object(ac.time, "monotonic", return_value=930),
              patch.object(ac.time, "time", return_value=stamp + 930)):
            self.c.tick()
            self.assertFalse(self.c.handle({"op": "check", "cores": 1, "mem": GIB})["ok"])
            self.release(lease)
        event = next(x for x in self.history() if x["event"] == "exclusive-ended")
        self.assertEqual(event["observed_seconds"], 900)
        self.assertEqual(event["active_seconds"], 0)

    def test_detached_unit_recovers_same_lease_after_wrapper_exit(self):
        unit = "test-driver.service"
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        lease = self.ask(1, agent="driver", pid=proc.pid, run_unit=unit, headroom=1)
        proc.wait(timeout=5)
        self.assertEqual(self.c.handle({"op": "poll", "id": lease["id"]}).get("restarting"), True)
        self.assertIn("error", self.ask(2, agent="driver", run_unit=unit))
        resumed = self.ask(1, agent="driver", run_unit=unit, headroom=1)
        self.assertEqual(resumed, {"id": lease["id"], "state": "granted"})

    def test_too_big_is_refused_not_queued(self):
        reply = self.ask(16)
        self.assertIn("error", reply)
        self.assertEqual(self.c.state["queue"], [])

    def test_check_reports_without_taking_anything(self):
        self.assertTrue(self.c.handle({"op": "check", "cores": 8, "mem": GIB})["ok"])
        self.ask(6)
        reply = self.c.handle({"op": "check", "cores": 4, "mem": GIB})
        self.assertFalse(reply["ok"])
        self.assertEqual(len(self.c.state["leases"]), 1)

    def test_waiters_nudge_holders_once(self):
        self.ask(8, agent="holder")
        waiting = self.ask(2, agent="b")
        self.c.state["queue"][0]["asked"] -= ac.NUDGE_AFTER + 1
        self.c.tick()
        self.c.tick()
        inbox = self.c.handle({"op": "inbox", "agent": "holder"})["messages"]
        self.assertEqual(len(inbox), 1)
        self.assertIn("b has waited", inbox[0]["text"])
        self.assertEqual(self.c.handle({"op": "poll", "id": waiting["id"]})["state"],
                         "waiting")  # fmt: skip

    def sample_io(self, usage):
        cpu = {pid: 0 for pid in usage}
        self.c.cpu_seconds = cpu.get
        self.c.footprint = lambda pid: GIB
        self.busy = self.c.busy = sum(usage.values())
        with patch.object(ac.time, "monotonic", return_value=1000) as clock:
            self.c.tick()
            clock.return_value += 4
            for pid, cores in usage.items():
                cpu[pid] += cores * 4
            self.c.tick()

    def test_io_skips_cpu_warmup_but_reserves_memory(self):
        self.ask(3, mem=12 * GIB, io=True, exclusive_locks=["wan"])
        self.assertEqual(self.ask(7, mem=5 * GIB)["state"], "waiting")
        self.assertIn("of 16.0G free", self.c.handle({"op": "status"})["queue"][0]["reason"])
        self.release(self.c.state["queue"][0])
        self.assertEqual(self.ask(7, mem=2 * GIB)["state"], "granted")

    def test_io_uses_measured_cpu_for_headroom(self):
        self.ask(3, io=True)
        self.sample_io({os.getpid(): 0.25})
        probe = {"op": "check", "cores": 5, "mem": GIB, "headroom": 2}
        self.assertTrue(self.c.handle(probe)["ok"])
        self.assertFalse(self.c.handle({**probe, "cores": 6})["ok"])
        lease, = self.c.handle({"op": "status"})["leases"]
        self.assertEqual(lease["measured_cores"], 0.25)

    def test_backfill_leaves_headroom_beside_measured_io(self):
        self.ask(3, agent="download", io=True, expect=7200)
        self.sample_io({os.getpid(): 0.25})
        holder = self.ask(4, mem=8 * GIB, expect=600)
        head = self.ask(5, mem=10 * GIB, agent="head", headroom=1)
        backfill = self.ask(1.5, mem=2 * GIB, expect=3600)
        self.assertEqual(backfill["state"], "granted")
        self.assertIn("leaves capacity", self.history()[-1]["backfill_reason"])
        self.release(holder)
        self.assertEqual(self.poll(head), "granted")

    def test_lock_wait_backfill_uses_measured_io(self):
        self.ask(3, agent="download", io=True)
        self.sample_io({os.getpid(): 0.25})
        self.c.handle({"op": "lock", "name": "gpu"})
        head = self.ask(5, locks=["gpu"], headroom=1)
        self.assertEqual(self.ask(1.5, mem=2 * GIB)["state"], "granted")
        self.c.handle({"op": "unlock", "name": "gpu"})
        self.assertEqual(self.poll(head), "granted")

    def test_io_nudges_skip_idle_holders_but_not_busy_ones(self):
        self.ask(1, agent="idle", io=True)
        self.ask(1, agent="busy", io=True, pid=os.getppid())
        self.sample_io({os.getpid(): 0.1, os.getppid(): 0.5})
        waiting = self.ask(8, agent="head")
        self.age(waiting, ac.NUDGE_AFTER + 1)
        self.poll(waiting)
        self.assertEqual(self.c.handle({"op": "inbox", "agent": "idle"})["messages"], [])
        busy, = self.c.handle({"op": "inbox", "agent": "busy"})["messages"]
        self.assertIn("head has waited", busy["text"])

    def test_io_memory_uses_footprint_above_declared_size(self):
        footprint = GIB // 4
        self.c.footprint = lambda pid: footprint
        self.ask(1, mem=GIB, io=True, running=True)
        probe = {"op": "check", "cores": 1, "mem": 15 * GIB}
        self.assertTrue(self.c.handle(probe)["ok"])
        self.assertFalse(self.c.handle({**probe, "mem": 16 * GIB})["ok"])
        footprint = 2 * GIB
        self.assertFalse(self.c.handle(probe)["ok"])
        lease, = self.c.handle({"op": "status"})["leases"]
        self.assertEqual(lease["claimed_mem"], 2 * GIB)

    def test_duplicate_io_holds_do_not_hide_a_stuck_head(self):
        self.c.footprint = lambda pid: 4 * GIB
        for agent in ("stream", "download"):
            self.ask(1, mem=GIB, io=True, running=True, agent=agent)
        head = self.stuck_head(10 * GIB, locks=())
        self.assertEqual(self.ask(2, mem=2 * GIB)["state"], "granted")
        hint, = self.c.handle({"op": "inbox", "agent": "big"})["messages"]
        self.assertIn(head["id"], hint["text"])

    def test_duplicate_io_holds_do_not_let_backfill_spend_memory_twice(self):
        self.c.footprint = lambda pid: 4 * GIB
        self.available = 10 * GIB
        self.c.tick()
        for agent in ("stream", "download"):
            self.ask(1, mem=GIB, io=True, running=True, agent=agent, locks=["wan"])
        head = self.ask(1, mem=8 * GIB, agent="head", locks=["wan"])
        self.assertEqual(head["state"], "waiting")
        self.assertEqual(self.ask(2, mem=4 * GIB)["state"], "waiting")

    def test_sustained_low_cpu_or_memory_hints_once(self):
        busy_pid = os.getppid()
        with (patch.object(ac.time, "time", return_value=100000) as wall,
              patch.object(ac.time, "monotonic", return_value=1000) as clock):
            idle = self.ask(3, agent="idle", mem=GIB)
            self.ask(3, agent="busy", pid=busy_pid, mem=GIB)
            memory = self.ask(1, agent="memory", mem=8 * GIB)
            self.ask(3, agent="io", mem=GIB, io=True)
            self.c.cpu_seconds = lambda pid: clock.return_value * (2 if pid == busy_pid else 0.2)
            self.c.footprint = lambda pid: GIB

            def advance(seconds):
                wall.return_value += seconds
                clock.return_value += seconds
                self.c.tick()

            self.c.tick()
            advance(ac.WARMUP)
            advance(ac.IO_HINT_AFTER - 1)
            self.assertFalse(self.c.handle({"op": "inbox", "agent": "idle"})["messages"])
            advance(1)
            advance(ac.IO_HINT_AFTER)
            for agent, lease, detail in (("idle", idle, "0.20 of 3 cores"),
                                         ("memory", memory, "1.0G of 8.0G RAM")):
                hint, = self.c.handle({"op": "inbox", "agent": agent})["messages"]
                self.assertIn(lease["id"], hint["text"])
                self.assertIn(f"{agent} job", hint["text"])
                self.assertIn(detail, hint["text"])
                self.assertIn("--io", hint["text"])
            for agent in ("io", "busy"):
                self.assertFalse(self.c.handle({"op": "inbox", "agent": agent})["messages"])

    def test_messages_to_one_agent_and_to_all(self):
        self.c.handle({"op": "send", "agent": "a", "to": "b", "text": "hi b"})
        self.c.handle({"op": "send", "agent": "a", "to": "all", "text": "hi all"})
        texts = [m["text"] for m in
                 self.c.handle({"op": "inbox", "agent": "b"})["messages"]]  # fmt: skip
        self.assertEqual(texts, ["hi b", "hi all"])
        self.assertEqual(self.c.handle({"op": "inbox", "agent": "b"})["messages"], [])
        own = self.c.handle({"op": "inbox", "agent": "a"})["messages"]
        self.assertEqual(own, [], "senders don't get their own broadcasts")

    def test_state_survives_a_restart(self):
        held = self.ask(3)
        self.c.handle({"op": "send", "agent": "a", "to": "b", "text": "kept"})
        self.c = self.make()
        self.assertEqual(self.c.handle({"op": "poll", "id": held["id"]})["state"],
                         "granted")  # fmt: skip
        self.assertEqual(len(self.c.handle({"op": "inbox", "agent": "b"})["messages"]), 1)


@unittest.skipIf(ac.WINDOWS, "the comparison baseline is Linux-only")
class SameAsMainWithoutExclusiveLeasesTest(unittest.TestCase):
    """Without exclusive-lock leases, admission decides exactly as main's
    code (the merge base with origin/main) on the same random workloads."""

    @classmethod
    def setUpClass(cls):
        here = Path(__file__).resolve().parent
        try:
            base = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], cwd=here,
                                  capture_output=True, text=True, check=True, timeout=10).stdout.strip()
            top = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=here,
                                 capture_output=True, text=True, check=True, timeout=10).stdout.strip()
            source = subprocess.run(["git", "show", f"{base}:{top}agent_coord.py"], cwd=here,
                                    capture_output=True, text=True, check=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise unittest.SkipTest(f"main's agent_coord.py is not available: {e}")
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name, "main_agent_coord.py")
        path.write_text(source)
        import importlib.util
        spec = importlib.util.spec_from_file_location("main_agent_coord", path)
        cls.main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.main)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_random_workloads_decide_as_on_main(self):
        import random
        pids = [os.getpid(), os.getppid()]
        for seed in range(60):
            rng = random.Random(seed)
            env = {"busy": 0.0, "now": 100000.0, "mono": 1000.0, "cpu": {pid: 0.0 for pid in pids}}
            with tempfile.TemporaryDirectory() as tmp, \
                    patch("time.time", side_effect=lambda: env["now"]), \
                    patch("time.monotonic", side_effect=lambda: env["mono"]):
                coordinators = [
                    module.Coordinator(
                        Path(tmp, name, "state.json"),
                        measure=lambda: {"vms": [], "busy": env["busy"], "available": 64 * GIB},
                        cap=lambda: (12.0, 58 * GIB), footprint=lambda pid: 0,
                        cpu_seconds=lambda pid: env["cpu"].get(pid),
                        **({"measured_mem": False, "scope_memory": lambda pid: None}
                           if module is ac else {}))
                    for module, name in ((self.main, "main"), (ac, "branch"))]
                for step in range(40):
                    op = rng.random()
                    if op < 0.45:
                        request = {"op": "acquire", "agent": f"a{rng.randrange(3)}",
                                   "pid": rng.choice(pids), "cores": rng.choice([1, 2, 3, 4, 6]),
                                   "mem": rng.choice([1, 2, 4, 8]) * GIB, "label": "job",
                                   "expect": rng.choice([60, 600, 3600]),
                                   "headroom": rng.choice([0, 0, 0, 1, 2]),
                                   "locks": rng.choice([[], [], ["nas"]]),
                                   "io": rng.random() < 0.1}
                        replies = [c.handle(dict(request)) for c in coordinators]
                    elif op < 0.65:
                        ids = [x["id"] for x in coordinators[0].state["leases"]
                               + coordinators[0].state["queue"]]
                        if ids:
                            victim = rng.choice(ids)
                            replies = [c.handle({"op": "release", "id": victim}) for c in coordinators]
                    else:
                        dt = rng.choice([2, 4, 15, 40])
                        env["now"] += dt
                        env["mono"] += dt
                        for pid in pids:
                            env["cpu"][pid] += rng.uniform(0, 6) * dt
                        env["busy"] = rng.uniform(0, 12)
                        for c in coordinators:
                            c.tick()
                    states = [([x["id"] for x in c.state["leases"]], [x["id"] for x in c.state["queue"]])
                              for c in coordinators]
                    self.assertEqual(states[0], states[1], f"seed {seed} step {step}")


RAM = 62 * GIB  # a 62 GB machine, as the owner's Linux one
FLOOR = ac.RESERVE_MEM + int(ac.MEM_MARGIN * RAM)


def growth_model(size, use, peak, ramping, expected=None):
    """Independent restatement of the growth allowance, for the simulations."""
    use, peak = min(use, size), min(max(peak, use), size)
    left = size - peak
    if expected is not None and peak <= expected:
        return peak - use + max(min(left, ac.MEM_GROWTH_FLOOR), min(expected, size) - peak)
    share = left if ramping else max(min(left, ac.MEM_GROWTH_FLOOR), int(ac.MEM_GROWTH * left))
    return peak - use + share


class MeasuredMemoryTest(unittest.TestCase):
    """Memory admission by measured use, with simulated measurements."""

    @classmethod
    def setUpClass(cls):
        # Leases and requests need live pids; each stands for one job.
        cls.procs = [subprocess.Popen(["sleep", "3600"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     for _ in range(32)]

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs:
            proc.kill()
            proc.wait()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"now": 100000.0, "mono": 1000.0, "base": 8 * GIB, "use": {}, "busy": 0.0}
        for name, key in (("time", "now"), ("monotonic", "mono")):
            patcher = patch(f"time.{name}", new=lambda key=key: self.env[key])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.c = self.make()

    def make(self, measured=True, state="state.json"):
        env = self.env
        return ac.Coordinator(
            Path(self.tmp.name, state), history_file=Path(self.tmp.name, f"{state}.history"),
            measure=lambda: {"vms": [], "busy": env["busy"],
                             "available": RAM - env["base"] - sum(env["use"].values())},
            cap=lambda: (64.0, RAM - ac.RESERVE_MEM),
            footprint=lambda pid: env["use"].get(pid, 0), cpu_seconds=lambda pid: None,
            tree_pids=lambda pid: {pid}, scope_memory=lambda pid: None, measured_mem=measured,
        )  # fmt: skip

    def pid(self, n):
        return self.procs[n].pid

    def ask(self, n, mem, held=False, label=None, c=None, **extra):
        return (c or self.c).handle({
            "op": "acquire", "agent": f"a{n}", "pid": self.pid(n), "cores": 1, "mem": mem,
            "label": label or f"job {n}", "expect": 3600, **({"running": True} if held else {}),
            **extra})  # fmt: skip

    def advance(self, seconds, step=30, c=None):
        for _ in range(int(seconds // step)):
            self.env["now"] += step
            self.env["mono"] += step
            (c or self.c).tick()

    def owner_case(self, c):
        """Three settled leases reserving 16G each and using 5G: by
        reservation 10G of the 58G lease capacity is free."""
        for n in range(3):
            self.ask(n, 16 * GIB, held=True, c=c)
            self.env["use"][self.pid(n)] = 5 * GIB
        self.advance(ac.MEM_RAMP + 60, c=c)

    def test_settled_oversized_leases_let_more_start(self):
        self.owner_case(self.c)
        # 62 - 8 - 15 = 39G available; allowances 3 x 5.5G; less the 4G
        # reserve and a 6.2G margin: 12.3G by measured use, 10G by reservation.
        reserved_only = self.make(measured=False, state="other.json")
        self.owner_case(reserved_only)
        self.assertEqual(self.ask(3, 12 * GIB, c=reserved_only)["state"], "waiting")
        self.assertEqual(self.ask(3, 12 * GIB)["state"], "granted")
        reply = self.ask(4, GIB)  # the new lease counts at its full size
        self.assertEqual(reply["state"], "waiting")
        self.assertIn("by measured use", reply["reason"])

    def test_young_and_ramping_leases_count_in_full(self):
        for n in range(3):
            self.ask(n, 16 * GIB, held=True)
            self.env["use"][self.pid(n)] = 5 * GIB
        self.advance(60)  # young: whole remaining size reserved
        self.assertEqual(self.ask(3, 12 * GIB)["state"], "waiting")
        self.c.handle({"op": "release", "id": self.c.state["queue"][0]["id"]})
        self.advance(ac.MEM_RAMP)
        self.assertEqual(self.ask(3, 12 * GIB)["state"], "granted")
        self.c.handle({"op": "release", "id": self.c.state["leases"][-1]["id"]})
        self.env["use"][self.pid(0)] = 8 * GIB  # ramping again: its peak rose
        self.advance(60)
        self.assertEqual(self.ask(3, 11 * GIB)["state"], "waiting")
        self.c.handle({"op": "release", "id": self.c.state["queue"][0]["id"]})
        self.env["use"][self.pid(0)] = 6 * GIB
        self.advance(ac.MEM_RAMP + 60)  # settled again (its peak stays 8G)
        self.assertEqual(self.c._growth(self.c.state["leases"][0], self.env["now"]),
                         2 * GIB + 4 * GIB)  # back to its peak, then half the rest

    def owner_snapshot(self, history):
        """The owner's report: 56.8G of 57.6G reserved (three gate runs at
        18.9G), each using 8G, 21G actually available."""
        size = int(18.9 * GIB)
        if history:
            past = {"id": "old", "agent": "a0", "label": "gate", "cores": 1, "mem": size}
            for peak in (8.5, 9.0, 8.8):
                self.c._log("ended", past, run=900, code=0, mem_peak=int(peak * GIB))
        for n in range(3):
            self.ask(n, size, held=True, label="gate")
            self.env["use"][self.pid(n)] = 8 * GIB
        self.env["base"] = RAM - 21 * GIB - 24 * GIB
        self.advance(60)

    def test_label_history_counts_growth_to_its_peak(self):
        self.owner_snapshot(history=True)
        # Each may grow to 9G x 1.15 = 10.35G: 2.35G; 21 - 7.05 - 4 - 6.2 = 3.75G,
        # even though the leases are young.
        room = self.c._mem_room()[0]
        self.assertAlmostEqual(room / GIB, 21 - 3 * 2.35 - 4 - 6.2, delta=0.01)
        self.assertEqual(self.ask(3, 3 * GIB)["state"], "granted")
        # A lease that goes past its label's peaks loses the history credit.
        self.env["use"][self.pid(0)] = 11 * GIB
        self.advance(30)
        lease = self.c.state["leases"][0]
        self.assertEqual(self.c._growth(lease, self.env["now"]), int(18.9 * GIB) - 11 * GIB)

    def test_without_history_the_snapshot_gains_nothing(self):
        self.owner_snapshot(history=False)
        self.advance(ac.MEM_RAMP)  # settled: half of the 10.9G left each
        self.assertLess(self.c._mem_room()[0], 0)
        self.assertEqual(self.ask(3, 2 * GIB)["state"], "waiting")

    def test_hard_cap_unchanged(self):
        with patch.object(ac.shutil, "which", return_value="/usr/bin/systemd-run"), \
                patch.object(ac.Path, "exists", return_value=True):
            cmd = ac.limited(["true"], 2, 3 * GIB, unit="agent-coord-L7-42")
        self.assertIn(f"MemoryMax={3 * GIB}", cmd)
        self.assertIn("--unit=agent-coord-L7-42", cmd)

    def test_overcommit_limit(self):
        # Leases sized far above their use: measured room is large, but all
        # sizes together stay within MEM_OVERCOMMIT times the lease capacity.
        self.env["base"] = 4 * GIB
        for n in range(4):
            self.ask(n, 14 * GIB, held=True)
            self.env["use"][self.pid(n)] = GIB // 2
        self.advance(ac.MEM_RAMP + 60)
        cap = RAM - ac.RESERVE_MEM
        granted = 0
        for n in range(4, 20):
            if self.ask(n, 4 * GIB)["state"] == "granted":
                granted += 1
        total = sum(x["mem"] for x in self.c.state["leases"])
        self.assertLessEqual(total, ac.MEM_OVERCOMMIT * cap)
        self.assertGreater(total, cap)
        self.assertGreater(granted, 0)

    def test_reservation_only_switch_decides_as_before(self):
        c = self.make(measured=False, state="off.json")
        self.owner_case(c)
        self.assertIsNone(c._mem_room())
        self.assertEqual(c.handle({"op": "status"})["measured_mem"], None)

    def test_status_shows_reserved_and_measured(self):
        self.owner_case(self.c)
        status = self.c.handle({"op": "status"})
        self.assertEqual(status["free"]["mem"], 10 * GIB)
        m = status["measured_mem"]
        self.assertEqual(m["used"], 15 * GIB)
        self.assertEqual(m["growth"], int(16.5 * GIB))
        self.assertAlmostEqual(m["free"] / GIB, 12.3, delta=0.01)
        self.assertEqual(status["leases"][0]["mem_used"], 5 * GIB)
        self.assertEqual(status["leases"][0]["mem_peak"], 5 * GIB)

    def test_nested_lease_is_taken_out_of_the_outer_one(self):
        outer, inner = self.pid(0), self.pid(1)
        self.c.tree_pids = lambda pid: {outer, inner} if pid == outer else {pid}
        self.ask(0, 16 * GIB, held=True)
        self.ask(1, 8 * GIB, held=True)
        self.env["use"] = {outer: 7 * GIB, inner: 6 * GIB}  # outer's tree includes inner
        self.c.tick()
        uses = [self.c._mem_use[x["id"]] for x in self.c.state["leases"]]
        self.assertEqual(uses, [GIB, 6 * GIB])

    def test_ended_event_records_peaks_and_peaks_query(self):
        cpu = {"t": 0.0}
        self.c.cpu_seconds = lambda pid: cpu["t"]
        for run, peak in enumerate((6, 9, 8)):
            lease = self.ask(0, 16 * GIB, label="gate: tests")
            for use in (2, peak, 3):
                self.env["use"][self.pid(0)] = use * GIB
                cpu["t"] += 3 * 30
                self.advance(30)
            self.c.handle({"op": "release", "id": lease["id"], "code": 0})
            self.env["use"].clear()
        ended = [e for e in ac.read_history(0, self.c.history_file) if e["event"] == "ended"]
        self.assertEqual([e["mem_peak"] for e in ended], [6 * GIB, 9 * GIB, 8 * GIB])
        self.assertTrue(all(0 < e["cores_peak"] <= 3 for e in ended))
        found = ac.peaks(ac.read_history(0, self.c.history_file), "gate:")
        self.assertEqual(found["gate: tests"]["mem_peak_max"], 9 * GIB)
        self.assertEqual(found["gate: tests"]["suggest_mem"], math.ceil(9 * GIB * 1.15))
        self.assertEqual(found["gate: tests"]["runs"], 3)
        self.assertEqual(ac.peaks(ac.read_history(0, self.c.history_file), "other"), {})

    def test_reserved_memory_still_counts_after_ramp(self):
        self.ask(0, 16 * GIB, held=True, reserve=True)
        self.ask(1, 16 * GIB, held=True)
        for n in (0, 1):
            self.env["use"][self.pid(n)] = GIB
        self.advance(ac.MEM_RAMP + 60)
        reserved, plain = self.c.state["leases"]
        now = self.env["now"]
        self.assertEqual(self.c._growth(reserved, now), 15 * GIB)  # all of its size less its use
        self.assertLess(self.c._growth(plain, now), 15 * GIB)  # settled: only a share

    def test_reserved_lease_never_sets_a_label_ceiling(self):
        for _ in range(ac.MEM_HISTORY_RUNS + 1):
            lease = self.ask(0, 16 * GIB, label="handover", reserve=True)
            self.env["use"][self.pid(0)] = GIB // 8
            self.advance(60)
            self.c.handle({"op": "release", "id": lease["id"], "code": 0})
            self.env["use"].clear()
        ended = [e for e in ac.read_history(0, self.c.history_file) if e["event"] == "ended"]
        self.assertTrue(ended and all(e.get("reserve") and "mem_peak" in e for e in ended))
        self.assertEqual(ac.peaks(ac.read_history(0, self.c.history_file), "handover"), {})
        fresh = self.make()  # reloads the history from disk
        probe = {"agent": "a0", "label": "handover"}
        self.assertIsNone(fresh._label_ceiling(probe))
        self.assertIsNone(self.c._label_ceiling(probe))
        self.assertEqual(fresh._runs("a0", "handover"), [])

    def test_scope_memory_reads_unreclaimable_use(self):
        with tempfile.TemporaryDirectory() as root:
            scope = Path(root, "user.slice/app.slice/agent-coord-L3-99.scope")
            scope.mkdir(parents=True)
            (scope / "memory.current").write_text(f"{10 * GIB}\n")
            (scope / "memory.stat").write_text(
                f"anon {6 * GIB}\nfile {4 * GIB}\nshmem {GIB}\nslab_reclaimable {GIB // 2}\n")
            (scope / "memory.swap.current").write_text("0\n")
            (scope / "memory.peak").write_text(f"{12 * GIB}\n")
            groups = {1: "/user.slice/app.slice/agent-shell.scope",
                      2: "/user.slice/app.slice/agent-coord-L3-99.scope"}
            with patch.object(ac, "CGROUP_ROOT", Path(root)), \
                    patch.object(ac, "_tree_pids", lambda pid: iter([1, 2])), \
                    patch.object(ac, "_cgroup_of", groups.get):
                use, peak = ac.scope_memory(1)
        self.assertEqual(use, 10 * GIB - 3 * GIB - GIB // 2)
        self.assertEqual(peak, 12 * GIB)

    # -- random workloads

    # Job kinds for the simulations: (label, size GiB, true peak as a share
    # of the size). Most ask for 1.4-3x their peak, as agents' leases do; one
    # kind ramps to its full size.
    KINDS = (("unit tests", 8, 0.4), ("gate", 16, 0.55), ("build", 12, 0.35),
             ("align", 24, 0.6), ("to cap", 8, 1.0), ("big gate", 24, 0.4))

    ARRIVAL = float(os.environ.get("AGENT_COORD_SIM_ARRIVAL", 0.12))  # per 30 s step

    def simulate(self, seed, measured, in_model=True, steps=480, step=30):
        """A random stream of jobs on the 62 GB machine. Each job's peak
        varies around its kind's (0.9-1.05x, capped at its size); it ramps up
        linearly for 30 s - 8 min, holds just below its peak and ends after
        5-40 min. in_model: after every admission, each running lease grows
        by at most its growth allowance at that moment (a lease that would
        grow further is held there and counted as clipped, a misprediction).
        Returns statistics; the bounds are checked at every admission and
        step."""
        import random
        rng = random.Random(seed)
        self.env.update(now=100000.0, mono=1000.0, use={}, base=rng.choice([4, 6, 8, 10, 12]) * GIB)
        c = self.make(measured=measured, state=f"sim-{seed}-{measured}-{in_model}.json")
        free_pids = list(range(len(self.procs)))
        jobs, stats = {}, {"done": 0, "wait": 0.0, "started": 0, "clipped": 0,
                           "min_free": RAM, "over_cap": 0, "asked": 0}  # fmt: skip
        ceiling: dict[str, int] = {}

        def call(fn):
            before = {x["id"] for x in c.state["leases"]}
            result = fn()
            new = {x["id"] for x in c.state["leases"]} - before
            if new:
                admitted(new)
            return result

        def admitted(new):
            now = self.env["now"]
            sizes = sum(x["mem"] for x in c.state["leases"])
            free = RAM - self.env["base"] - sum(self.env["use"].values())
            allow = 0
            for x in c.state["leases"]:
                if x["id"] in new:
                    ceiling[x["id"]] = x["mem"]
                    stats["started"] += 1
                    stats["wait"] += now - x["asked"]
                    continue
                rose = c._mem_step.get(x["id"], (0, -math.inf))[1]
                ramping = (x["id"] not in c._mem_use or now - x["granted"] < ac.MEM_RAMP
                           or now - rose < ac.MEM_RAMP)  # fmt: skip
                g = growth_model(x["mem"], c._mem_use.get(x["id"], 0),
                                 c._mem_peak.get(x["id"], 0), ramping,
                                 c._label_ceiling(x) if x["id"] in c._mem_use else None)
                allow += g
                ceiling[x["id"]] = self.env["use"].get(x["pid"], 0) + g
            added = sum(x["mem"] for x in c.state["leases"] if x["id"] in new)
            reserved_ok = sizes <= RAM - ac.RESERVE_MEM
            measured_ok = (free - allow - added >= FLOOR
                           and sizes <= ac.MEM_OVERCOMMIT * (RAM - ac.RESERVE_MEM))
            self.assertTrue(reserved_ok or (measured and measured_ok),
                            f"seed {seed} measured={measured}: admitted past the bound: "
                            f"sizes {sizes / GIB:.2f} free {free / GIB:.2f} allow {allow / GIB:.2f} "
                            f"added {added / GIB:.2f} new {new}")

        for _ in range(steps):
            if free_pids and rng.random() < self.ARRIVAL:
                n = free_pids.pop(rng.randrange(len(free_pids)))
                label, size, share = rng.choice(self.KINDS)
                job = {"n": n, "size": size * GIB, "ramp": rng.uniform(30, 480),
                       "peak": min(size * GIB, int(size * GIB * share * rng.uniform(0.9, 1.05))),
                       "run": rng.uniform(300, 2400)}  # fmt: skip
                jobs[self.procs[n].pid] = job
                stats["asked"] += 1
                call(lambda: self.ask(n, job["size"], c=c, label=label, expect=job["run"]))
            self.env["now"] += step
            self.env["mono"] += step
            held = {x["pid"]: x for x in c.state["leases"]}
            for pid, job in list(jobs.items()):
                lease = held.get(pid)
                if lease is None:
                    continue
                age = self.env["now"] - lease["granted"]
                if age >= job["run"]:
                    self.env["use"].pop(pid, None)
                    ceiling.pop(lease["id"], None)
                    call(lambda: c.handle({"op": "release", "id": lease["id"], "code": 0}))
                    free_pids.append(job["n"])
                    del jobs[pid]
                    stats["done"] += 1
                    continue
                want = int(job["peak"] * min(1.0, age / job["ramp"]) * (1 - rng.uniform(0, 0.03)))
                if in_model and want > ceiling.get(lease["id"], job["size"]):
                    stats["clipped"] += 1
                    want = ceiling[lease["id"]]
                self.env["use"][pid] = want
            call(c.tick)
            free = RAM - self.env["base"] - sum(self.env["use"].values())
            stats["min_free"] = min(stats["min_free"], free)
            if sum(x["mem"] for x in c.state["leases"]) > RAM - ac.RESERVE_MEM:
                stats["over_cap"] += 1
                if in_model:
                    # Overcommitted: the last admission was by measured use.
                    self.assertGreaterEqual(free, FLOOR, f"seed {seed}: below the floor")
        return stats

    def sim_totals(self, in_model):
        totals = {}
        for seed in range(int(os.environ.get("AGENT_COORD_SIM_SEEDS", 12))):
            for measured in (False, True):
                agg = totals.setdefault(measured, {})
                for key, value in self.simulate(seed, measured, in_model).items():
                    agg[key] = (min(agg.get(key, value), value) if key == "min_free"
                                else agg.get(key, 0) + value)  # fmt: skip
        report = {("measured" if m else "reservation"): {
            "asked": t["asked"], "started": t["started"], "done": t["done"],
            "mean_wait_min": round(t["wait"] / max(t["started"], 1) / 60, 2),
            "min_free_gib": round(t["min_free"] / GIB, 2),
            "overcommitted_steps": t["over_cap"], "clipped": t["clipped"]}
            for m, t in totals.items()}  # fmt: skip
        if os.environ.get("AGENT_COORD_SIM_REPORT"):
            print(f"\nin_model={in_model}\n" + json.dumps(report, indent=2), file=sys.stderr)
        return totals[False], totals[True]

    def test_random_workloads_keep_the_floor_and_start_more(self):
        off, on = self.sim_totals(in_model=True)
        self.assertGreater(on["started"], off["started"])
        self.assertGreater(on["done"], off["done"])
        self.assertGreater(on["over_cap"], 0)  # some admissions were by measured use

    def test_random_workloads_beyond_the_model(self):
        """Jobs grow as they like up to their caps (no clipping, so a job
        whose peak varies past its label's history is not held back): on
        these workloads free memory still never reaches the reserve."""
        off, on = self.sim_totals(in_model=False)
        self.assertGreater(on["done"], off["done"])
        self.assertGreater(on["min_free"], ac.RESERVE_MEM)


class FootprintTest(unittest.TestCase):
    def test_tree_footprint_counts_descendants(self):
        child = subprocess.Popen([sys.executable, "-c", "x = bytearray(64 * 1024**2); input()"],
                                 stdin=subprocess.PIPE)  # fmt: skip
        try:
            own = ac._footprint(os.getpid())
            for _ in range(100):
                if ac._footprint(child.pid) > 60 * 1024**2:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(ac.tree_footprint(os.getpid()), own + 60 * 1024**2)
        finally:
            child.stdin.close()
            child.kill()
            child.wait()

    def test_tree_footprint_of_a_dead_pid_is_zero(self):
        self.assertEqual(ac.tree_footprint(dead_pid()), 0)

    def test_tree_cpu_counts_live_children_and_reaped_children_where_supported(self):
        pid = os.getpid()
        before = ac.tree_cpu_seconds(pid)
        code = ("import time; start = time.process_time()\n"
                "while time.process_time() - start < 0.1: pass\n"
                "print('ready', flush=True); input()")
        with subprocess.Popen([sys.executable, "-c", code], text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            active = ac.tree_cpu_seconds(pid)
            child.communicate("\n", timeout=5)
        reaped = ac.tree_cpu_seconds(pid)
        self.assertGreater(active - before, 0.07)
        if ac.DARWIN or ac.WINDOWS:
            self.assertLess(reaped, active)
        else:
            self.assertGreaterEqual(reaped + 1e-6, active)  # sums in another order


class ArchiveTest(unittest.TestCase):
    M = ("/mnt/nas",)

    def test_outputs_and_workdir_on_the_archive_are_found(self):
        cmd = ["wgsextract", "realign", "--input", "/mnt/nas/ref/x.cram",
               "--outdir", "/mnt/nas/out", "-T=/mnt/nas/tmp"]  # fmt: skip
        found = ac.archive_writes(cmd, "/mnt/nas/work", self.M)
        self.assertEqual(len(found), 3)
        self.assertIn("--outdir /mnt/nas/out", found[1])

    def test_reading_from_the_archive_is_fine(self):
        cmd = ["bwa", "mem", "/mnt/nas/ref/hs38.fa", "r.fq", "-o", "/home/x.sam"]
        self.assertEqual(ac.archive_writes(cmd, "/home/mac", self.M), [])
        self.assertEqual(ac.archive_writes(["ls"], "/mnt/nasty", self.M), [])


class LaunchdTest(unittest.TestCase):
    def test_bootstraps_a_private_named_job_and_can_resolve_its_pid(self):
        with tempfile.TemporaryDirectory() as root:
            launch = {}
            overrides = {"AGENT_COORD_RUNTIME_DIR": str(Path(root, "runtime")),
                         "AGENT_COORD_STATE_DIR": str(Path(root, "state"))}

            def bootstrap(argv, **kwargs):
                with Path(argv[-1]).open("rb") as f:
                    launch.update(plistlib.load(f))

            with (patch.object(ac.sys, "platform", "darwin"),
                  patch.object(ac.os, "getuid", return_value=501, create=True),
                  patch.dict(os.environ, {**overrides, "UNRELATED_ENV": "do not forward"}),
                  patch.object(ac, "STATE_FILE", Path(root, "state.json")),
                  patch.object(ac.subprocess, "run", side_effect=bootstrap),
                  patch.object(ac.subprocess, "check_output", return_value="state = running\npid = 42\n")):
                self.assertEqual(ac.main(["--as", "driver", "run", "--io", "--label=-label",
                                          "--detach", "--unit", "bench", "--allow-archive",
                                          "--", "driver", "text with spaces"]), 0)
                self.assertEqual(ac.unit_pid("bench"), 42)
            self.assertEqual(launch["Label"], "bench")
            self.assertEqual(launch["ProgramArguments"][-2:], ["driver", "text with spaces"])
            self.assertIn("--label=-label", launch["ProgramArguments"])
            self.assertEqual(launch["WorkingDirectory"], os.getcwd())
            self.assertEqual(launch["EnvironmentVariables"], overrides)
            self.assertEqual(Path(launch["StandardOutPath"]), Path(root, "units", "bench.log"))
            self.assertFalse(list(Path(root, "units").glob("*.plist")))


class ServiceTest(unittest.TestCase):
    """The CLI against a real service on a temporary socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = Path(self.tmp.name, ac.SOCKET.name)
        self.server = subprocess.Popen(
            [sys.executable, "-c",
             ("import agent_coord as ac, pathlib, sys; "
             "ac.serve(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), "
             "measure=lambda: {'vms': [], 'busy': 0.0, 'available': 64 * 1024**3}, "
             "cap=lambda: (8.0, 16 * 1024**3))"),
             str(self.sock), str(Path(self.tmp.name, "state.json"))],
            cwd=Path(__file__).parent, stdout=subprocess.DEVNULL,
        )  # fmt: skip
        for _ in range(100):
            if self.sock.exists():
                break
            time.sleep(0.05)

    def tearDown(self):
        self.server.terminate()
        self.server.wait(10)
        self.tmp.cleanup()

    def cli(self, *args):
        code = (f"import agent_coord as ac, pathlib, sys; "
                f"ac.SOCKET = pathlib.Path({str(self.sock)!r}); "
                f"ac.HISTORY_FILE = pathlib.Path({str(Path(self.tmp.name, 'history.jsonl'))!r}); "
                f"sys.exit(ac.main(sys.argv[1:]))")  # fmt: skip
        return subprocess.run([sys.executable, "-c", code, *args],
                              cwd=Path(__file__).parent,
                              capture_output=True, text=True, timeout=60, check=False)  # fmt: skip

    def test_run_executes_capped_and_releases(self):
        result = self.cli("--as", "t", "run", "--cores", "1", "--mem", "256M",
                          "--label", "echo", "--", sys.executable, "-c",
                          "import os; print(os.environ['COMPUTE_LEASE_CORES'])")  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        status = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(status["leases"], [])

    def test_phase_cli_finds_enclosing_driver_after_directory_change(self):
        cli = str(Path(ac.__file__).resolve())
        worker = (
            "import os, subprocess, sys; "
            "os.chdir(sys.argv[2]); "
            "subprocess.run([sys.executable, sys.argv[1], 'phase', '--exclusive-lock', 'gpu'], check=True); "
            "subprocess.run([sys.executable, sys.argv[1], 'phase', '--release', 'gpu'], check=True)"
        )
        with patch.dict(os.environ, {"AGENT_COORD_RUNTIME_DIR": str(self.sock.parent)}):
            result = self.cli("--as", "driver", "run", "--cores", "1", "--mem", "128M",
                              "--label", "phases", "--", sys.executable, "-c",
                              worker, cli, str(self.sock.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in self.cli("history", "--json").stdout.splitlines()]
        self.assertEqual([e["event"] for e in events if e.get("resource") == "gpu"],
                         ["exclusive-started", "exclusive-ended"])

    def test_io_defaults_and_history_round_trip(self):
        self.assertEqual(self.cli("check", "--io").returncode, 0)
        self.assertNotEqual(self.cli("check", "--mem", "512M").returncode, 0)
        run = self.cli("run", "--io", "--label", "download", "--", sys.executable,
                       "-c", "import os; print(os.environ['COMPUTE_LEASE_CORES'])")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "1")
        events = [json.loads(line) for line in self.cli("history", "--json").stdout.splitlines()]
        self.assertTrue(events)
        self.assertTrue(all(e["io"] and e["mem"] == GIB // 2 for e in events))
        hold = self.cli("hold", "--io", "--label", "stream", "--pid", str(os.getpid()))
        self.assertEqual(hold.returncode, 0, hold.stderr)
        status = self.cli("status")
        self.assertIn("1-core cap", status.stdout)
        self.assertIn("stream io", status.stdout)

    def test_run_returns_the_commands_exit_code(self):
        result = self.cli("run", "--cores", "1", "--mem", "64M", "--label", "x",
                          "--", sys.executable, "-c", "raise SystemExit(3)")  # fmt: skip
        self.assertEqual(result.returncode, 3)
        history = self.cli("history", "--json")
        events = [json.loads(line) for line in history.stdout.splitlines()]
        self.assertEqual([e["event"] for e in events], ["requested", "started", "ended"])
        self.assertEqual(events[-1]["code"], 3)
        self.assertIn("ended", self.cli("history", "--since", "1h").stdout)

    def test_expect_cli_round_trip(self):
        result = self.cli("run", "--cores", "1", "--mem", "64M", "--label", "short",
                          "--expect", "10m", "--", sys.executable, "-c", "pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in self.cli("history", "--json").stdout.splitlines()]
        self.assertTrue(all(e["expect"] == 600 for e in events))
        self.assertTrue(all(e["expect_source"] == "explicit" for e in events))
        result = self.cli("check", "--cores", "1", "--mem", "64M", "--expect", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive", result.stderr)

    def test_run_with_exclusive_lock_and_headroom(self):
        result = self.cli("run", "--cores", "1", "--mem", "64M", "--label", "timed",
                          "--exclusive-lock", "gpu", "--headroom", "2",
                          "--", sys.executable, "-c", "pass")  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli("check", "--cores", "1", "--mem", "64M",
                                  "--exclusive-lock", "gpu").returncode, 0)  # fmt: skip
        both = self.cli("run", "--cores", "1", "--mem", "64M", "--label", "x",
                        "--exclusive", "--exclusive-lock", "gpu", "--",
                        sys.executable, "-c", "pass")  # fmt: skip
        self.assertNotEqual(both.returncode, 0)

    def test_messages_round_trip(self):
        self.assertEqual(self.cli("--as", "a", "send", "--to", "b", "hello").returncode, 0)
        self.assertIn("a -> b: hello", self.cli("--as", "b", "inbox").stdout)
        self.assertIn("no new messages", self.cli("--as", "b", "inbox").stdout)

    def test_unreachable_service_is_explained(self):
        self.server.terminate()
        self.server.wait(10)
        result = self.cli("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(ac.START_HINT, result.stderr)


@unittest.skipUnless(ac.WINDOWS, "Windows backend")
class WindowsTest(unittest.TestCase):
    def test_alive_checks_without_killing(self):
        # os.kill(pid, 0) would TerminateProcess the target on Windows.
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE)  # fmt: skip
        try:
            self.assertTrue(ac.alive(child.pid))
            self.assertTrue(ac.alive(child.pid))
            self.assertIsNone(child.poll())
        finally:
            child.stdin.close()
            child.wait(10)
        self.assertFalse(ac.alive(child.pid))
        self.assertFalse(ac.alive(0))

    def test_measurements(self):
        sample = ac.measure(0.2)
        self.assertGreater(sample["available"], 0)
        self.assertGreater(ac.mem_total(), sample["available"] // 2)
        self.assertGreaterEqual(sample["busy"], 0)
        self.assertIsNotNone(ac._cpu_seconds(os.getpid()))
        self.assertTrue(ac.mount_of(__file__).endswith("\\"))
        self.assertGreater(ac.disk_free(ac.mount_of(__file__)), 0)

    def test_tick_counts_cpu_from_a_live_leased_child(self):
        with tempfile.TemporaryDirectory() as root:
            coord = ac.Coordinator(
                Path(root, "state.json"),
                measure=lambda: {"vms": [], "busy": 0.0, "available": 64 * GIB},
            )
            lease = coord.handle({"op": "acquire", "pid": os.getpid(), "label": "sample",
                                  "io": True, "running": True})["id"]
            worker = ("import time; input(); end = time.process_time() + .35\n"
                      "while time.process_time() < end: pass\n"
                      "print('ready', flush=True); input()")
            with subprocess.Popen(
                [sys.executable, "-c", worker],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            ) as child:
                try:
                    coord.tick()
                    child.stdin.write("\n")
                    child.stdin.flush()
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    coord.tick()
                    status = coord.handle({"op": "status"})
                    self.assertEqual(status["leases"][0]["id"], lease)
                    self.assertGreater(status["leases"][0]["measured_cores"], .05)
                finally:
                    child.terminate()

    def test_job_object_caps_memory_and_reports_cores(self):
        code = ("import os, sys\n"
                "assert os.environ['COMPUTE_LEASE_CORES'] == '1'\n"
                "try:\n    x = bytearray(int(sys.argv[1]))\nexcept MemoryError:\n    sys.exit(7)\n")
        env = dict(os.environ, COMPUTE_LEASE_CORES="1")
        for size, expected in ((16 * 1024**2, 0), (512 * 1024**2, 7)):
            child, job = ac.spawn_limited([sys.executable, "-c", code, str(size)], 1, 128 * 1024**2, env)
            self.assertIsNotNone(job)
            self.assertEqual(child.wait(30), expected)

    def test_archive_paths_from_the_environment(self):
        with tempfile.TemporaryDirectory() as nas, patch.dict(os.environ, {"AGENT_COORD_ARCHIVE": nas}):
            mounts = ac.archive_mounts()
            self.assertEqual(len(ac.archive_writes(["x", "-o", os.path.join(nas, "out")], "C:\\", mounts)), 1)
            self.assertEqual(ac.archive_writes(["x", "-o", nas + "ty"], "C:\\", mounts), [])


@unittest.skipUnless(ac.WINDOWS, "Windows backend")
class WindowsServiceTest(ServiceTest):
    def test_named_detach_survives_launcher_exit(self):
        stop = Path(self.tmp.name, "stop")
        worker = (
            "from pathlib import Path; import sys, time; "
            "print('driver started', flush=True); "
            "print('driver stderr', file=sys.stderr, flush=True); "
            "deadline = time.monotonic() + 10\n"
            "while time.monotonic() < deadline and not Path(sys.argv[1]).exists(): time.sleep(.1)"
        )
        with patch.dict(os.environ, {"AGENT_COORD_RUNTIME_DIR": self.tmp.name,
                                     "AGENT_COORD_STATE_DIR": self.tmp.name}):
            result = self.cli("--as", "driver", "run", "--detach", "--unit", "test-driver",
                              "--cores", "1", "--mem", "128M", "--label", "detached",
                              "--", sys.executable, "-c", worker, str(stop))
        try:
            self.assertEqual(result.returncode, 0, result.stderr)
            for _ in range(50):
                status = json.loads(self.cli("status", "--json").stdout)
                if any(x.get("run_unit") == "test-driver" for x in status["leases"]):
                    break
                time.sleep(.1)
            else:
                self.fail("detached wrapper left no lease")
        finally:
            stop.touch()
        for _ in range(50):
            if not json.loads(self.cli("status", "--json").stdout)["leases"]:
                break
            time.sleep(.1)
        else:
            self.fail("detached lease was not released on exit")
        log = Path(self.tmp.name, "units", "test-driver.log")
        self.assertIn("driver started", log.read_text())
        self.assertIn("driver stderr", log.read_text())

    def test_one_server_per_endpoint(self):
        second = subprocess.run(
            [sys.executable, "-c", "import agent_coord as ac, pathlib, sys; ac.serve(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))",
             str(self.sock), str(Path(self.tmp.name, "other.json"))],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=30)  # fmt: skip
        self.assertIn("already serving", second.stdout)
        self.assertEqual(self.cli("status").returncode, 0)

    def test_requests_need_the_token(self):
        endpoint = json.loads(self.sock.read_text())
        bad = Path(self.tmp.name, "bad.json")
        bad.write_text(json.dumps({**endpoint, "token": "wrong"}))
        with self.assertRaises(SystemExit) as caught:
            ac.call({"op": "status"}, bad)
        self.assertIn("bad token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
