"""Tests for agent-coord: python3 -m unittest -v test_agent_coord"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

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
        big = self.ask(4, agent="b")
        medium = self.ask(2, agent="c")  # fits now, but must not overtake b
        self.assertEqual(medium["state"], "waiting")
        self.assertIn("1 request(s) ahead", medium["reason"])
        self.assertEqual(big["state"], "waiting")

    def test_small_requests_backfill_past_a_blocked_head(self):
        self.ask(6)
        self.assertEqual(self.ask(4, agent="b")["state"], "waiting")
        self.assertEqual(self.ask(1, mem=GIB // 2, agent="c")["state"], "granted")

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


class ArchiveTest(unittest.TestCase):
    M = ["/mnt/nas"]

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
        self.server = subprocess.Popen(
            [sys.executable, "-c",
             "import agent_coord as ac, pathlib, sys; "
             "ac.serve(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), "
             "measure=lambda: {'vms': [], 'busy': 0.0, 'available': 64 * 1024**3}, "
             "cap=lambda: (8.0, 16 * 1024**3))",
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
                              capture_output=True, text=True, timeout=60)  # fmt: skip

    def test_run_executes_capped_and_releases(self):
        result = self.cli("--as", "t", "run", "--cores", "1", "--mem", "256M",
                          "--label", "echo", "--", sys.executable, "-c",
                          "import os; print(os.environ['COMPUTE_LEASE_CORES'])")  # fmt: skip
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        status = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(status["leases"], [])

    def test_run_returns_the_commands_exit_code(self):
        result = self.cli("run", "--cores", "1", "--mem", "64M", "--label", "x",
                          "--", "sh", "-c", "exit 3")  # fmt: skip
        self.assertEqual(result.returncode, 3)
        history = self.cli("history", "--json")
        events = [json.loads(line) for line in history.stdout.splitlines()]
        self.assertEqual([e["event"] for e in events], ["requested", "started", "ended"])
        self.assertEqual(events[-1]["code"], 3)
        self.assertIn("ended", self.cli("history", "--since", "1h").stdout)

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
