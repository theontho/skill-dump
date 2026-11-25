"""Tests for agent-coord: python3 -m unittest -v test_agent_coord"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_coord as ac

GIB = 1024**3
# Tests that create real (transient, uniquely named) systemd user scopes run
# only when AGENT_COORD_SYSTEMD_TESTS=1; by default a seam stands in for them.
REAL_SCOPES = os.environ.get("AGENT_COORD_SYSTEMD_TESTS") == "1"


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
        coordinator = ac.Coordinator(
            self.state,
            measure=lambda: {"vms": list(self.vms), "busy": self.busy,
                             "available": self.available},
            cap=lambda: (8.0, 16 * GIB),
            # No real CPU readings: tests that need lease use inject it.
            cpu_seconds=lambda pid: None,
        )
        return coordinator

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
        # Two timing leases, the inner one in the outer's tree: the outer
        # tree's measurement includes the inner one (6 + 2 = 8). Per
        # outermost tree, its use up to both sizes comes off, once.
        outer = os.getpid()
        self.timed(size=6, headroom=0, pid=outer)
        inner = self.ask(2, agent="perf", exclusive_locks=["nas"], pid=os.getppid())
        self.age(inner, ac.WARMUP + 1, "granted")
        self.measure_leases({outer: 8.0, os.getppid(): 2.0}, busy=9.0,
                            trees={outer: {outer, os.getppid()}})
        # 6 + 2 reserved; 9 busy less the tree's 8: 1 of other work.
        self.assertAlmostEqual(self.free_cores(), 3.0, places=1)

    def test_process_trees_are_walked_only_for_leases_counted_once(self):
        calls = []
        self.configure(cores=12, mem=58, footprint=lambda pid: GIB)  # in use: no IO hint
        for pid in (os.getpid(), os.getppid()):
            self.age(self.ask(3, pid=pid), ac.WARMUP + 1, "granted")
        self.measure_leases({os.getpid(): 1.0, os.getppid(): 1.0})
        self.c.tree_pids = lambda pid: calls.append(pid) or {pid}
        self.c.tick()
        self.assertEqual(calls, [])  # ordinary leases only: no walk
        self.ask(2, agent="perf", exclusive_locks=["gpu"])
        self.c.tick()
        self.assertTrue(calls)
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
        self.assertIn("of disk free on /", second["reason"])

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


class ElasticTest(unittest.TestCase):
    """MIN:MAX leases, preemption and fair shares (in-process
    coordinator with injected clocks, measurements and scope resizing;
    nothing reaches systemd and nothing is signalled)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name, "state.json")
        self.cores = 8.0
        self.busy = 0.0
        self.outside = 0.0  # load not leased through agent-coord
        self.now = 100000.0
        self.mono = 1000.0
        self.cpu: dict[int, float] = {}
        self.children = []
        self.check_grants = True
        self.resized: list[tuple[str, float]] = []
        self.failing: set[str] = set()  # scopes whose resize fails
        self.c = ac.Coordinator(
            self.state,
            measure=lambda: {"vms": [], "busy": self.busy, "available": 64 * GIB},
            cap=lambda: (self.cores, 64 * GIB),
            cpu_seconds=lambda pid: self.cpu.get(pid),
            footprint=lambda pid: 0,
            tree_pids=lambda pid: {pid},
            resize=self.resize,
        )
        patchers = [patch.object(ac.time, "time", side_effect=lambda: self.now),
                    patch.object(ac.time, "monotonic", side_effect=lambda: self.mono)]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        for child in self.children:
            child.kill()
            child.wait()
        self.tmp.cleanup()

    def resize(self, scope, cores):
        """The seam for systemctl set-property: records, or fails."""
        self.resized.append((scope, cores))
        return scope not in self.failing

    def pid(self) -> int:
        """A real, live process of our own to hold a lease (only ever probed
        with signal 0 by the coordinator's liveness check)."""
        child = subprocess.Popen(["sleep", "300"])
        self.children.append(child)
        return child.pid

    def ask(self, cores, high=None, project="p", agent=None, **extra):
        req = {"op": "acquire", "agent": agent or project, "pid": extra.pop("pid", None) or self.pid(),
               "cores": cores, "mem": extra.pop("mem", GIB), "label": f"{project} job",
               "project": project, "repo": extra.pop("repo", project),
               "client": extra.pop("client", ac.CLIENT_VERSION),
               "resizable": extra.pop("resizable", True), **extra}
        if high is not None:
            req["max_cores"] = high
        return self.c.handle(req)

    def lease(self, reply) -> dict:
        return next(x for x in self.c.state["leases"] + self.c.state["queue"]
                    if x["id"] == reply["id"])

    def state_of(self, reply) -> str:
        return self.c._ticket(reply["id"])["state"]

    def reason(self, reply) -> str:
        return self.c._ticket(reply["id"]).get("reason", "")

    def sizes(self, *replies):
        return tuple(self.lease(r)["cores"] for r in replies)

    def ready(self):
        """What each MIN:MAX client does once its command runs in its scope:
        report it (a lease is resized only after that)."""
        for x in list(self.c.state["leases"]):
            if x.get("elastic") and not x.get("scope_ready"):
                self.c.handle({"op": "poll", "id": x["id"], "pid": x["pid"], "scope_ready": True})

    def later(self, seconds=10):
        """Let the running leases' clients report their scopes, and time pass."""
        self.ready()
        self.now += seconds

    def assert_grants_fit(self):
        """The invariant: sizes in force add up to at most the machine. (Not
        checked where, as before this change, fixed sizes and MINs were
        admitted on measured use of leases using less than they hold.)"""
        if self.check_grants:
            total = sum(self.c._size(x) for x in self.c.state["leases"] if not x.get("io"))
            self.assertLessEqual(total, self.cores + 1e-9,
                                 [(x["id"], x["cores"]) for x in self.c.state["leases"]])

    def advance(self, seconds, usage=None):
        """Let seconds pass, with each lease (by id) using usage[id] cores
        (default: all its cap allows), and tick."""
        step = min(seconds, ac.TICK)
        while seconds > 0:
            self.ready()
            use = {x["id"]: (usage or {}).get(x["id"], self.c._size(x))
                   for x in self.c.state["leases"]}
            self.busy = sum(use.values()) + self.outside  # smoothed by tick
            self.now += step
            self.mono += step
            for x in self.c.state["leases"]:
                self.cpu[x["pid"]] = self.cpu.get(x["pid"], 0.0) + use[x["id"]] * step
            self.c.tick()
            self.assert_grants_fit()
            seconds -= step

    def history(self):
        return ac.read_history(0, Path(self.tmp.name, "history.jsonl"))

    # -- request form

    def test_cores_parser(self):
        self.assertEqual(ac.parse_cores("4"), (4.0, None))
        self.assertEqual(ac.parse_cores("2:8"), (2.0, 8.0))
        self.assertEqual(ac.parse_cores("1.5:3"), (1.5, 3.0))
        self.assertEqual(ac.parse_cores("3:3"), (3.0, None))
        for bad in ("0", "8:2", "2:", ":4", "x", "-1:2"):
            with self.subTest(bad=bad), self.assertRaises(ac.argparse.ArgumentTypeError):
                ac.parse_cores(bad)

    def test_cli_sends_min_max_and_refuses_ranges_for_timing_runs(self):
        with patch.object(ac, "call", return_value={"ok": True, "cores": 6.0}) as call, \
                patch("sys.stdout", io.StringIO()):
            self.assertEqual(ac.main(["--as", "x", "check", "--cores", "2:8", "--mem", "1G",
                                      "--project", "proj"]), 0)
            sent = call.call_args.args[0]
            self.assertEqual((sent["cores"], sent["max_cores"], sent["project"]), (2.0, 8.0, "proj"))
            for extra in (["--exclusive-lock", "gpu"], ["--headroom", "1"], ["--exclusive"], ["--io"]):
                with self.subTest(extra=extra), self.assertRaises(SystemExit), \
                        patch("sys.stderr"):
                    ac.main(["check", "--cores", "2:8", "--mem", "1G", *extra])
            with self.assertRaises(SystemExit), patch("sys.stderr"):
                ac.main(["hold", "--cores", "2:8", "--mem", "1G", "--label", "x", "--pid", "1"])
        self.assertIn("error", self.ask(2, 8, exclusive_locks=["gpu"]))
        self.assertIn("error", self.ask(2, 8, headroom=1))
        self.assertIn("error", self.ask(4, 2))

    def test_hold_takes_the_project_from_the_held_process(self):
        with patch.object(ac, "call", return_value={"id": "L1"}) as call, \
                patch.object(ac, "default_project", return_value="theirs") as project, \
                patch("sys.stdout", io.StringIO()):
            ac.main(["--as", "x", "hold", "--cores", "1", "--mem", "1G", "--label", "x",
                     "--pid", str(os.getpid())])
        self.assertEqual(project.call_args.args[1], os.getcwd())  # /proc/PID/cwd of the held pid
        self.assertEqual(call.call_args.args[0]["project"], "theirs")

    def test_scope_names_are_the_services(self):
        a = self.ask(2, 8)
        lease = self.lease(a)
        self.assertEqual(lease["scope"], f"agent-coord-{a['id']}-{lease['pid']}.scope")
        self.assertEqual(self.c._ticket(a["id"])["scope"], lease["scope"])
        self.assertTrue(self.c._ticket(a["id"])["elastic"])
        self.c.handle({"op": "release", "id": a["id"]})
        fixed = self.ask(2, 8, resizable=False)  # no systemd scope: sized once, then fixed
        self.assertNotIn("scope", self.lease(fixed))
        self.assertFalse(self.c._ticket(fixed["id"])["elastic"])

    # -- large timing runs, and the elastic pool beside a timing run

    def test_timing_runs_of_any_size_are_accepted_as_on_main(self):
        # Review 4, finding 4: no reserve, so a large timing run is admitted
        # (or waits) exactly as before.
        self.cores = 12.0
        self.assertEqual(self.ask(11, project="t", exclusive_locks=["gpu"], headroom=1)["state"],
                         "granted")
        self.c.state["leases"].clear()
        self.c.state["locks"].clear()
        self.assertEqual(self.ask(12, project="t", exclusive_locks=["gpu"])["state"], "granted")
        self.assertIn("error", self.ask(12, project="t", headroom=1))  # more than the machine
    def timing_run_start(self, names: bool) -> float:
        """When a timing run waiting behind a 6-core holder starts, with or
        without other projects (or one agent with many --project names)
        re-asking for 2 cores every 20 s meanwhile (review 2, finding 2)."""
        self.c.state["leases"].clear()
        self.c.state["queue"].clear()
        self.c.state["locks"].clear()
        self.cores = 12.0
        start = self.now
        holder = self.ask(6, project="h", expect=600)
        timed = self.ask(8, project="t", exclusive_locks=["gpu"], headroom=1)
        self.assertEqual(timed["state"], "waiting")
        for round_ in range(6):
            if names and self.state_of(timed) == "waiting":
                asks = [self.ask(2, project=f"me-{round_}-{i}", agent="me", expect=60)
                        for i in range(4)]
                self.assertEqual({self.state_of(r) for r in asks}, {"waiting"})
                for r in asks:
                    self.c.handle({"op": "release", "id": r["id"]})
            self.advance(20)
            if round_ == 2:
                self.c.handle({"op": "release", "id": holder["id"]})
        started = [e["time"] for e in self.history()
                   if e["id"] == timed["id"] and e["event"] == "started"]
        if started:
            return started[-1] - start
        while self.state_of(timed) != "granted":
            self.advance(ac.TICK)
            self.assertLess(self.now - start, 3600)
        return self.now - start

    def test_many_project_names_do_not_delay_a_waiting_timing_run(self):
        plain = self.timing_run_start(names=False)
        self.assertEqual(self.timing_run_start(names=True), plain)

    def test_many_project_names_do_not_delay_a_large_fixed_head(self):
        self.cores = 12.0
        holder = self.ask(6, project="h", expect=600)
        head = self.ask(10, project="big")
        later = [self.ask(2, project=f"n{i}", expect=7200) for i in range(4)]
        # Exactly what plain EASY backfill allows (as on main): one fits
        # beside the head's projected start, the rest would delay it.
        self.assertEqual([self.state_of(r) for r in later], ["granted"] + ["waiting"] * 3)
        self.assertIn("would delay", self.reason(later[1]))
        self.c.handle({"op": "release", "id": holder["id"]})
        self.assertEqual(self.state_of(head), "granted")

    def test_no_reservation_for_projects_with_nothing_waiting(self):
        self.cores = 12.0
        self.ask(1, project="x")
        self.ask(1, project="y", io=True)
        self.assertEqual(self.ask(10, project="a")["state"], "granted")

    # -- sizing and preemption

    def test_admitted_at_min_granted_up_to_max(self):
        self.assertEqual(self.lease(self.ask(2, 64))["cores"], 8)  # MAX clamped to the machine
        self.c.state["leases"].clear()
        self.ask(5, project="p")
        self.assertEqual(self.lease(self.ask(2, 8, project="q"))["cores"], 3)
        self.assertEqual(self.ask(1, 4, project="r")["state"], "waiting")  # q is just starting

    def test_starts_at_its_fair_size_not_max_when_others_wait(self):
        self.cores = 12.0
        hog = self.ask(12, project="h", exclusive=True)
        e = self.ask(2, 12, project="p")
        w = self.ask(4, project="q")
        self.c.handle({"op": "release", "id": hog["id"]})
        self.assertEqual((self.state_of(e), self.state_of(w)), ("granted", "granted"))
        self.assertEqual(self.lease(e)["cores"], 6)  # its share beside q, leaving q's 4
        self.assertFalse([x for x in self.history() if x["event"] == "resized"])

    def test_service_resizes_the_scope_before_counting_the_cores(self):
        a = self.ask(2, 8, agent="elastic")
        self.later()
        b = self.ask(4, project="q", agent="fixed")
        self.assertEqual(b["state"], "granted")  # at once: the service capped a's scope
        self.assertEqual(self.resized, [(self.lease(a)["scope"], 4.0)])
        self.assertEqual(self.lease(a)["cores"], 4)
        resized, = [e for e in self.history() if e["event"] == "resized"]
        self.assertEqual((resized["from"], resized["cores"], resized["kind"]), (8, 4, "preempted"))
        note, = self.c.handle({"op": "inbox", "agent": "elastic"})["messages"]
        self.assertIn("was shrunk to 4 cores", note["text"])

    def test_only_scopes_it_named_are_resized(self):
        a = self.ask(2, 8)
        self.later()
        self.lease(a)["scope"] = "other-" + self.lease(a)["scope"]  # not the service's prefix
        self.assertEqual(self.ask(4, project="q")["state"], "waiting")
        self.assertEqual(self.resized, [])
        self.assertNotIn("elastic", self.lease(a))

    def test_large_fixed_request_gets_no_more_than_its_share(self):
        # Review 2, finding 3 (the supervisor's example), 12 cores.
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.advance(64)
        fixed = self.ask(10, project="b")
        self.assertEqual(fixed["state"], "waiting")
        self.assertEqual(self.lease(a)["cores"], 12)  # not shrunk for a request it cannot start
        self.assertIn("fair share (6 of 12 cores)", self.reason(fixed))
        self.assertIn("MIN:MAX", self.reason(fixed))
        self.c.handle({"op": "release", "id": fixed["id"]})
        elastic = self.ask(2, 10, project="b")
        self.assertEqual(self.state_of(elastic), "granted")
        self.assertEqual(self.sizes(a, elastic), (6, 6))
        self.c.handle({"op": "release", "id": elastic["id"]})
        self.advance(64)
        self.assertEqual(self.ask(6, project="b")["state"], "granted")  # a fixed 6 fits its share
        self.assertEqual(self.lease(a)["cores"], 6)

    def test_requests_behind_a_lock_or_memory_wait_also_preempt(self):
        # Review 2, finding 5.
        self.cores = 12.0
        a = self.ask(2, 12, project="a", mem=4 * GIB)
        self.advance(64)
        self.c.handle({"op": "lock", "agent": "owner", "name": "gpu", "reason": "x"})
        gpu = self.ask(1, project="g", locks=["gpu"])
        b = self.ask(4, project="b")
        self.assertEqual((self.state_of(gpu), self.state_of(b)), ("waiting", "granted"))
        self.assertEqual(self.lease(a)["cores"], 7)  # b's 4, and room for the GPU job's 1
        big = self.ask(1, project="m", mem=63 * GIB)  # waits for memory at the line's end
        c = self.ask(2, project="c")
        self.assertEqual((self.state_of(big), self.state_of(c)), ("waiting", "granted"))

    def test_shrinks_to_min_on_contention_and_no_further(self):
        a = self.ask(2, 8, agent="elastic")
        self.later()
        b = self.ask(4, project="q", agent="fixed")
        self.assertEqual(b["state"], "granted")
        self.assertEqual(self.lease(a)["cores"], 4)
        c = self.ask(3, project="r", agent="third")  # a would have to go below MIN
        self.assertEqual(c["state"], "waiting")
        self.assertEqual(self.lease(a)["cores"], 4)
        fourth = self.ask(2, project="s", agent="fourth")
        self.assertEqual(fourth["state"], "waiting")  # in line behind c
        self.c.handle({"op": "release", "id": c["id"]})
        self.assertEqual(self.state_of(fourth), "granted")
        self.assertEqual(self.lease(a)["cores"], 2)

    def test_fixed_exclusive_and_headroom_leases_are_never_shrunk(self):
        fixed = self.ask(3)
        timed = self.ask(2, exclusive_locks=["gpu"], headroom=1)
        elastic = self.ask(1, 4)
        self.assertEqual(self.lease(elastic)["cores"], 2)  # 8 - 3 - 2 - 1 headroom
        self.later()
        waiting = self.ask(2)
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(self.sizes(fixed, timed, elastic), (3, 2, 2))
        self.c.handle({"op": "release", "id": waiting["id"]})
        self.assertEqual(self.ask(1)["state"], "granted")  # the elastic one gave a core
        self.assertEqual(self.sizes(fixed, timed, elastic), (3, 2, 1))

    def test_grows_back_with_hysteresis(self):
        a = self.ask(2, 8)
        self.later()
        b = self.ask(4)
        self.c.handle({"op": "release", "id": b["id"]})
        self.advance(30)
        self.assertEqual(self.lease(a)["cores"], 4)  # within resize_interval
        self.advance(100)
        self.assertEqual(self.lease(a)["cores"], 7)  # 8 free less the 1-core margin
        self.assertEqual([e for e in self.history() if e["event"] == "resized"][-1]["kind"], "grew")
        self.advance(120)
        self.assertEqual(self.lease(a)["cores"], 7)

    def test_no_growth_while_a_request_waits_for_cores(self):
        a = self.ask(2, 8)
        self.later()
        self.ask(4)
        self.advance(ac.WARMUP + 5)
        self.assertEqual(self.ask(3)["state"], "waiting")  # a would need to go below MIN
        self.advance(120)
        self.assertEqual(self.lease(a)["cores"], 4)
        self.assertFalse([e for e in self.history() if e.get("kind") == "grew"])

    def test_idle_shrink_and_regrow(self):
        a = self.ask(2, 8)
        self.advance(ac.WARMUP + 4)
        self.advance(100, usage={a["id"]: 1.0})
        self.assertEqual(self.lease(a)["cores"], 8)
        self.advance(40, usage={a["id"]: 1.0})
        self.assertEqual(self.lease(a)["cores"], 2)  # max(MIN, ceil(1.0 / 0.7))
        self.assertEqual([e for e in self.history() if e["event"] == "resized"][-1]["kind"], "idle")
        self.assertTrue(self.c.handle({"op": "status"})["leases"][0]["shrunk"])
        self.advance(128, usage={a["id"]: 2.0})  # busy at its new size: grows on demand
        self.assertEqual(self.lease(a)["cores"], 7)

    def test_without_scopes_a_range_is_granted_at_min_and_fixed(self):
        # Review 4, finding 5: it could never be shrunk, so it gets MIN only.
        a = self.ask(2, 8, resizable=False)
        self.assertEqual(self.lease(a)["cores"], 2)
        self.assertNotIn("elastic", self.lease(a))
        self.c.state["leases"].clear()
        e = self.ask(2, 12, project="e")
        self.later()
        f = self.ask(2, 12, project="f", resizable=False)
        # e gave only f's MIN, not f's fair share: f could never give it back.
        self.assertEqual((self.lease(f)["cores"], self.lease(e)["cores"]), (2, 6))

    def test_backfilled_range_shrinks_others_only_for_its_min(self):
        # Review 4, finding 6: behind a GPU-lock head, a backfilled 2:8
        # starts at MIN, so only 2 cores are taken from the elastic lease.
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.advance(64)
        self.c.handle({"op": "lock", "agent": "owner", "name": "gpu", "reason": "x"})
        self.ask(1, project="g", locks=["gpu"])
        b = self.ask(2, 8, project="b")
        self.assertEqual(self.state_of(b), "granted")
        self.assertEqual((self.lease(a)["cores"], self.lease(b)["cores"]), (9, 2))

    def test_current_client_without_repo_is_not_a_free_project_name(self):
        x = self.lease(self.ask(1, project="invented", repo=None))
        self.assertEqual(self.c._project(x), "default")
        old = self.lease(self.ask(1, project="named", repo=None, client=None))
        old.pop("client", None)
        self.assertEqual(self.c._project(old), "named")  # older clients keep their name

    def test_a_newer_resize_of_the_same_lease_is_not_lost(self):
        # Review 5, finding 2, with the real worker thread: while the first
        # shrink of A completes, a second waiting request asks A to shrink
        # again; that second resize must stay pending and run.
        self.c.resize_async = True
        first_called, release = threading.Event(), threading.Event()
        calls = []

        def resize(scope, cores):
            calls.append(cores)
            if len(calls) == 1:
                first_called.set()
                release.wait(10)
            return True

        self.c.resize = resize
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.advance(64)
        self.c.ensure_worker(start=True)
        b = self.ask(4, project="b")
        self.assertTrue(first_called.wait(10))
        c = self.ask(4, project="c")  # waits: A's first shrink is under way
        self.assertEqual(self.c._jobs.qsize(), 0)  # at most one job for A at a time
        release.set()
        for _ in range(500):
            if self.state_of(c) == "granted":
                break
            time.sleep(0.01)
        self.assertEqual((self.state_of(b), self.state_of(c)), ("granted", "granted"))
        self.assertEqual(calls, [8.0, 4.0])
        for _ in range(100):
            if not self.c._pending:
                break
            time.sleep(0.01)
        self.assertEqual((self.lease(a)["cores"], self.c._pending), (4, {}))

    def test_tolerance_only_with_elastic_leases(self):
        self.ask(4, project="t", exclusive_locks=["gpu"])
        self.assertEqual(self.c._eps(), 0.0)  # timing runs alone: decisions as on #11
        self.ask(2, 4, project="e")
        self.assertEqual(self.c._eps(), ac.CPU_EPS)

    def test_resize_worker_survives_failures_and_is_restarted(self):
        # Review 4, finding 3: an exception in a job (a full disk in _save, a
        # resize call that raises) must not end resizing for good.
        self.c.resize_async = True
        calls = []

        def resize(scope, cores):
            calls.append(cores)
            if len(calls) == 1:
                raise OSError("bus gone")
            return True

        self.c.resize = resize
        a = self.ask(2, 8, project="p")
        self.later()
        b = self.ask(4, project="q")
        self.assertEqual(self.c._jobs.qsize(), 1)
        save, failed = self.c._save, []

        def failing_save():
            if not failed:
                failed.append(1)
                raise OSError(28, "No space left on device")
            save()

        self.c._save = failing_save
        with patch("sys.stderr", io.StringIO()):
            self.assertFalse(self.c.ensure_worker())  # nothing to restart yet
            self.assertTrue(self.c.ensure_worker(start=True))
            for _ in range(200):
                if not self.c._jobs.qsize() and not self.c._pending:
                    break
                time.sleep(0.01)
            time.sleep(0.05)
            self.assertTrue(self.c._worker.is_alive())  # survived the failing call and save
            self.assertEqual(self.c._pending, {})
            self.assertEqual(self.lease(a)["resize_failures"], 1)  # counted, retried later
            self.now += ac.RESIZE_BACKOFF + 1
            with self.c.lock:
                self.c._admit()
            for _ in range(200):
                if self.state_of(b) == "granted":
                    break
                time.sleep(0.01)
            self.assertEqual((self.state_of(b), self.lease(a)["cores"]), ("granted", 4))
            dead = threading.Thread(target=lambda: None)
            dead.start()
            dead.join()
            self.c._worker = dead  # as if it had died
            self.c.tick()
            self.assertTrue(self.c._worker.is_alive())  # the tick's watchdog restarted it
    def test_two_projects_converge_to_half_each(self):
        a = self.ask(2, 8, project="p")
        self.later()
        b = self.ask(2, 8, project="q")
        self.assertEqual(self.sizes(a, b), (4, 4))  # its share at once
        self.advance(240)
        self.assertEqual(self.sizes(a, b), (4, 4))

    def test_third_project_gets_its_share_at_once(self):
        self.cores = 12.0
        a = self.ask(2, 12, project="p")
        self.later()
        b = self.ask(2, 12, project="q")
        self.advance(64)
        self.assertEqual(self.sizes(a, b), (6, 6))
        c = self.ask(2, 12, project="r")
        self.assertEqual(self.state_of(c), "granted")
        self.assertEqual(sorted(self.sizes(a, b, c)), [4, 4, 4])

    def test_idle_projects_share_is_used_and_handed_back(self):
        a = self.ask(2, 8, project="p")
        self.advance(64)
        self.assertEqual(self.lease(a)["cores"], 8)  # q idle: p may take everything
        b = self.ask(2, 8, project="q")
        self.assertEqual(self.sizes(a, b), (4, 4))
        self.c.handle({"op": "release", "id": b["id"]})
        self.advance(128)
        self.assertEqual(self.lease(a)["cores"], 7)  # q idle again: p grows back

    def test_unequal_weights(self):
        config = Path(self.tmp.name, "config.toml")
        config.write_text("[projects.q]\nweight = 3\n")
        self.c.config_file = config
        self.c.tick()
        self.assertEqual(self.c._weight("q"), 3)
        a = self.ask(1, 8, project="p")
        self.later()
        b = self.ask(1, 8, project="q")
        self.assertEqual(self.sizes(a, b), (2, 6))
        rows = {r["name"]: r for r in self.c.handle({"op": "status"})["projects"]}
        self.assertEqual((rows["p"]["share"], rows["q"]["share"]), (2, 6))

    def test_line_stays_first_come_first_served_across_projects(self):
        self.cores = 12.0
        holder = self.ask(6, project="h", expect=600)
        head = self.ask(8, project="big")
        later = [self.ask(2, project="small", expect=72000) for _ in range(3)]
        self.assertEqual([self.state_of(r) for r in later], ["granted", "granted", "waiting"])
        self.assertIn("leaves capacity", self.lease(later[1])["backfill_reason"])
        self.assertIn("would delay", self.reason(later[2]))
        self.c.handle({"op": "release", "id": holder["id"]})
        self.assertEqual(self.state_of(head), "granted")

    def test_nothing_passes_a_waiting_exclusive_after_its_window(self):
        self.ask(4, project="h")
        first = self.ask(6, project="p")
        whole = self.ask(1, project="p", exclusive=True)
        self.now += ac.EXCLUSIVE_PASS_WINDOW + 1
        self.busy = self.c.busy = 4.0  # the holder is busy
        late = self.ask(2, project="q", mem=8 * GIB)
        self.assertEqual(late["state"], "waiting")
        self.assertEqual([x["id"] for x in self.c.state["queue"]], [first["id"], whole["id"], late["id"]])

    def test_whole_machine_exclusive_keeps_its_rules(self):
        self.ask(1, project="q")
        whole = self.ask(2, project="p", exclusive=True)
        self.assertEqual(whole["state"], "waiting")
        self.assertIn("must end first", whole["reason"])
        self.assertEqual(self.ask(2, project="r")["state"], "granted")  # small, within the window
        self.now += ac.EXCLUSIVE_PASS_WINDOW + 1
        self.assertEqual(self.ask(1, project="s")["state"], "waiting")
        for x in list(self.c.state["leases"]):
            self.c.handle({"op": "release", "id": x["id"]})
        self.assertEqual(self.state_of(whole), "granted")

    # -- the reviews' scenarios, 12 cores

    def test_scenario_a_fixed_request_beside_a_full_elastic_lease(self):
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.advance(64)
        b = self.ask(4, project="b")
        self.assertEqual(self.state_of(b), "granted")
        self.assertEqual(self.lease(a)["cores"], 8)
        self.c.handle({"op": "release", "id": b["id"]})
        self.advance(128)
        self.assertEqual(self.lease(a)["cores"], 11)

    def test_scenario_b_many_elastic_leases_and_a_newcomer(self):
        self.cores = 12.0
        mine = [self.ask(2, 4, project="a") for _ in range(3)]
        self.assertEqual(self.sizes(*mine), (4, 4, 4))
        self.later()
        b = self.ask(2, 8, project="b")
        self.assertEqual(self.lease(b)["cores"], 6)
        self.assertEqual(sum(self.sizes(*mine)), 6)
        self.advance(120)
        self.assertEqual((sum(self.sizes(*mine)), self.lease(b)["cores"]), (6, 6))

    def test_scenario_d_three_busy_projects_then_one_leaves(self):
        self.cores = 12.0
        leases = []
        for p in "abc":
            leases.append(self.ask(2, 12, project=p))
            self.advance(8)
        self.advance(300)  # the invariant is checked after every tick
        self.assertEqual(sorted(self.sizes(*leases)), [4, 4, 4])
        self.c.handle({"op": "release", "id": leases[2]["id"]})
        self.advance(300)
        self.assertEqual(self.sizes(*leases[:2]), (6, 6))

    def test_even_out_never_grants_more_than_the_machine(self):
        # Review 2, finding 7: a 2:12 and a fixed 6 in one project, a 2:12 in another.
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.advance(8)
        self.ask(6, project="a")
        self.advance(8)
        b = self.ask(2, 12, project="b")
        self.advance(300)
        self.assertEqual(self.sizes(a, b), (2, 4))

    def test_scenario_f_memory_waits_never_preempt(self):
        a = self.ask(2, 4, project="a", mem=10 * GIB)
        self.later()
        b = self.ask(1, project="b", mem=60 * GIB)
        self.assertEqual(b["state"], "waiting")
        self.assertEqual(self.c._why(self.lease(b))[0], "mem")
        self.assertEqual(self.lease(a)["cores"], 4)
        self.assertEqual(self.resized, [])

    def test_a_ramping_lease_is_not_over_subtracted(self):
        # Review 2, finding 8: smoothing starts from 0, like the busy figure.
        self.cores = 12.0
        big = self.ask(8, project="big")
        self.advance(120)
        self.check_grants = False
        big2 = self.ask(4, project="big")
        small = self.ask(2, project="small")
        self.assertEqual(small["state"], "waiting")
        self.advance(2 * ac.TICK, usage={big2["id"]: 4.0})
        self.assertEqual(self.state_of(small), "waiting")
        self.assertLessEqual(self.c._own_cores[big2["id"]], 1.0)
        self.assertTrue(big)

    def test_nested_and_smoothed_lease_measurements(self):
        self.cores = 12.0
        outer = self.ask(4, project="t", exclusive_locks=["gpu"])
        inner = self.ask(2, project="t", exclusive_locks=["nas"])
        pid_outer, pid_inner = self.lease(outer)["pid"], self.lease(inner)["pid"]
        self.c.tree_pids = lambda pid: {pid_outer, pid_inner} if pid == pid_outer else {pid}
        self.now += ac.WARMUP + 1
        self.check_grants = False
        self.advance(200, usage={outer["id"]: 4.0, inner["id"]: 2.0})  # outer's 4 includes inner's 2
        self.outside = 3.0
        self.busy = self.c.busy = 7.0
        self.assertAlmostEqual(self.c.handle({"op": "status"})["free"]["cores"], 3.0, places=1)

    # -- review 3

    def test_fixed_only_project_alone_runs_as_on_main(self):
        # Review 3, finding 2: three fixed 4-core gates and a 2-core job, each
        # using less than it holds, 90 s apart: all four run, as on main.
        self.cores = 12.0
        self.outside = 0.5
        usage = {}
        jobs = []
        for size, use in ((4, 2.5), (4, 2.5), (4, 2.5), (2, 1.5)):
            jobs.append(self.ask(size, project="gates", expect=3600))
            usage[jobs[-1]["id"]] = use
            self.check_grants = False  # fixed sizes admitted on measured use, as on main
            self.advance(90, usage=usage)
        self.assertEqual([self.state_of(r) for r in jobs], ["granted"] * 4)

    def test_invented_project_names_share_their_repositorys_entitlement(self):
        # Review 3, finding 4: five --project names from one repository.
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.later()
        names = [self.ask(2, 12, project=f"B{i}", repo="b") for i in range(5)]
        self.advance(120)
        self.assertEqual(self.lease(a)["cores"], 6)  # repository b's share, not five shares
        self.assertEqual(sum(self.lease(r)["cores"] for r in names
                             if self.state_of(r) == "granted"), 6)
        self.assertEqual(self.c._project(self.lease(names[0])), "b")

    def test_configured_project_names_count_on_their_own(self):
        config = Path(self.tmp.name, "config.toml")
        config.write_text("[projects.sub]\nweight = 1\n")
        self.c.config_file = config
        self.c.tick()
        x = self.lease(self.ask(1, project="sub", repo="b"))
        self.assertEqual(self.c._project(x), "sub")

    def test_timing_run_takes_other_projects_elastic_cores_down_to_min(self):
        # Review 5, finding 1: whatever its fair share, a timing run at the
        # head shrinks other projects' elastic leases as far as it needs.
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.advance(64)
        self.assertEqual(self.lease(a)["cores"], 12)
        timed = self.ask(8, project="t", exclusive_locks=["gpu"], headroom=1)
        self.assertEqual(self.state_of(timed), "granted")  # within one resize
        self.assertEqual(self.lease(a)["cores"], 3)  # 12 - 8 - 1 headroom
        self.assertEqual(len(self.resized), 1)

    def test_timing_run_shrinks_a_small_elastic_lease_within_its_share(self):
        self.cores = 12.0
        b = self.ask(2, 4, project="b")
        self.later()
        self.assertEqual(self.lease(b)["cores"], 4)
        timed = self.ask(8, project="t", exclusive_locks=["gpu"], headroom=1)
        self.assertEqual((self.state_of(timed), self.lease(b)["cores"]), ("granted", 3))

    def test_waiting_timing_run_is_not_told_to_use_min_max(self):
        self.cores = 12.0
        self.ask(2, 12, project="a")
        self.later()
        self.ask(6, project="f")  # fixed: never shrunk
        timed = self.ask(8, project="t", exclusive_locks=["gpu"], headroom=1)
        self.assertEqual(self.state_of(timed), "waiting")  # 6 + MIN 2 + 8 + 1 > 12
        self.assertNotIn("MIN:MAX", self.reason(timed))

    def test_elastic_holder_beside_a_timing_run_gives_back_and_is_named(self):
        # Review 3, finding 5 (scenario g).
        self.cores = 12.0
        self.ask(8, project="t", exclusive_locks=["gpu"], headroom=1)
        b = self.ask(2, 4, project="b")
        self.assertEqual(self.lease(b)["cores"], 3)
        self.later()
        c = self.ask(1, 4, project="c")
        self.assertEqual(self.state_of(c), "granted")  # reclaimed from b, down to b's MIN
        self.assertEqual((self.lease(b)["cores"], self.lease(c)["cores"]), (2, 1))
        d = self.ask(2, 4, project="d")
        self.assertEqual(d["state"], "waiting")
        reason = self.reason(d)
        self.assertIn(f"{self.lease(b)['id']} (project b, elastic, at its MIN 2)", reason)
        self.assertIn("(project t, timing run 8, never shrunk)", reason)

    def test_fixed_lease_keeping_a_project_below_its_share_is_named(self):
        # (c2): a held (already running) fixed 11 cannot be shrunk.
        self.cores = 12.0
        hog = self.ask(11, project="a", agent="hog", running=True)
        self.busy = self.c.busy = 11.0
        b = self.ask(2, 4, project="b")
        self.assertEqual(b["state"], "waiting")
        self.assertIn(f"held by {hog['id']} (project a, fixed 11, never shrunk)", self.reason(b))
        status = self.c.handle({"op": "status"})
        lease, = [x for x in status["leases"] if x["id"] == hog["id"]]
        self.assertEqual(lease["holding_back"], ["b"])
        hint, = self.c.handle({"op": "inbox", "agent": "hog"})["messages"]
        self.assertIn("--cores MIN:MAX", hint["text"])

    def test_timing_runs_are_not_told_to_use_min_max(self):
        self.cores = 12.0
        self.ask(8, project="a", agent="timer", exclusive_locks=["gpu"], headroom=1)
        self.ask(2, project="a", agent="other")
        self.assertEqual(self.ask(4, 6, project="b")["state"], "waiting")
        self.assertTrue([e for e in self.history() if e["event"] == "unfair"])
        self.assertFalse(self.c.handle({"op": "inbox", "agent": "timer"})["messages"])
        self.assertIn("MIN:MAX", self.c.handle({"op": "inbox", "agent": "other"})["messages"][0]["text"])

    def test_resize_failures_are_retried_before_a_lease_is_fixed(self):
        # Review 3, finding 3.
        for recovers in (True, False):
            with self.subTest(recovers=recovers):
                self.c.state["leases"].clear()
                self.c.state["queue"].clear()
                self.busy = self.c.busy = 0.0
                a = self.ask(2, 8, project="p")
                self.later()
                scope = self.lease(a)["scope"]
                self.failing.add(scope)
                b = self.ask(2, project="q")
                self.assertEqual(b["state"], "waiting")
                self.assertTrue(self.lease(a)["elastic"])  # one failure: kept, retried later
                self.assertFalse(self.c._donor(self.lease(a)))
                if recovers:
                    self.failing.discard(scope)
                self.busy = self.c.busy = 8.0  # a is busy
                self.c._own_cores = {a["id"]: 8.0}
                self.c._size_avg = {a["id"]: 8.0}  # as measured while it ran at 8
                for attempt in range(1, ac.RESIZE_ATTEMPTS):
                    self.now += ac.RESIZE_BACKOFF * 2 ** (attempt - 1) + 1
                    self.c._admit()
                if recovers:
                    self.assertEqual((self.state_of(b), self.lease(a)["cores"]), ("granted", 6))
                    self.assertNotIn("resize_failures", self.lease(a))
                else:
                    self.assertNotIn("elastic", self.lease(a))  # fixed after the last attempt
                    self.assertEqual(self.lease(a)["resize_kind"], "fixed")
                    self.assertTrue([m for m in self.c.handle({"op": "inbox", "agent": "p"})["messages"]
                                     if "can no longer be resized" in m["text"]])
                    self.assertEqual(self.state_of(b), "waiting")
        with patch.object(ac.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 5)):
            self.assertFalse(ac.resize_scope("agent-coord-L1-1.scope", 2))
        with patch.object(ac.subprocess, "run", side_effect=OSError("no systemctl")):
            self.assertFalse(ac.resize_scope("agent-coord-L1-1.scope", 2))
    def test_never_resized_before_its_client_reports_the_scope(self):
        a = self.ask(2, 8, project="p")
        self.now += 30  # granted, but its client has not reported its scope
        self.assertEqual(self.ask(4, project="q")["state"], "waiting")
        self.assertEqual(self.resized, [])
        self.now += ac.SCOPE_READY_AFTER  # no report at all: tried after the window
        self.c._admit()
        self.assertEqual(self.lease(a)["cores"], 4)

    def test_resizes_run_outside_the_lock_and_count_when_done(self):
        # Review 3, finding 6, and the service's own order: a shrink counts
        # only after systemctl succeeded, and the call never holds the lock.
        self.c.resize_async = True
        seen = []

        def resize(scope, cores):
            seen.append(self.c.lock.locked())
            return True

        self.c.resize = resize
        a = self.ask(2, 8, project="p")
        self.later()
        b = self.ask(4, project="q")
        self.assertEqual(b["state"], "waiting")  # the shrink is under way
        self.assertEqual(self.lease(a)["cores"], 8)
        self.assertEqual(self.c._jobs.qsize(), 1)
        self.assertEqual(self.ask(1, project="r")["state"], "waiting")  # not planned twice
        self.assertEqual(self.c._jobs.qsize(), 1)
        self.c.resize_job(self.c._jobs.get())
        self.assertEqual(seen, [False])
        self.assertEqual((self.lease(a)["cores"], self.state_of(b)), (4, "granted"))

    def test_settles_over_900_ticks_with_resizable_leases(self):
        # Review 3's oscillation probe: A busy, B at 80%, C busy and idle in
        # turn every 5 minutes; sizes in force never exceed the machine and
        # leases are not resized back and forth.
        self.cores = 12.0
        a = self.ask(2, 12, project="a")
        self.advance(8)
        b = self.ask(2, 12, project="b")
        self.advance(8)
        c = self.ask(2, 8, project="c")
        changes = []
        for tick in range(900):
            busy_c = (tick * ac.TICK // 300) % 2 == 0
            usage = {a["id"]: self.lease(a)["cores"], b["id"]: 0.8 * self.lease(b)["cores"],
                     c["id"]: self.lease(c)["cores"] if busy_c else 0.2}
            before = len(self.history())
            self.advance(ac.TICK, usage=usage)
            changes.append(len([e for e in self.history()[before:] if e["event"] == "resized"]))
        windows = [sum(changes[i:i + 150]) for i in range(0, 900, 150)]
        self.assertLessEqual(max(windows), 8, windows)
        self.assertEqual(sum(self.sizes(a, b, c)) <= 12, True)

    # -- configuration

    def test_config_defaults_and_every_bad_value(self):
        self.assertEqual(ac.load_config(None)["resize_interval"], 60)
        cases = {
            "default_weight = 0\n": "default_weight", "idle_target_use = 0\n": "idle_target_use",
            "projects = 3\n": "projects must be a table", "idle_fraction = 2\n": "idle_fraction",
            "resize_interval = 1e9\n": "resize_interval", 'grow_demand = "x"\n': "grow_demand",
            "reserve_cores = 2\n": "unknown setting",
            "[projects.x]\nweight = 0\n": "projects.x", "[projects]\nx = 3\n": "projects.x",
            "nonsense = 1\n": "unknown setting", "this is not toml": "using defaults",
            "floor_cores = 2\n": "unknown setting",
        }
        path = Path(self.tmp.name, "bad.toml")
        for text, error in cases.items():
            with self.subTest(text=text), patch("sys.stderr", io.StringIO()):
                path.write_text(text)
                config = ac.load_config(path)
                self.assertTrue(any(error in e for e in config["errors"]), config["errors"])
                for key, default in ac.DEFAULT_CONFIG.items():
                    self.assertEqual(config[key], default)
                self.assertEqual(config["projects"], {})
        path.write_text("resize_interval = 30\n[projects.x]\nweight = 2\n")
        config = ac.load_config(path)
        self.assertEqual((config["resize_interval"], config["projects"]), (30, {"x": {"weight": 2}}))

    def test_bad_config_never_stops_admission_or_ticks(self):
        path = Path(self.tmp.name, "config.toml")
        self.c.config_file = path
        self.check_grants = False  # idle leases: MINs admitted on measured use
        a = self.ask(2, 8, project="p")
        for text in ("default_weight = 0\n", "idle_target_use = 0\n", "projects = 3\n", "\x00"):
            with self.subTest(text=text), patch("sys.stderr", io.StringIO()):
                path.write_text(text)
                self.now += 1
                os.utime(path, (self.now, self.now))
                self.advance(200, usage={a["id"]: 0.5})
                self.assertIn("state", self.ask(2, 4, project=f"q{len(text)}"))
                self.assertTrue(self.c.handle({"op": "status"})["config_errors"])
        out = io.StringIO()
        with patch.object(ac, "call", side_effect=lambda req, **_: self.c.handle(req)), \
                patch("sys.stdout", out):
            ac.cmd_status(ac.argparse.Namespace(json=False))
        self.assertIn("config: ", out.getvalue())

    # -- visibility

    def test_status_and_history_text(self):
        a = self.ask(2, 8, project="p")
        self.later()
        self.ask(4, project="q")
        out = io.StringIO()
        with patch.object(ac, "call", side_effect=lambda req, **_: self.c.handle(req)), \
                patch("sys.stdout", out):
            ac.cmd_status(ac.argparse.Namespace(json=False))
        text = out.getvalue()
        self.assertIn("4 of 2:8 cores", text)
        self.assertIn("SHRUNK (preempted by", text)
        self.assertIn("[project p]", text)
        self.assertRegex(text, r"q +weight 1 +share 4.0 +holds 4 +waiting 0")
        out = io.StringIO()
        with patch.object(ac, "HISTORY_FILE", Path(self.tmp.name, "history.jsonl")), \
                patch("sys.stdout", out):
            ac.cmd_history(ac.argparse.Namespace(since=3600.0, json=False))
        self.assertRegex(out.getvalue(), rf"resized +{a['id']} .*2:8 \[project p\] \[8->4 cores\]")


class LeaseFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "leases", "L1-1")

    def test_content_and_atomic_replacement(self):
        info = {"lease": "L1", "min": 2, "max": 8, "granted": 6.0, "reason": "granted", "time": 1}
        ac.write_lease_file(self.path, info)
        first = self.path.stat().st_ino
        line, data = self.path.read_text().splitlines()
        self.assertEqual(line, "6")
        self.assertEqual(json.loads(data), info)
        with patch.object(Path, "write_text", autospec=True,
                          side_effect=Path.write_text) as write:
            ac.write_lease_file(self.path, {**info, "granted": 2.5})
        self.assertNotEqual(write.call_args.args[0], self.path)  # written aside, then renamed
        self.assertNotEqual(self.path.stat().st_ino, first)
        self.assertEqual(self.path.read_text().splitlines()[0], "2")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["L1-1"])

    def follow(self, notify, grants, handler=True, stderr=None):
        """Run follow_grant against a real child that records SIGUSR1.
        grants: poll replies in turn (an exception instance is raised)."""
        out = Path(self.tmp.name, "signals")
        code = ("import signal, sys, time\n"
                "def got(*_):\n"
                "    open(sys.argv[1], 'a').write('usr1\\n')\n"
                + ("signal.signal(signal.SIGUSR1, got)\n" if handler else "")
                + "print('ready', flush=True)\n"
                "sys.stdin.readline()\n")
        child = subprocess.Popen([sys.executable, "-c", code, str(out)], text=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        polls = []

        def poll(request):
            polls.append(request)
            if len(polls) > len(grants):
                stop.set()
                return {"state": "granted", "cores": None}
            reply = grants[len(polls) - 1]
            if isinstance(reply, BaseException):
                raise reply
            return reply

        info = {"lease": "L1", "min": 2, "max": 8, "granted": 8.0, "reason": "granted", "time": 0}
        stop = threading.Event()
        try:
            with patch("sys.stderr", stderr or io.StringIO()):
                ac.follow_grant("L1", child, self.path, info, notify, stop, poll=poll, interval=0.01)
            time.sleep(0.2)
            alive = child.poll() is None
            child.communicate("\n", timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        signals = out.read_text().split() if out.exists() else []
        return signals, polls, info, alive

    def test_grant_change_rewrites_and_signals_only_the_child(self):
        signals, polls, info, _ = self.follow(True, [
            {"state": "granted", "cores": 8.0}, {"state": "granted", "cores": 4.0, "reason": "preempted"},
            {"state": "granted", "cores": 4.0}])
        self.assertEqual(signals, ["usr1"])
        self.assertEqual(self.path.read_text().splitlines()[0], "4")
        self.assertEqual(info["reason"], "preempted")
        self.assertTrue(all(set(p) == {"op", "id"} for p in polls))  # it only reads

    def test_signal_is_opt_in(self):
        # No handler: SIGUSR1 would kill this child, so it surviving proves none was sent.
        signals, _, _, alive = self.follow(False, [{"state": "granted", "cores": 4.0}], handler=False)
        self.assertTrue(alive)
        self.assertEqual(signals, [])
        self.assertEqual(self.path.read_text().splitlines()[0], "4")

    def test_follower_survives_errors(self):
        class Closed(io.StringIO):
            def write(self, _text):
                raise BrokenPipeError("stderr closed")

        signals, polls, _, _ = self.follow(True, [
            OSError("service restarting"), SystemExit("unreachable"),
            {"state": "granted", "cores": 4.0}, {"state": "granted", "cores": 3.0}],
            stderr=Closed())
        self.assertEqual(len(polls), 5)  # kept polling through every error
        self.assertEqual(signals, ["usr1", "usr1"])
        self.assertEqual(self.path.read_text().splitlines()[0], "3")

    def test_stale_lease_files_are_swept(self):
        folder = Path(self.tmp.name, "leases")
        folder.mkdir()
        dead, live = dead_pid(), os.getpid()
        for name in (f"L1-{dead}", f".L2-{dead}.tmp", f"L3-{live}", "unrelated"):
            Path(folder, name).write_text("1\n")
        self.assertEqual(ac.sweep_lease_files(folder), 2)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), [f"L3-{live}", "unrelated"])
        self.assertEqual(ac.sweep_lease_files(Path(self.tmp.name, "missing")), 0)

    def test_resize_and_scope_commands(self):
        self.assertEqual(ac.resize_command("agent-coord-L3-9.scope", 2.5),
                         ["systemctl", "--user", "set-property", "--runtime",
                          "agent-coord-L3-9.scope", "CPUQuota=250%"])
        with patch.dict(os.environ, {"AGENT_COORD_SCOPE_PREFIX": "agent-coord-test"}):
            self.assertEqual(ac.scope_name("L12"), f"agent-coord-test-L12-{os.getpid()}.scope")
        with patch.dict(os.environ, {"AGENT_COORD_SCOPE_PREFIX": "../x"}), \
                self.assertRaises(SystemExit):
            ac.scope_name("L1")
        with patch.object(ac, "has_scopes", return_value=True):
            cmd = ac.limited(["true"], 3, GIB, "agent-coord-test-L1-1.scope")
        self.assertEqual(cmd[cmd.index("--unit") + 1], "agent-coord-test-L1-1.scope")

    def test_default_project_is_the_main_checkout(self):
        repo = Path(self.tmp.name, "repo")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
        subprocess.run([*git, "init", "-q", str(repo)], check=True)
        subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        worktree = Path(self.tmp.name, "wt-feature")
        subprocess.run([*git, "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True)
        here = os.getcwd()
        try:
            for where in (repo, worktree):
                os.chdir(where)
                self.assertEqual(ac.default_project("agent"), "repo")
            os.chdir(self.tmp.name)
            self.assertEqual(ac.default_project("agent"), "agent")
        finally:
            os.chdir(here)


@unittest.skipUnless(REAL_SCOPES,
                     "real systemd scope test; set AGENT_COORD_SYSTEMD_TESTS=1")
class RealScopeTest(unittest.TestCase):
    """Resize a real transient scope of our own (unique test name), then stop it."""

    def test_set_property_resizes_a_running_scope(self):
        scope = f"agent-coord-test-{os.getpid()}-{int(time.time())}.scope"
        child = subprocess.Popen(ac.limited(["sleep", "60"], 1, 64 * 1024**2, scope))
        try:
            def quota():
                return subprocess.run(["systemctl", "--user", "show", "-p", "CPUQuotaPerSecUSec",
                                       "--value", scope], capture_output=True, text=True).stdout.strip()
            for _ in range(100):
                if quota() == "1s":
                    break
                time.sleep(0.05)
            self.assertEqual(quota(), "1s")
            self.assertTrue(ac.resize_scope(scope, 2.5))
            self.assertEqual(quota(), "2.500000s")
        finally:
            child.kill()
            child.wait()
            subprocess.run(["systemctl", "--user", "stop", scope], capture_output=True)


class OrdinaryWorkloadInvariantTest(unittest.TestCase):
    """Workloads without MIN:MAX (fixed, IO, held and timing leases, several
    projects, current and old clients) are admitted as
    before this change: on random sequences, every term it adds is zero at
    every step. Replaces comparing with main's code through git, which
    stops meaning anything once merged."""

    def test_new_terms_are_zero_for_fixed_only_workloads(self):
        import random
        pids = [os.getpid(), os.getppid()]
        for seed in range(60):
            rng = random.Random(seed)
            env = {"busy": 0.0, "now": 100000.0, "mono": 1000.0, "cpu": {pid: 0.0 for pid in pids}}
            resized = []
            with tempfile.TemporaryDirectory() as tmp, \
                    patch("time.time", side_effect=lambda: env["now"]), \
                    patch("time.monotonic", side_effect=lambda: env["mono"]):
                c = ac.Coordinator(
                    Path(tmp, "state.json"),
                    measure=lambda: {"vms": [], "busy": env["busy"], "available": 64 * GIB},
                    cap=lambda: (12.0, 58 * GIB), footprint=lambda pid: 0,
                    cpu_seconds=lambda pid: env["cpu"].get(pid),
                    resize=lambda scope, cores: resized.append(scope) or True)
                for step in range(40):
                    op = rng.random()
                    if op < 0.45:
                        project = rng.choice(["p", "q", "r"])
                        req = {"op": "acquire", "agent": f"a{rng.randrange(3)}",
                               "pid": rng.choice(pids), "cores": rng.choice([1, 2, 3, 4, 6, 11, 12]),
                               "mem": rng.choice([1, 2, 4, 8]) * GIB, "label": "job",
                               "expect": rng.choice([60, 600, 3600]),
                               "headroom": rng.choice([0, 0, 0, 1, 2]),
                               "locks": rng.choice([[], [], ["nas"]]),
                               "exclusive_locks": rng.choice([[], [], [], ["gpu"]]),
                               "io": rng.random() < 0.1, "running": rng.random() < 0.1}
                        if rng.random() < 0.7:  # a current client, else an old one
                            req.update(project=project, repo=project, client=ac.CLIENT_VERSION)
                        reply = c.handle(dict(req))
                        # The reply main gives: refused only when bigger than
                        # the machine, else granted at its size or waiting with
                        # main's reasons.
                        claim = 1 if req["io"] else req["cores"]
                        too_big = max(req["cores"], claim + req["headroom"]) > 12
                        self.assertEqual("error" in reply, too_big and not req["running"],
                                         (seed, step, reply))
                        if "error" not in reply:
                            self.assertIn(reply["state"], ("granted", "waiting"))
                            if reply["state"] == "granted":
                                self.assertEqual(reply["cores"], req["cores"])
                            else:
                                for new in ("fair share", "never shrunk", "reserve", "elastic"):
                                    self.assertNotIn(new, reply["reason"], (seed, step))
                    elif op < 0.65:
                        ids = [x["id"] for x in c.state["leases"] + c.state["queue"]]
                        if ids:
                            c.handle({"op": "release", "id": rng.choice(ids)})
                    else:
                        dt = rng.choice([2, 4, 15, 40])
                        env["now"] += dt
                        env["mono"] += dt
                        for pid in pids:
                            env["cpu"][pid] += rng.uniform(0, 8) * dt
                        env["busy"] = rng.uniform(0, 12)
                        c.tick()
                    where = f"seed {seed} step {step}"
                    once = c._counted_once()
                    self.assertTrue(all(x.get("exclusive_locks") for x in c.state["leases"]
                                        if x["id"] in once), where)
                    if not once:
                        self.assertEqual(c._net_busy(), max(0.0, c.busy), where)
                    claims = sum(c._cpu_claim(x) for x in c.state["leases"])
                    self.assertEqual(c._unleased(), max(0.0, c.busy - claims), where)  # PR 11's
                    self.assertEqual(c._eps(), 0.0, where)  # no elastic lease: as on #11
                    self.assertEqual(resized, [], where)
                    self.assertFalse([e for e in ac.read_history(0, Path(tmp, "history.jsonl"))
                                      if e["event"] in ("resized", "unfair")], where)


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

    def test_tree_cpu_counts_child_cpu_even_after_reaping(self):
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
        self.assertGreaterEqual(reaped, active)


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


class ServiceTest(unittest.TestCase):
    """The CLI against a real service on a temporary socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = Path(self.tmp.name, "sock")
        # A private service: its own socket, state and scope names. Scope
        # resizes are logged, not sent to systemd, unless real scopes are
        # opted in.
        self.server_log = Path(self.tmp.name, "server.log")
        resize = "" if REAL_SCOPES else (
            ", resize=lambda scope, cores: print('resize', scope, cores, flush=True) or True")
        with self.server_log.open("w") as log:
            self.server = subprocess.Popen(
                [sys.executable, "-c",
                 ("import agent_coord as ac, pathlib, sys; "
                 "ac.serve(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), "
                 "measure=lambda: {'vms': [], 'busy': 0.0, 'available': 64 * 1024**3}, "
                 f"cap=lambda: (8.0, 16 * 1024**3), config_file=None{resize})"),
                 str(self.sock), str(Path(self.tmp.name, "state.json"))],
                cwd=Path(__file__).parent, stdout=log,
                env=dict(os.environ, AGENT_COORD_SCOPE_PREFIX="agent-coord-test"),
            )  # fmt: skip
        for _ in range(100):
            if self.sock.exists():
                break
            time.sleep(0.05)

    def tearDown(self):
        self.server.terminate()
        self.server.wait(10)
        self.tmp.cleanup()

    def cli_args(self, *args):
        # A private instance only: its own socket, state, lease files and
        # scope names (the private service records resizes; see setUp).
        code = (f"import agent_coord as ac, pathlib, sys; "
                f"ac.SOCKET = pathlib.Path({str(self.sock)!r}); "
                f"ac.HISTORY_FILE = pathlib.Path({str(Path(self.tmp.name, 'history.jsonl'))!r}); "
                + ("" if REAL_SCOPES else
                   # Seam: no real systemd scope unless opted in; the client
                   # behaves as if it had one.
                   "ac.has_scopes = lambda: True; ac.scope_exists = lambda pid, scope: True; "
                   "ac.limited = lambda cmd, cores, mem, scope=None: ['nice', '-n', '19', *cmd]; ")
                + f"sys.exit(ac.main(sys.argv[1:]))")  # fmt: skip
        env = dict(os.environ, AGENT_COORD_RUNTIME_DIR=str(Path(self.tmp.name, "run")),
                   AGENT_COORD_STATE_DIR=self.tmp.name, AGENT_COORD_SCOPE_PREFIX="agent-coord-test",
                   AGENT_COORD_CONFIG=str(Path(self.tmp.name, "none.toml")))  # fmt: skip
        return [sys.executable, "-c", code, *args], env

    def cli(self, *args):
        argv, env = self.cli_args(*args)
        return subprocess.run(argv, cwd=Path(__file__).parent, env=env,
                              capture_output=True, text=True, timeout=60, check=False)  # fmt: skip

    def test_run_executes_capped_and_releases(self):
        result = self.cli("--as", "t", "run", "--cores", "1", "--mem", "256M",
                          "--label", "echo", "--", sys.executable, "-c",
                          "import os; print(os.environ['COMPUTE_LEASE_CORES'])")  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        status = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(status["leases"], [])

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
                          "--", "sh", "-c", "exit 3")  # fmt: skip
        self.assertEqual(result.returncode, 3)
        history = self.cli("history", "--json")
        events = [json.loads(line) for line in history.stdout.splitlines()]
        self.assertEqual([e["event"] for e in events], ["requested", "started", "ended"])
        self.assertEqual(events[-1]["code"], 3)
        self.assertIn("ended", self.cli("history", "--since", "1h").stdout)

    def test_expect_cli_round_trip(self):
        result = self.cli("run", "--cores", "1", "--mem", "64M", "--label", "short",
                          "--expect", "10m", "--", "true")
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
                          "--", "true")  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli("check", "--cores", "1", "--mem", "64M",
                                  "--exclusive-lock", "gpu").returncode, 0)  # fmt: skip
        both = self.cli("run", "--cores", "1", "--mem", "64M", "--label", "x",
                        "--exclusive", "--exclusive-lock", "gpu", "--", "true")  # fmt: skip
        self.assertNotEqual(both.returncode, 0)

    def test_elastic_run_is_shrunk_for_a_waiting_request_and_told(self):
        """A MIN:MAX run starts at MAX on an empty machine, is shrunk when a
        fixed request needs the cores, sees the new count in its lease file,
        and (with --notify-signal) gets SIGUSR1: the command itself, recorded
        by a real child process."""
        out = Path(self.tmp.name, "child.out")
        stop = Path(self.tmp.name, "stop")
        child = (
            "import os, signal, sys, time, pathlib\n"
            "out = pathlib.Path(sys.argv[1]); stop = pathlib.Path(sys.argv[2])\n"
            "def note(text):\n"
            "    with out.open('a') as f: f.write(text + '\\n')\n"
            "lease = pathlib.Path(os.environ['COMPUTE_LEASE_FILE'])\n"
            "signal.signal(signal.SIGUSR1, lambda *_: note('usr1 ' + lease.read_text().split()[0]))\n"
            "note('start %s %s %s %s' % (os.getpid(), os.environ['COMPUTE_LEASE_CORES'],\n"
            "     os.environ['COMPUTE_LEASE_MIN'], os.environ['COMPUTE_LEASE_MAX']))\n"
            "deadline = time.time() + 40\n"
            "while not stop.exists() and time.time() < deadline: time.sleep(0.1)\n"
            "note('end ' + lease.read_text().replace('\\n', ' '))\n")
        argv, env = self.cli_args("--as", "big", "run", "--cores", "2:8", "--mem", "64M",
                                  "--notify-signal", "--label", "elastic", "--",
                                  sys.executable, "-c", child, str(out), str(stop))  # fmt: skip
        with subprocess.Popen(argv, cwd=Path(__file__).parent, env=env, text=True,
                              stderr=subprocess.PIPE) as elastic:
            try:
                for _ in range(200):
                    if out.exists():
                        break
                    time.sleep(0.05)
                start = out.read_text().split()
                self.assertEqual(start[0], "start")
                self.assertEqual(start[2:], ["8", "2", "8"])
                small = self.cli("--as", "small", "run", "--cores", "4", "--mem", "64M",
                                 "--label", "fixed", "--", "true")
                self.assertEqual(small.returncode, 0, small.stderr)
                for _ in range(300):
                    if "usr1" in out.read_text():
                        break
                    time.sleep(0.05)
                stop.touch()
                _, err = elastic.communicate(timeout=30)
            finally:
                if elastic.poll() is None:
                    elastic.kill()
        self.assertEqual(elastic.returncode, 0, err)
        lines = out.read_text().splitlines()
        self.assertIn("usr1 4", lines)
        end = lines[-1].split(" ", 2)
        self.assertEqual(end[:2], ["end", "4"])
        info = json.loads(end[2])
        self.assertEqual((info["min"], info["max"], info["granted"]), (2, 8, 4))
        self.assertIn("preempted by", info["reason"])
        if not REAL_SCOPES:  # the seam's record; with real scopes, the event below says it worked
            self.assertRegex(self.server_log.read_text(), r"resize agent-coord-test-L1-\d+\.scope 4")
        self.assertFalse(list(Path(self.tmp.name, "run", "leases").iterdir()))
        events = [json.loads(line) for line in self.cli("history", "--json").stdout.splitlines()]
        resized, = [e for e in events if e["event"] == "resized"]
        self.assertEqual((resized["from"], resized["cores"], resized["kind"]), (8, 4, "preempted"))
        self.assertIn("2:8", self.cli("history").stdout)
        inbox = self.cli("--as", "big", "inbox").stdout
        self.assertIn("was shrunk to 4 cores", inbox)

    def test_messages_round_trip(self):
        self.assertEqual(self.cli("--as", "a", "send", "--to", "b", "hello").returncode, 0)
        self.assertIn("a -> b: hello", self.cli("--as", "b", "inbox").stdout)
        self.assertIn("no new messages", self.cli("--as", "b", "inbox").stdout)

    def test_unreachable_service_is_explained(self):
        self.server.terminate()
        self.server.wait(10)
        result = self.cli("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemctl --user start agent-coord", result.stderr)


if __name__ == "__main__":
    unittest.main()
