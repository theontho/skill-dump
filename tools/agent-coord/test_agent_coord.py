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

    def ask(self, cores, mem=GIB, agent="a", pid=None, exclusive=False):
        return self.c.handle({
            "op": "acquire", "agent": agent, "pid": pid or os.getpid(),
            "cores": cores, "mem": mem, "label": f"{agent} job",
            "exclusive": exclusive,
        })  # fmt: skip

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

    def test_nothing_passes_an_exclusive_head(self):
        self.ask(6)
        self.ask(2, agent="perf", exclusive=True)
        self.assertEqual(self.ask(1, mem=GIB // 2, agent="c")["state"], "waiting")

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
