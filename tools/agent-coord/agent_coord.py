#!/usr/bin/env python3
"""agent-coord: a local coordinator for the agents (and people) on this computer.

A user service (agent-coord.service) keeps the state; agents talk to it with
this same file as a CLI. It hands out CPU/RAM leases first come, first served
(running QEMU guests count at their measured use), so heavy jobs queue instead
of piling up, and it carries messages between agents.
"""

import argparse
import json
import math
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agent-coord"
SOCKET = RUNTIME / "sock"
STATE_FILE = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    / "agent-coord"
    / "state.json"
)
# One JSON line per lease event, next to the state; rotated by size.
HISTORY_FILE = STATE_FILE.with_name("history.jsonl")
HISTORY_MAX_BYTES = 8 * 1024**2
HISTORY_KEEP = 4  # rotated files kept: history.jsonl.1 .. .4
# Left for the desktop, the agents themselves and unleased short commands.
RESERVE_MEM = 4 * 1024**3
TICK = 2.0
POLL = 3.0
# Holders hear about a request that has waited this long (once per request).
NUDGE_AFTER = 60
# A new lease counts at its full size this long, until its use is measured.
WARMUP = 60
# Estimate until this agent/label has completed a run.
DEFAULT_EXPECT = 30 * 60
PROJECTION_GRACE = 60
BACKFILL_PASS_WINDOW = 10 * 60
# Preserve the original ordering behind a skipped stuck request.
BACKFILL_CORES, BACKFILL_MEM = 1.0, 1024**3
# Requests this small may start while an --exclusive request waits ahead of
# them (it waits for every lease to end anyway; a small one only adds its own
# run time), but only until that request has waited EXCLUSIVE_PASS_WINDOW:
# after that nothing new starts past it, so it waits at most the window plus
# the longest lease already running.
SMALL_CORES, SMALL_MEM = 2.0, 6 * 1024**3
EXCLUSIVE_PASS_WINDOW = 30 * 60
# A request short of memory is stuck once it has waited this long and would not
# fit even if every lease ended: what it needs is held outside the line, so
# holding others back gains it nothing. The wait rides out brief dips.
STUCK_AFTER = 120
# Kept free on every disk beyond all reservations.
DISK_MARGIN = 20 * 1024**3
MAX_MESSAGES = 1000

HELP = """\
agent-coord - share this computer's CPU and RAM between agents, and message them

QUOTA
  agent-coord check --cores N --mem SIZE [--exclusive | --exclusive-lock NAME]
      Exit 0 if a lease that size would start now, else 1 with the reason.
  agent-coord run --cores N --mem SIZE --label TEXT [--exclusive | --exclusive-lock NAME]
                  [--headroom N] [--expect D] [--max-wait D] -- CMD ...
      Wait in line until N cores and SIZE of RAM are free, then run CMD capped
      at them (systemd scope: CPUQuota, MemoryMax; exceeding MemoryMax kills
      CMD) at nice 19 and idle IO priority. CMD sees COMPUTE_LEASE_CORES=N:
      give its tools that many threads. The lease ends when CMD exits.
  agent-coord hold --cores N --mem SIZE --label TEXT --pid PID [--exclusive]
      Register a job that is already running (no waiting: it is running),
      until PID exits. Prints the lease ID. Holding a VM's qemu PID replaces
      its measured use with the lease.
  agent-coord release ID
  --exclusive   Whole-machine exclusive: wait until no other lease is held,
                then keep new ones waiting until this one ends. Small requests
                (<= 2 cores, 6G, no disk) may still start while it waits, for
                its first 30 min of waiting only.
  --exclusive-lock NAME
                Exclusive on resource NAME only (e.g. gpu, for a GPU timing
                run): holds NAME like --lock, waits until nobody else holds it,
                and its cores count in full (not measured use) so they stay
                guaranteed. CPU-only leases still run beside it in the rest of
                the machine. Repeatable. Prefer it to --exclusive.
  --headroom N  Start only when N cores beyond this lease are unclaimed
                (leases counted at their full size), and keep them unclaimed
                while it runs: bounds CPU contention for timing runs.
  --expect D    Expected run time (e.g. 10m), not a time limit. Defaults to the
                90th percentile of the last 20 successful runs for this agent/label,
                or 30m without history. Safe backfill may pass a blocked head.
  --max-wait D  Give up (exit 75) if not started within D (30m, 2h).
  --disk PATH:SIZE  Reserve SIZE of free space on PATH's disk for the job's
                peak (temp + outputs); repeatable. Waits until it's free
                beyond other reservations and a 20G margin.
  --lock NAME   Hold named resource NAME (windows-vm, gpu, nas-io, ...) for
                the lease; waits while anyone else holds it. Repeatable.

RESOURCES
  agent-coord lock NAME --reason TEXT [--pid PID] [--ttl D]
      Own NAME (a VM, the GPU, the NAS link ...) until unlock, PID exits or
      D passes. Fails if someone else holds it.
  agent-coord unlock NAME [--force]
  run refuses to work or write on the HDD archive pool (ZFS, /mnt/nas): its
  working directory, or -o/--outdir/-T/--tmp-dir/... values there. Reading
  references from it is fine. --allow-archive overrides (archiving a result).

MESSAGES
  agent-coord send --to AGENT|all TEXT
  agent-coord inbox [--wait D] [--all]     Unread messages (then marked read).
  agent-coord agents                       Agents seen, newest first.

  agent-coord status [--json]              Leases, line, VMs, free capacity.
  agent-coord history [--since D] [--json] Lease events (requested, waiting,
                                           started, ended, cancelled) with
                                           wait/run times; default last 24h.

Lease anything using more than 2 cores or 2 GB for more than a few minutes.
Running QEMU guests and other unleased load are measured and counted. --as NAME sets who you are
(default: your git worktree's folder name). Sizes: 512M, 8G.
"""


# ---------------------------------------------------------------- helpers


def parse_size(text: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)i?B?", text.strip(), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"not a size: {text} (e.g. 512M, 8G)")
    scale = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(float(match[1]) * scale[match[2].upper()])


def parse_duration(text: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration: {text} (e.g. 30m, 2h)")
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def parse_expect(text: str) -> float:
    value = parse_duration(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("expect must be a positive finite duration")
    return value


def human(size: float) -> str:
    return f"{size / 1024**3:.1f}G"


def exclusive_mode(x: dict) -> str | None:
    """'machine' for --exclusive, 'lock' for --exclusive-lock, else None."""
    if x.get("exclusive"):
        return "machine"
    return "lock" if x.get("exclusive_locks") else None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def mem_total() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("no MemTotal in /proc/meminfo")


def capacity() -> tuple[float, int]:
    return float(os.cpu_count() or 1), mem_total() - RESERVE_MEM


def _cpu_seconds(pid: int) -> float | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return None
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _footprint(pid: int) -> int:
    """Resident plus swapped-out memory: what the process needs back in RAM."""
    total = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmSwap:")):
                total += int(line.split()[1]) * 1024
    except OSError:
        pass
    return total


def tree_footprint(pid: int) -> int:
    """Resident plus swapped-out memory of pid and its descendants."""
    total, seen, stack = 0, set(), [pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        total += _footprint(current)
        try:
            for task in Path(f"/proc/{current}/task").iterdir():
                stack.extend(int(x) for x in (task / "children").read_text().split())
        except OSError:
            pass  # exited, or no children file: counts as what was read so far
    return total


def _host_busy() -> tuple[float, float]:
    """(busy, total) CPU seconds since boot, summed over all cores."""
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    tick = os.sysconf("SC_CLK_TCK")
    return (sum(fields[:8]) - idle) / tick, sum(fields[:8]) / tick


def mem_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def measure(sample: float = 1.0) -> dict:
    """VMs (measure_vms), cores busy host-wide over sample seconds, and
    memory available now."""
    busy_before, _ = _host_busy()
    vms = measure_vms(sample)
    busy_after, _ = _host_busy()
    elapsed = sample if vms else 0
    if not elapsed:
        time.sleep(sample)
        busy_after, _ = _host_busy()
    return {"vms": vms, "busy": round((busy_after - busy_before) / sample, 1),
            "available": mem_available()}  # fmt: skip


def measure_vms(sample: float = 1.0) -> list[dict]:
    """Running QEMU guests: pid, name, cores in use over sample seconds, and
    memory (resident plus swapped out)."""
    guests: dict[int, dict] = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or not Path(argv[0].decode(errors="replace")).name.startswith(
            "qemu-system"
        ):
            continue
        args = [a.decode(errors="replace") for a in argv]
        name = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "-name"), "")
        guests[int(proc.name)] = {"pid": int(proc.name), "name": name.split(",")[0]}
    before = {pid: _cpu_seconds(pid) for pid in guests}
    if guests:
        time.sleep(sample)
    for pid, guest in guests.items():
        after, start = _cpu_seconds(pid), before[pid]
        guest["cores"] = (
            round((after - start) / sample, 1)
            if after is not None and start is not None
            else 0.0
        )
        guest["mem"] = _footprint(pid)
    return list(guests.values())


def default_agent() -> str:
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()  # fmt: skip
        if top:
            return Path(top).name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd().name


# ---------------------------------------------------------------- service


class Coordinator:
    """All state, guarded by one lock; saved to STATE_FILE on every change."""

    def __init__(self, state_file: Path, measure=measure, cap=capacity,
                 history_file: Path | None = None, footprint=tree_footprint):
        self._saved = None
        self._samples = None
        self._footprints = None
        self.disk_free = disk_free
        self.footprint = footprint
        self.state_file = state_file
        self.history_file = history_file or state_file.with_name(HISTORY_FILE.name)
        self.measure = measure
        self.capacity = cap
        self.lock = threading.Lock()
        self.vms: list[dict] = []
        # Cores busy host-wide (smoothed) and memory available, as last measured.
        self.busy = 0.0
        self.available: int | None = None
        self.state = {"leases": [], "queue": [], "messages": [], "read": {},
                      "agents": {}, "locks": {}, "next": 1}  # fmt: skip
        if state_file.exists():
            try:
                self.state.update(json.loads(state_file.read_text()))
                self.state.setdefault("locks", {})
            except ValueError:
                pass
        self._prune()

    # -- persistence and housekeeping

    def _save(self) -> None:
        serialized = json.dumps(self.state)
        if serialized == self._saved:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(serialized)
        tmp.replace(self.state_file)
        self._saved = serialized

    def _log(self, event: str, x: dict, **extra) -> None:
        """Append one lease event to the history (labels only, no commands or
        environment), rotating it by size. Never fails a request."""
        entry = {"time": round(time.time(), 1), "event": event, "id": x["id"],
                 "agent": x["agent"], "label": x["label"], "cores": x["cores"],
                 "mem": x["mem"], "locks": x.get("locks", []),
                 "exclusive": exclusive_mode(x)}  # fmt: skip
        if x.get("exclusive_locks"):
            entry["exclusive_locks"] = x["exclusive_locks"]
        for key in ("headroom", "disk", "expect", "expect_source", "backfill_reason", "backfill_head"):
            if x.get(key):
                entry[key] = x[key]
        entry.update(extra)
        if self._samples is not None:
            self._remember(entry)
        path = self.history_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= HISTORY_MAX_BYTES:
                for n in range(HISTORY_KEEP, 0, -1):
                    older = path if n == 1 else path.with_name(f"{path.name}.{n - 1}")
                    if older.exists():
                        older.replace(path.with_name(f"{path.name}.{n}"))
            with path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            print(f"agent-coord: history: {e}", file=sys.stderr, flush=True)

    def _log_end(self, x: dict, held: bool, **extra) -> None:
        now = time.time()
        if held:
            granted = x.get("granted", now)
            self._log("ended", x, wait=round(granted - x["asked"], 1),
                      run=round(now - granted, 1), held=bool(x.get("running")), **extra)  # fmt: skip
        else:
            self._log("cancelled", x, wait=round(now - x["asked"], 1), **extra)

    def _prune(self) -> bool:
        changed = False
        for key in ("leases", "queue"):
            live = []
            for x in self.state[key]:
                if alive(x["pid"]):
                    live.append(x)
                else:
                    self._log_end(x, key == "leases", why="process exited")
            changed |= len(live) != len(self.state[key])
            self.state[key] = live
        now = time.time()
        for name, lock in list(self.state["locks"].items()):
            if (lock.get("pid") and not alive(lock["pid"])) or (
                lock.get("expires") and lock["expires"] < now
            ):
                del self.state["locks"][name]
                changed = True
        return changed

    def _lock_holder(self, name: str, lease_id: str | None = None) -> str | None:
        """Who holds named resource name (a lock, or another lease's --lock)."""
        if lock := self.state["locks"].get(name):
            return f"{lock['agent']} ({lock['reason']})"
        for x in self.state["leases"]:
            if name in x.get("locks", ()) and x["id"] != lease_id:
                return f"{x['agent']}'s lease {x['id']} ({x['label']})"
        return None

    def _reserved(self, mount: str) -> int:
        return sum(x.get("disk", {}).get(mount, 0) for x in self.state["leases"])

    def _seen(self, agent: str, cwd: str | None = None) -> None:
        entry = self.state["agents"].setdefault(agent, {})
        entry["last_seen"] = time.time()
        if cwd:
            entry["cwd"] = cwd

    def _post(self, sender: str, to: str, text: str) -> None:
        self.state["messages"].append(
            {"id": self.state["next"], "time": time.time(), "from": sender,
             "to": to, "text": text}
        )
        self.state["next"] += 1
        del self.state["messages"][:-MAX_MESSAGES]

    # -- admission

    def _vms(self) -> list[dict]:
        """Measured VMs not already counted by a lease (a hold on the VM)."""
        held = {x["pid"] for x in self.state["leases"]}
        return [g for g in self.vms if g["pid"] not in held]

    def _unleased(self) -> float:
        """Cores busy with work nobody leased (agents not using agent-coord)."""
        leased = sum(x["cores"] for x in self.state["leases"])
        return max(0.0, self.busy - sum(g["cores"] for g in self.vms) - leased)

    def _warming(self) -> list[dict]:
        """Leases started in the last WARMUP seconds: not yet at full speed,
        so not yet fully in the measurement."""
        now = time.time()
        return [x for x in self.state["leases"]
                if not x.get("running") and now - x.get("granted", now) < WARMUP]  # fmt: skip

    def _usage(self) -> tuple[float, int]:
        """(cores, memory) in use. CPU is measured, since leases mostly use
        less than they ask and sharing CPU only slows everyone a little (each
        lease is capped at its size, at nice 19); new leases count at their
        size until they ramp up. Memory counts every lease at its size, since
        running out of it means swapping or killing."""
        warming = {x["id"] for x in self._warming()}
        # Leases exclusive on a lock (timing runs) count in full all along, so
        # their cores stay guaranteed. Conservative: their measured use is in
        # self.busy too.
        cores = self.busy + sum(x["cores"] for x in self.state["leases"]
                                if x["id"] in warming or x.get("exclusive_locks"))  # fmt: skip
        vms = self._vms()
        mem = sum(x["mem"] for x in self.state["leases"]) + sum(g["mem"] for g in vms)
        return cores, mem

    def _claimed(self) -> float:
        """Cores claimed: every lease at its full size, plus VMs and unleased
        load as measured. --headroom is kept against this, not measured use,
        since a lease may speed up at any moment (up to its CPUQuota)."""
        leased = sum(x["cores"] for x in self.state["leases"])
        return leased + sum(g["cores"] for g in self._vms()) + self._unleased()

    def _too_big(self, req: dict) -> str | None:
        cores, mem = self.capacity()
        if req["cores"] + req.get("headroom", 0) > cores or req["mem"] > mem:
            return f"more than this computer has ({cores:g} cores, {human(mem)})"
        return None

    def _why(self, req: dict) -> tuple[str, str] | None:
        """(kind, reason) req can't start now, ignoring the line, or None.
        kind is a stable category (exclusive, lock:NAME, disk, cores, mem,
        available, headroom, too-big) for the history."""
        leases = self.state["leases"]
        holder = next((x for x in leases if x["exclusive"]), None)
        if holder:
            return "exclusive", f"{holder['agent']} holds an exclusive lease ({holder['label']})"
        if req["exclusive"] and leases:
            return "exclusive", f"{len(leases)} other lease(s) must end first"
        for name in req.get("locks", ()):
            if holder := self._lock_holder(name, req.get("id")):
                return f"lock:{name}", f"{name} is held by {holder}"
        for mount, size in req.get("disk", {}).items():
            free = self.disk_free(mount) - self._reserved(mount) - DISK_MARGIN
            if size > free:
                return "disk", (f"{human(max(free, 0))} of disk free on {mount} after "
                                f"reservations and a {human(DISK_MARGIN)} margin")  # fmt: skip
        if too_big := self._too_big(req):
            return "too-big", too_big
        cores, mem = self.capacity()
        used_cores, used_mem = self._usage()
        if used_cores + req["cores"] > cores:
            return "cores", f"{max(cores - used_cores, 0):.1f} of {cores:g} cores free"
        if used_mem + req["mem"] > mem:
            return "mem", f"{human(max(mem - used_mem, 0))} of {human(mem)} free"
        if self.available is not None and req["mem"] > self.available - RESERVE_MEM:
            return "available", (f"only {human(max(self.available - RESERVE_MEM, 0))} "
                                 "of memory actually available")  # fmt: skip
        keeper = max([req, *leases], key=lambda x: x.get("headroom", 0))
        if need := keeper.get("headroom", 0):
            claimed = self._claimed()
            if claimed + req["cores"] + need > cores:
                whose = "this request" if keeper is req else (
                    f"{keeper['agent']}'s lease {keeper['id']} ({keeper['label']})")
                return "headroom", (f"{max(cores - claimed, 0):.1f} cores unclaimed; "
                                    f"{need:g} are kept free for {whose}")  # fmt: skip
        return None

    def _stuck(self, req: dict) -> bool:
        """Whether req has waited STUCK_AFTER for memory that no lease's end
        would free. Each lease is counted as giving back what it really holds,
        at most its size. Plain --exclusive requests wait for every lease
        anyway and keep their own rules."""
        if req["exclusive"] or self.available is None:
            return False
        if time.time() - req["asked"] < STUCK_AFTER:
            return False
        if req["mem"] <= self.available - RESERVE_MEM:
            return False  # fits now: anything holding it up is not memory
        freed = sum(min(x["mem"], self._footprint(x["pid"])) for x in self.state["leases"])
        return req["mem"] > self.available + freed - RESERVE_MEM

    def _blocked(self, req: dict) -> str | None:
        """Why req can't start now, ignoring the line, or None."""
        why = self._why(req)
        return why[1] if why else None

    def _admit(self) -> bool:
        # Cache only within this pass; new measurements are visible next time.
        self._footprints = {}
        try:
            return self._admit_pass()
        finally:
            self._footprints = None

    def _footprint(self, pid: int) -> int:
        if self._footprints is None:
            return self.footprint(pid)
        if pid not in self._footprints:
            self._footprints[pid] = self.footprint(pid)
        return self._footprints[pid]

    def _admit_pass(self) -> bool:
        """Start requests in order, backfilling around a blocked head without
        postponing its projected start (exclusive windows keep their rules)."""
        changed = False
        while self.state["queue"]:
            head = self.state["queue"][0]
            if "head_since" not in head:
                head["head_since"] = time.time()
                changed = True
            why = self._why(head)
            if why:
                kind, reason = why
                if head.get("reason_kind") != kind:
                    head["reason"] = reason
                    head["reason_kind"] = kind
                    self._log("waiting", head, kind=kind, reason=reason,
                              waited=round(time.time() - head["asked"], 1))  # fmt: skip
                    changed = True
                if not head.get("nudged") and time.time() - head["asked"] > NUDGE_AFTER:
                    head["nudged"] = True
                    for holder in self.state["leases"]:
                        self._post(
                            "agent-coord", holder["agent"],
                            f"{head['agent']} has waited "
                            f"{(time.time() - head['asked']) / 60:.0f} min for "
                            f"{head['cores']:g} cores/{human(head['mem'])} "
                            f"({head['label']}); you hold lease {holder['id']} "
                            f"({holder['label']}).",
                        )  # fmt: skip
                    changed = True
                if not head.get("stuck_told") and self._stuck(head):
                    head["stuck_told"] = True
                    self._post(
                        "agent-coord", head["agent"],
                        f"{head['id']} ({head['label']}) needs {human(head['mem'])}, more than "
                        "would be available even if every lease ended; other jobs may now "
                        "pass it. Ask for less, or free the memory held outside leases.",
                    )  # fmt: skip
                    changed = True
                before = head.get("projected_start")
                self._projection(head)
                changed |= before != head.get("projected_start")
                return self._backfill(head) or changed
            self.state["queue"].pop(0)
            self._grant(head)
            changed = True
        return changed

    def _grant(self, req: dict) -> None:
        req["granted"] = time.time()
        if not req.get("backfill_reason", "").startswith("backfilled:"):
            req.pop("backfill_reason", None)
            req.pop("backfill_head", None)
        req.pop("reason", None)
        req.pop("reason_kind", None)
        for key in ("head_since", "projected_start", "projected_ending"):
            req.pop(key, None)
        self.state["leases"].append(req)
        self._log("started", req, wait=round(req["granted"] - req["asked"], 1))

    def _lock_wait(self, head: dict) -> set[str]:
        """The named locks head is waiting for (held by someone else)."""
        return {n for n in head.get("locks", ()) if self._lock_holder(n, head.get("id"))}

    def _leaves_room(self, req: dict, head: dict, waiting_for: set[str]) -> bool:
        """Whether req can run alongside head once head's lock frees: head
        keeps its place, so backfill never makes it wait for cores or memory.
        Leases holding the awaited lock end when it frees, so their share
        counts as free."""
        cores, mem = self.capacity()
        used_cores, used_mem = self._usage()
        ending = [x for x in self.state["leases"] if waiting_for & set(x.get("locks", ()))]
        freed_cores = sum(x["cores"] for x in ending)
        freed_mem = sum(x["mem"] for x in ending)
        if used_cores - freed_cores + req["cores"] + head["cores"] > cores:
            return False
        if used_mem - freed_mem + req["mem"] + head["mem"] > mem:
            return False
        if head.get("headroom") and (self._claimed() - freed_cores + req["cores"]
                                     + head["cores"] + head["headroom"] > cores):  # fmt: skip
            return False
        return self.available is None or (
            req["mem"] + head["mem"] <= self.available + freed_mem - RESERVE_MEM)

    def _remember(self, entry: dict) -> None:
        run = entry.get("run")
        if (entry.get("event") != "ended" or type(entry.get("code")) is not int
                or entry["code"] != 0 or entry.get("held") or entry.get("why")
                or type(run) not in (int, float) or not math.isfinite(run) or run <= 0):
            return
        for key in ((entry.get("agent"), entry.get("label")), (None, entry.get("label"))):
            samples = self._samples.setdefault(key, [])
            samples.append(run)
            del samples[:-20]

    def _runs(self, agent: str, label: str) -> list[float]:
        if self._samples is None:
            self._samples = {}
            for entry in read_history(0, self.history_file):
                self._remember(entry)
        return self._samples.get((agent, label)) or self._samples.get((None, label), [])

    def _estimate(self, agent: str, label: str) -> tuple[float, str]:
        runs = sorted(self._runs(agent, label))
        return ((runs[math.ceil(.9 * len(runs)) - 1], "history") if runs
                else (DEFAULT_EXPECT, "default"))

    def _end(self, lease: dict) -> float:
        return lease["granted"] + lease.get("expect", DEFAULT_EXPECT)

    def _holder_expect(self, lease: dict) -> float:
        if lease.get("expect_source") == "explicit":
            return lease.get("expect", DEFAULT_EXPECT)
        runs = self._runs(lease["agent"], lease["label"])
        if not runs:
            # Label-independent fallback, counting each successful run once.
            runs = [run for (agent, _), samples in self._samples.items()
                    if agent is None for run in samples]
        runs = sorted(runs)
        return ((runs[(len(runs) - 1) // 2] + runs[len(runs) // 2]) / 2
                if runs else DEFAULT_EXPECT)

    def _holder_end(self, lease: dict, now: float | None = None) -> float:
        now = time.time() if now is None else now
        end = lease["granted"] + self._holder_expect(lease)
        return end if end > now else now + PROJECTION_GRACE

    def _projected_fit(self, head: dict, ending: list[dict], req: dict | None = None) -> bool:
        """Use admission's measured CPU, full memory claims, and reserve any
        remaining memory growth against the measured available sample."""
        remaining = [x for x in self.state["leases"] if x not in ending]
        jobs = [*remaining, head, *([req] if req else [])]
        cores, mem = self.capacity()
        if sum(x["mem"] for x in jobs) + sum(g["mem"] for g in self._vms()) > mem:
            return False
        warming = self._warming()
        outside = self._unleased() + sum(g["cores"] for g in self._vms())
        if req is None:
            # Measured use finds the head's likely start, but subtracting a
            # full ending claim can undercount holders that remain.
            used = max(outside, self.busy - sum(x["cores"] for x in ending))
        else:
            used = outside + min(sum(x["cores"] for x in remaining),
                                 max(0, self.busy - outside))
        used += sum(x["cores"] for x in remaining if x in warming or x.get("exclusive_locks"))
        if used + head["cores"] + (req["cores"] if req else 0) > cores:
            return False
        headroom = max(x.get("headroom", 0) for x in jobs)
        if headroom and (sum(x["cores"] for x in jobs) + self._unleased()
                         + sum(g["cores"] for g in self._vms()) + headroom > cores):
            return False
        if self.available is not None:
            # Multiple leases may name the same process. Its measured memory
            # can be returned/reserved only once, including within this pass.
            measured = {}
            footprints = {}
            for x in self.state["leases"]:
                pid = x["pid"]
                budget = measured.setdefault(pid, self._footprint(pid))
                footprints[x["id"]] = min(x["mem"], budget)
                measured[pid] -= footprints[x["id"]]
            freed = sum(footprints[x["id"]] for x in ending)
            growth = sum(max(0, x["mem"] - footprints[x["id"]]) for x in remaining)
            need = head["mem"] + (req["mem"] if req else 0)
            if need > self.available + freed - growth - RESERVE_MEM:
                return False
        return True

    def _projection(self, head: dict) -> tuple[float | None, list[dict]]:
        now = time.time()
        ends = [(self._holder_end(x, now), x) for x in self.state["leases"]]
        for start in [now, *sorted({end for end, _ in ends})]:
            ending = [x for end, x in ends if end <= start]
            if self._projected_fit(head, ending):
                if start < head.get("projected_start", math.inf):
                    head["projected_start"] = start
                    head["projected_ending"] = [x["id"] for x in ending]
                break
        ids = head.get("projected_ending", [])
        return head.get("projected_start"), [x for x in self.state["leases"] if x["id"] in ids]

    def _overruns(self, head: dict) -> list[dict]:
        overdue = [x for x in self.state["leases"]
                   if x.get("backfill_head") == head.get("id") and self._end(x) <= time.time()]
        return overdue if overdue and self._projected_fit(head, overdue) else []

    def _easy(self, req: dict, head: dict) -> tuple[bool, str]:
        if self._overruns(head):
            return False, f"backfill held: overrun delays {head['id']}"
        if req.get("disk"):
            return False, "backfill held: disk reservations do not overtake capacity waits"
        now = time.time()
        start, ending = self._projection(head)
        if start is not None:
            # Expired estimates cannot tell us which holder ends first.
            # Protect every single departure that would let the head start,
            # even when the pinned projection grouped several holders.
            for holder in self.state["leases"]:
                if (holder["granted"] + self._holder_expect(holder) <= now
                        and self._projected_fit(head, [holder])
                        and not self._projected_fit(head, [holder], req)):
                    return False, f"backfill held: would delay {head['id']} after an overdue holder ends"
            if now + req.get("expect", DEFAULT_EXPECT) <= start:
                return True, f"backfilled: expected end before {head['id']} projected start"
            if self._projected_fit(head, ending, req):
                return True, f"backfilled: leaves capacity for {head['id']} projected start"
            return False, f"backfill held: would delay {head['id']} projected start"
        return False, f"backfill held: no projected start for {head['id']}"

    def _backfill(self, _head: dict) -> bool:
        """Start the requests behind a blocked head that fit and may pass."""
        started = False
        for holder in self._overruns(_head):
            if holder.get("overrun_told") != _head["id"]:
                holder["overrun_told"] = _head["id"]
                reason = (f"backfill overrun: lease {holder['id']} delays {_head['id']} "
                          f"({_head['label']}); no new backfill until capacity frees")
                holder["overrun_reason"] = reason
                self._post("agent-coord", holder["agent"], reason)
                self._log("overrun", holder, reason=reason)
                started = True
        for req in list(self.state["queue"][1:]):
            req.pop("backfill_reason", None)
            req.pop("backfill_head", None)
            why = self._why(req)
            if not why and self._may_pass(req):
                self.state["queue"].remove(req)
                self._grant(req)
                started = True
            else:
                reason = why[1] if why else req.get("backfill_reason", "backfill held: waiting in line")
                kind = why[0] if why else reason
                if req.get("reason_kind") != kind:
                    req["reason"] = reason
                    req["reason_kind"] = kind
                    self._log("waiting", req, kind=kind, reason=reason)
                    started = True
        return started

    def _may_pass(self, req: dict) -> bool:
        """Whether req (which fits now) may start ahead of the requests before
        it in line (the whole line, for a check).

        - Plain --exclusive requests never overtake.
        - An --exclusive request waits for every lease to end, so a small one
          (SMALL_CORES, SMALL_MEM, no disk, none of its locks) only delays it
          by its own run time: small ones pass it until it has waited
          EXCLUSIVE_PASS_WINDOW. After that nothing passes it, so its wait is
          bounded by the window plus the longest lease already running.
        - When the head waits only for a named lock (a GPU job while another
          holds the GPU), any request that doesn't need that lock may start if
          it leaves room for the head (cores, memory, headroom), so hours of
          GPU work don't stall every CPU job behind it.
        - Capacity waits keep the tiny pass rule alongside EASY backfill:
          finish before the pinned head start or leave its capacity free.
          Larger jobs stop passing BACKFILL_PASS_WINDOW after head arrival.
          Disk and lock safeguards remain.
        - A stuck request (see _stuck) doesn't count as being ahead: no lease
          ending would let it start, so the line behind it moves on, in order.
          It takes its turn again as soon as it would fit."""
        queue = self.state["queue"]
        ahead = queue[: queue.index(req)] if req in queue else queue
        if not ahead:
            return True

        def hold(reason):
            req["backfill_reason"] = f"backfill held: {reason}"
            return False

        if req["exclusive"]:
            return hold("exclusive requests never overtake")
        for x in ahead:
            if not self._stuck(x) and set(req.get("locks", ())) & set(x.get("locks", ())):
                return hold(f"lock order behind {x.get('id', 'queued request')}")
        now = time.time()
        small = req["cores"] <= SMALL_CORES and req["mem"] <= SMALL_MEM and not req.get("disk")
        for x in ahead:
            if x["exclusive"] and not (
                small and now - x["asked"] < EXCLUSIVE_PASS_WINDOW
                and not set(req.get("locks", ())) & set(x.get("locks", ()))
            ):  # fmt: skip
                return hold("exclusive pass window or size limit")
        skipped_stuck = any(self._stuck(x) for x in ahead)
        ahead = [x for x in ahead if not self._stuck(x)]
        if not ahead:
            return True
        head = ahead[0]
        if head["exclusive"]:
            return True
        waiting_for = self._lock_wait(head)
        if waiting_for:
            return self._leaves_room(req, head, waiting_for) or hold("lock-wait head capacity reservation")
        if head.get("locks"):
            return hold("lock-owning head waits for capacity")
        if skipped_stuck:
            return (req["cores"] <= BACKFILL_CORES and req["mem"] <= BACKFILL_MEM
                    and not req.get("disk")) or hold("ordering behind a stuck head")
        if req["cores"] <= BACKFILL_CORES and req["mem"] <= BACKFILL_MEM and not req.get("disk"):
            return True
        if now - head.get("head_since", now) >= BACKFILL_PASS_WINDOW:
            return hold("capacity pass window expired")
        if self._overruns(head):
            return hold(f"overrun delays {head['id']}")
        allowed, reason = self._easy(req, head)
        req["backfill_reason"] = reason
        if allowed:
            req["backfill_head"] = head.get("id")
        return allowed

    def tick(self) -> None:
        sample = self.measure()
        with self.lock:
            self.vms = sample["vms"]
            self.available = sample["available"]
            # Smoothed over ~10 s: a moment's burst shouldn't hold the line.
            self.busy += 0.2 * (sample["busy"] - self.busy)
            changed = self._prune()
            changed |= self._admit()
            if changed:
                self._save()

    # -- requests

    def handle(self, req: dict) -> dict:
        with self.lock:
            op = req.get("op")
            agent = req.get("agent") or "unknown"
            if req.get("agent"):
                self._seen(agent, req.get("cwd"))
            result = getattr(self, f"op_{op}", None)
            if result is None:
                return {"error": f"unknown op {op!r}"}
            reply = result(req, agent)
            self._prune()
            self._admit()
            self._save()
            return reply

    def op_acquire(self, req: dict, agent: str) -> dict:
        for key in ("cores", "mem", "pid", "label"):
            if key not in req:
                return {"error": f"missing {key}"}
        if req["cores"] <= 0 or req["mem"] < 0:
            return {"error": "cores must be positive and mem not negative"}
        scoped = [str(n) for n in req.get("exclusive_locks") or []]
        headroom = float(req.get("headroom") or 0)
        if scoped and req.get("exclusive"):
            return {"error": "--exclusive already excludes everything; drop --exclusive-lock"}
        if headroom < 0:
            return {"error": "headroom must not be negative"}
        expect = req.get("expect")
        if expect is not None and (type(expect) not in (int, float) or not math.isfinite(expect) or expect <= 0):
            return {"error": "expect must be a positive finite duration"}
        estimate, source = ((expect, "explicit") if expect is not None
                            else self._estimate(agent, str(req["label"])))
        locks = list(dict.fromkeys([*(str(n) for n in req.get("locks") or []), *scoped]))
        lease = {
            "id": f"L{self.state['next']}", "agent": agent, "pid": int(req["pid"]),
            "cores": float(req["cores"]), "mem": int(req["mem"]),
            "label": str(req["label"]), "exclusive": bool(req.get("exclusive")),
            "asked": time.time(), "cwd": req.get("cwd", ""),
            "locks": locks, "expect": estimate, "expect_source": source,
            "disk": {mount_of(p): int(n) for p, n in (req.get("disk") or {}).items()},
        }  # fmt: skip
        # Only set when used, so the state stays readable by older versions.
        if scoped:
            lease["exclusive_locks"] = scoped
        if headroom:
            lease["headroom"] = headroom
        self.state["next"] += 1
        too_big = self._too_big(lease)
        if too_big and not req.get("running"):
            return {"error": f"asks for {too_big}"}
        if req.get("running"):
            # A job already running: record it as it is (it isn't asking to
            # start), so it counts against everything that is waiting.
            self._log("requested", lease, held=True)
            lease["granted"] = time.time()
            lease["running"] = True
            self.state["leases"].append(lease)
            self._log("started", lease, wait=0.0, held=True)
        else:
            self.state["queue"].append(lease)
            self._log("requested", lease, reason=self._blocked(lease))
        self._admit()
        return {"id": lease["id"], **self._ticket(lease["id"])}

    def _ticket(self, lease_id: str) -> dict:
        if any(x["id"] == lease_id for x in self.state["leases"]):
            return {"state": "granted"}
        for position, x in enumerate(self.state["queue"]):
            if x["id"] == lease_id:
                reason = self._blocked(x) or x.get("backfill_reason") or x.get("reason") or "waiting"
                if position:
                    reason = f"{position} request(s) ahead in line; {reason}"
                return {"state": "waiting", "reason": reason}
        return {"state": "gone"}

    def op_poll(self, req: dict, _agent: str) -> dict:
        return self._ticket(str(req.get("id")))

    def op_release(self, req: dict, _agent: str) -> dict:
        lease_id = str(req.get("id"))
        extra = {k: req[k] for k in ("code", "why") if req.get(k) is not None}
        found = False
        for key in ("leases", "queue"):
            for x in [x for x in self.state[key] if x["id"] == lease_id]:
                self._log_end(x, key == "leases", **extra)
                found = True
            self.state[key] = [x for x in self.state[key] if x["id"] != lease_id]
        return {"released": found} if found else {"error": f"no lease {lease_id}"}

    def op_check(self, req: dict, _agent: str) -> dict:
        expect = req.get("expect")
        if expect is not None and (type(expect) not in (int, float) or not math.isfinite(expect) or expect <= 0):
            return {"error": "expect must be a positive finite duration"}
        if expect is None:
            expect, _ = self._estimate(_agent, str(req.get("label", "")))
        scoped = list(req.get("exclusive_locks") or [])
        probe = {"cores": float(req.get("cores", 1)), "mem": int(req.get("mem", 0)),
                 "exclusive": bool(req.get("exclusive")),
                 "locks": list(dict.fromkeys([*req.get("locks", []), *scoped])),
                 "exclusive_locks": scoped, "headroom": float(req.get("headroom") or 0),
                 "disk": req.get("disk", {}), "expect": expect}  # fmt: skip
        reason = self._blocked(probe)
        if not reason and self.state["queue"] and not self._may_pass(probe):
            reason = probe.get("backfill_reason") or f"{len(self.state['queue'])} request(s) already waiting"
        return {"ok": reason is None, "reason": reason}

    def op_lock(self, req: dict, agent: str) -> dict:
        name = str(req.get("name", "")).strip()
        if not name:
            return {"error": "lock needs a name"}
        if (lock := self.state["locks"].get(name)) and lock["agent"] != agent:
            return {"error": f"{name} is held by {self._lock_holder(name)}"}
        if (holder := self._lock_holder(name)) and not lock:
            return {"error": f"{name} is held by {holder}"}
        ttl = req.get("ttl")
        self.state["locks"][name] = {
            "agent": agent, "reason": str(req.get("reason", "")), "since": time.time(),
            "pid": int(req["pid"]) if req.get("pid") else None,
            "expires": time.time() + float(ttl) if ttl else None,
        }  # fmt: skip
        return {"locked": name}

    def op_unlock(self, req: dict, agent: str) -> dict:
        name = str(req.get("name", ""))
        lock = self.state["locks"].get(name)
        if not lock:
            return {"error": f"{name} isn't locked"}
        if lock["agent"] != agent and not req.get("force"):
            return {"error": f"{name} is held by {lock['agent']}; --force to take it"}
        del self.state["locks"][name]
        if lock["agent"] != agent:
            self._post(agent, lock["agent"], f"I force-unlocked {name} ({lock['reason']}).")
        return {"unlocked": name}

    def op_send(self, req: dict, agent: str) -> dict:
        text, to = str(req.get("text", "")).strip(), str(req.get("to", "")).strip()
        if not text or not to:
            return {"error": "send needs --to and a message"}
        self._post(agent, to, text)
        return {"sent": self.state["next"] - 1}

    def op_inbox(self, req: dict, agent: str) -> dict:
        cursor = 0 if req.get("all") else self.state["read"].get(agent, 0)
        mine = [m for m in self.state["messages"]
                if m["id"] > cursor and m["to"] in (agent, "all")
                and m["from"] != agent]  # fmt: skip
        if mine:
            self.state["read"][agent] = max(
                self.state["read"].get(agent, 0), mine[-1]["id"]
            )
        return {"messages": mine}

    def op_agents(self, _req: dict, _agent: str) -> dict:
        agents = sorted(self.state["agents"].items(),
                        key=lambda kv: -kv[1].get("last_seen", 0))  # fmt: skip
        return {"agents": [{"name": k, **v} for k, v in agents]}

    def op_status(self, _req: dict, _agent: str) -> dict:
        cores, mem = self.capacity()
        used_cores, used_mem = self._usage()
        return {
            "capacity": {"cores": cores, "mem": mem, "reserve": RESERVE_MEM},
            "free": {"cores": cores - used_cores, "mem": mem - used_mem},
            "leases": self.state["leases"],
            "queue": [{**x, "reason": self._blocked(x) or x.get("backfill_reason") or x.get("reason") or "waiting"}
                      for x in self.state["queue"]],
            "vms": self._vms(), "load": os.getloadavg(),
            "locks": self.state["locks"],
            "disks": {m: {"free": self.disk_free(m), "reserved": self._reserved(m)}
                      for m in sorted({m for x in self.state["leases"]
                                       for m in x.get("disk", {})})},
            "unleased_cores": round(self._unleased(), 1),
            "busy_cores": round(self.busy, 1),
            "available": self.available,
        }


def serve(
    socket_path: Path | None = None, state_file: Path | None = None, **coordinator_args
) -> None:
    socket_path, state_file = socket_path or SOCKET, state_file or STATE_FILE
    coordinator = Coordinator(state_file, **coordinator_args)
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path.unlink(missing_ok=True)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline()
            try:
                reply = coordinator.handle(json.loads(line))
            except (ValueError, TypeError, KeyError) as e:
                reply = {"error": f"bad request: {e}"}
            self.wfile.write(json.dumps(reply).encode() + b"\n")

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    # Measure before answering anyone: admission needs real numbers, and the
    # smoothed CPU starts from a real reading rather than zero.
    first = coordinator.measure()
    coordinator.vms, coordinator.available = first["vms"], first["available"]
    coordinator.busy = first["busy"]
    with Server(str(socket_path), Handler) as server:
        os.chmod(socket_path, 0o600)

        def ticker() -> None:
            while True:
                try:
                    coordinator.tick()
                except Exception as e:  # noqa: BLE001 - keep serving; log for journalctl
                    print(f"agent-coord: tick failed: {e}", file=sys.stderr, flush=True)
                time.sleep(TICK)

        threading.Thread(target=ticker, daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
            target=server.shutdown).start())  # fmt: skip
        print(f"agent-coord: serving on {socket_path}", flush=True)
        server.serve_forever()


# ---------------------------------------------------------------- client


def call(request: dict, socket_path: Path | None = None, retry: float = 0) -> dict:
    """Send request; retry for up to retry seconds while the service restarts."""
    deadline = time.monotonic() + retry
    while True:
        try:
            return _call(request, socket_path or SOCKET)
        except _ServiceError:
            raise
        except SystemExit:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def _call(request: dict, socket_path: Path) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(30)
            sock.connect(str(socket_path))
            sock.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as e:
        raise SystemExit(
            f"agent-coord: the service isn't reachable ({e}). "
            "Start it: systemctl --user start agent-coord"
        ) from e
    try:
        reply = json.loads(data)
    except ValueError as e:  # cut off mid-reply, e.g. by a service restart
        raise SystemExit(f"agent-coord: no answer from the service ({e})") from e
    if "error" in reply:
        raise _ServiceError(f"agent-coord: {reply['error']}")
    return reply


class _ServiceError(SystemExit):
    """The service answered with an error (not worth retrying)."""


def _wait_for(lease_id: str, reply: dict, max_wait: float | None) -> None:
    deadline = None if max_wait is None else time.monotonic() + max_wait
    said, said_at = None, 0.0
    while reply["state"] == "waiting":
        # Free capacity changes by the second: repeat at most once a minute.
        if said is None or (reply["reason"] != said and time.monotonic() - said_at > 60):
            print(f"agent-coord: {lease_id} waiting: {reply['reason']}",
                  file=sys.stderr, flush=True)  # fmt: skip
            said, said_at = reply["reason"], time.monotonic()
        if deadline is not None and time.monotonic() > deadline:
            call({"op": "release", "id": lease_id, "why": "max-wait"}, retry=120)
            print("agent-coord: gave up waiting", file=sys.stderr)
            raise SystemExit(75)
        time.sleep(POLL)
        reply = call({"op": "poll", "id": lease_id}, retry=120)
    if reply["state"] != "granted":
        raise SystemExit(f"agent-coord: lease {lease_id} vanished")


def mount_of(path: str) -> str:
    """The mount point holding path (or the nearest existing parent)."""
    full = Path(os.path.realpath(os.path.expanduser(path)))
    while not full.exists() and full != full.parent:
        full = full.parent
    best = "/"
    for line in Path("/proc/mounts").read_text().splitlines():
        point = line.split()[1].replace("\\040", " ")
        if (str(full) == point or str(full).startswith(point.rstrip("/") + "/")) and len(
            point
        ) > len(best):
            best = point
    return best


def disk_free(mount: str) -> int:
    st = os.statvfs(mount)
    return st.f_bavail * st.f_frsize


# Options naming where a command works or writes (output, temp, work dirs).
_OUTPUT_OPTIONS = {"-o", "-O", "--output", "--out", "--outdir", "--output-dir",
                   "--output_vcf", "--out-bam", "--out-variants", "-T", "--tmp-dir",
                   "--TMP_DIR", "--temp-dir", "--intermediate_results_dir", "-d",
                   "--directory", "-w", "--workdir"}  # fmt: skip


def archive_mounts() -> list[str]:
    """Mount points of the HDD archive pools (ZFS): reference archives only;
    active work (alignment, sorting, calling) belongs on the SSDs."""
    mounts = []
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[2] == "zfs":
            mounts.append(fields[1].replace("\\040", " "))
    return mounts


def _on_archive(path: str, mounts: list[str]) -> str | None:
    full = os.path.realpath(os.path.expanduser(path))
    return next((m for m in mounts if full == m or full.startswith(m.rstrip("/") + "/")), None)


def archive_writes(cmd: list[str], cwd: str, mounts: list[str]) -> list[str]:
    """Where cmd would work or write on an archive pool: its working
    directory, and the values of output/temp options (-o X, --outdir=X)."""
    found = []
    if mount := _on_archive(cwd, mounts):
        found.append(f"working directory {cwd} (on {mount})")
    for i, arg in enumerate(cmd):
        name, eq, value = arg.partition("=")
        if eq and name in _OUTPUT_OPTIONS:
            target = value
        elif arg in _OUTPUT_OPTIONS and i + 1 < len(cmd):
            target = cmd[i + 1]
        else:
            continue
        if mount := _on_archive(target, mounts):
            found.append(f"{arg.split('=')[0]} {target} (on {mount})")
    return found


def limited(cmd: list[str], cores: float, mem: int) -> list[str]:
    """cmd in a systemd user scope capped at cores and mem, at low priority."""
    low = ["nice", "-n", "19", "ionice", "-c", "3", *cmd]
    bus = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "bus")
    if not shutil.which("systemd-run") or not bus.exists():
        print("agent-coord: no systemd user session; running uncapped at low "
              "priority", file=sys.stderr)  # fmt: skip
        return low
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "-p", f"CPUQuota={max(int(cores * 100), 1)}%", "-p", f"MemoryMax={mem}",
        "-p", "MemorySwapMax=0", "--", *low,
    ]  # fmt: skip


def parse_disk(text: str) -> tuple[str, int]:
    path, sep, size = text.rpartition(":")
    if not sep or not path:
        raise argparse.ArgumentTypeError(f"not PATH:SIZE: {text} (e.g. /home:150G)")
    return path, parse_size(size)


def _lease_request(args: argparse.Namespace, pid: int) -> dict:
    return {"op": "acquire", "agent": args.agent, "cwd": os.getcwd(), "pid": pid,
            "cores": args.cores, "mem": args.mem, "label": args.label,
            "exclusive": args.exclusive, "locks": args.lock or [],
            "exclusive_locks": args.exclusive_lock or [], "headroom": args.headroom,
            "expect": args.expect, "disk": dict(args.disk or [])}  # fmt: skip


def cmd_run(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("agent-coord run: give the command after --")
    if not args.allow_archive and (bad := archive_writes(command, os.getcwd(),
                                                         archive_mounts())):  # fmt: skip
        raise SystemExit(
            "agent-coord: the HDD archive pool holds reference archives only; run "
            "heavy work on the SSDs (/home, /mnt/Windows). This command would work "
            "or write there: " + "; ".join(bad) + ". Reading inputs from it is "
            "fine. Override with --allow-archive (e.g. archiving a finished result)."
        )
    reply = call(_lease_request(args, os.getpid()))
    lease_id = reply["id"]
    child = None
    end: dict = {}

    def forward(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)
        else:
            raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    try:
        _wait_for(lease_id, reply, args.max_wait)
        print(f"agent-coord: {lease_id} started ({args.cores:g} cores, "
              f"{human(args.mem)})", file=sys.stderr, flush=True)  # fmt: skip
        env = dict(os.environ, COMPUTE_LEASE_CORES=str(max(int(args.cores), 1)))
        # JVMs size their heap from the host's RAM, not the scope's limit, and
        # get killed at it: tell them the lease's memory (heap up to 60% of it).
        java = f"-XX:MaxRAM={args.mem} -XX:MaxRAMPercentage=60"
        env["JAVA_TOOL_OPTIONS"] = f"{env.get('JAVA_TOOL_OPTIONS', '')} {java}".strip()
        child = subprocess.Popen(limited(command, args.cores, args.mem), env=env)
        code = child.wait()
        if code == -signal.SIGKILL:
            print(f"agent-coord: {lease_id} was killed; if it used more than "
                  f"--mem {human(args.mem)}, lease more", file=sys.stderr)  # fmt: skip
        end["code"] = 128 - code if code < 0 else code
        return end["code"]
    except KeyboardInterrupt:
        end.update(code=130, why="interrupted")
        return 130
    finally:
        try:
            call({"op": "release", "id": lease_id, **end}, retry=120)
        except SystemExit:
            pass


def cmd_hold(args: argparse.Namespace) -> int:
    if not alive(args.pid):
        raise SystemExit(f"agent-coord hold: no process {args.pid}")
    reply = call({**_lease_request(args, args.pid), "running": True})
    print(reply["id"])
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    reply = call({"op": "check", "cores": args.cores, "mem": args.mem,
                  "exclusive": args.exclusive, "locks": args.lock or [],
                  "exclusive_locks": args.exclusive_lock or [], "headroom": args.headroom,
                  "agent": args.agent or default_agent(), "label": args.label,
                  "expect": args.expect, "disk": {mount_of(p): n for p, n in args.disk or []}})  # fmt: skip
    print("ok" if reply["ok"] else f"no: {reply['reason']}")
    return 0 if reply["ok"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    s = call({"op": "status"})
    if args.json:
        print(json.dumps(s, indent=2))
        return 0
    cap, free = s["capacity"], s["free"]
    load = " ".join(f"{x:.1f}" for x in s["load"])
    print(f"{cap['cores']:g} cores, {human(cap['mem'])} for leases "
          f"({human(cap['reserve'])} kept back); free {free['cores']:.1f} cores, "
          f"{human(free['mem'])}; load {load}")  # fmt: skip
    if s.get("available") is not None:
        print(f"memory actually available now: {human(s['available'])} "
              f"(leases need theirs plus {human(cap['reserve'])} of it)")
    rows = [("held", x) for x in s["leases"]] + [("waiting", x) for x in s["queue"]]
    for state, x in rows:
        since = (time.time() - x.get("granted", x["asked"])) / 60
        flag = " EXCLUSIVE" if x["exclusive"] else ""
        if x.get("exclusive_locks"):
            flag += f" EXCLUSIVE-ON {','.join(x['exclusive_locks'])}"
        if x.get("headroom"):
            flag += f" headroom {x['headroom']:g} cores"
        if x.get("locks"):
            flag += f" locks {','.join(x['locks'])}"
        if x.get("disk"):
            flag += " disk " + ",".join(f"{m}:{human(n)}" for m, n in x["disk"].items())
        detail = x.get("reason") if state == "waiting" else x.get("overrun_reason") or x.get("backfill_reason")
        why = f" -- {detail}" if detail else ""
        print(f"  {x['id']:<6} {state:<7} {x['cores']:>4g} cores {human(x['mem']):>6} "
              f"{since:5.0f} min  {x['agent']}: {x['label']}{flag} (pid {x['pid']}){why}")
    if "busy_cores" in s:
        print(f"cores busy now (measured): {s['busy_cores']:g} of {cap['cores']:g}; "
              "CPU admission uses this, memory admission uses lease sizes")
    if s.get("unleased_cores"):
        print(f"  other  running {s['unleased_cores']:>4g} cores"
              "                   work not leased through agent-coord (measured)")
    for name, lock in s.get("locks", {}).items():
        since = (time.time() - lock["since"]) / 60
        until = (f", expires in {(lock['expires'] - time.time()) / 60:.0f} min"
                 if lock.get("expires") else "")  # fmt: skip
        print(f"  lock   {name:<22} {since:5.0f} min  {lock['agent']}: {lock['reason']}{until}")
    for mount, d in s.get("disks", {}).items():
        print(f"  disk   {mount:<22} {human(d['free'])} free, {human(d['reserved'])} reserved")
    for g in s["vms"]:
        print(f"  vm     running {g['cores']:>4g} cores {human(g['mem']):>6}"
              f"            {g['name'] or 'qemu'} (pid {g['pid']}, measured)")
    if not rows and not s["vms"]:
        print("  nothing held")
    return 0


def history_files(path: Path | None = None) -> list[Path]:
    """The history and its rotated files, oldest first."""
    path = path or HISTORY_FILE
    rotated = [path.with_name(f"{path.name}.{n}") for n in range(HISTORY_KEEP, 0, -1)]
    return [f for f in [*rotated, path] if f.exists()]


def read_history(since: float, path: Path | None = None) -> list[dict]:
    events = []
    for f in history_files(path):
        for line in f.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("time", 0) >= since:
                events.append(event)
    return events


def _minutes(seconds: float | None) -> str:
    return "" if seconds is None else f"{seconds / 60:.1f}m"


def cmd_history(args: argparse.Namespace) -> int:
    events = read_history(time.time() - args.since)
    if args.json:
        for event in events:
            print(json.dumps(event))
        return 0
    for e in events:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(e["time"]))
        flag = {"machine": " EXCLUSIVE", "lock": " EXCLUSIVE-ON "
                + ",".join(e.get("exclusive_locks", []))}.get(e.get("exclusive"), "")  # fmt: skip
        if e.get("locks"):
            flag += f" locks {','.join(e['locks'])}"
        times = []
        for key in ("wait", "waited", "run"):
            if key in e:
                times.append(f"{'wait' if key == 'waited' else key} {_minutes(e[key])}")
        if "code" in e:
            times.append(f"exit {e['code']}")
        detail = e.get("reason") or e.get("why") or e.get("backfill_reason") or ""
        print(f"{stamp} {e['event']:<9} {e['id']:<6} {e['cores']:>4g}c {human(e['mem']):>6} "
              f"{e['agent']}: {e['label']}{flag}"
              + (f" [{', '.join(times)}]" if times else "")
              + (f" -- {detail}" if detail else ""))  # fmt: skip
    if not events:
        print("no lease events in that time")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    call({"op": "send", "agent": args.agent, "cwd": os.getcwd(), "to": args.to,
          "text": " ".join(args.text)})  # fmt: skip
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + (args.wait or 0)
    while True:
        messages = call({"op": "inbox", "agent": args.agent, "cwd": os.getcwd(),
                         "all": args.all})["messages"]  # fmt: skip
        if messages or time.monotonic() >= deadline:
            break
        time.sleep(POLL)
    for m in messages:
        stamp = time.strftime("%H:%M", time.localtime(m["time"]))
        print(f"[{stamp}] {m['from']} -> {m['to']}: {m['text']}")
    if not messages:
        print("no new messages")
    return 0


def cmd_agents(_args: argparse.Namespace) -> int:
    for a in call({"op": "agents"})["agents"]:
        ago = (time.time() - a.get("last_seen", 0)) / 60
        print(f"  {a['name']:<32} seen {ago:5.0f} min ago  {a.get('cwd', '')}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    call({"op": "lock", "agent": args.agent, "cwd": os.getcwd(), "name": args.name,
          "reason": args.reason, "pid": args.pid, "ttl": args.ttl})  # fmt: skip
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    call({"op": "unlock", "agent": args.agent, "name": args.name, "force": args.force})
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    call({"op": "release", "id": args.id})
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    parser = argparse.ArgumentParser(prog="agent-coord", add_help=False)
    parser.add_argument("--as", dest="agent")
    subs = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "hold", "check"):
        sub = subs.add_parser(name)
        sub.add_argument("--cores", type=float, required=True)
        sub.add_argument("--mem", type=parse_size, required=True)
        sub.add_argument("--exclusive", action="store_true")
        sub.add_argument("--exclusive-lock", action="append")
        sub.add_argument("--headroom", type=float, default=0.0)
        sub.add_argument("--disk", type=parse_disk, action="append")
        sub.add_argument("--lock", action="append")
        sub.add_argument("--expect", type=parse_expect, help="expected run time, e.g. 10m (default: learned per agent/label, else 30m)")
        if name == "check":
            sub.add_argument("--label", default="", help="label to match learned run estimates")
            continue
        sub.add_argument("--label", required=True)
        sub.add_argument("--max-wait", type=parse_duration)
        if name == "hold":
            sub.add_argument("--pid", type=int, required=True)
        else:
            sub.add_argument("--allow-archive", action="store_true")
            sub.add_argument("command", nargs=argparse.REMAINDER)
    subs.add_parser("status").add_argument("--json", action="store_true")
    history = subs.add_parser("history")
    history.add_argument("--since", type=parse_duration, default=24 * 3600.0)
    history.add_argument("--json", action="store_true")
    send = subs.add_parser("send")
    send.add_argument("--to", required=True)
    send.add_argument("text", nargs="+")
    inbox = subs.add_parser("inbox")
    inbox.add_argument("--wait", type=parse_duration)
    inbox.add_argument("--all", action="store_true")
    subs.add_parser("agents")
    lock = subs.add_parser("lock")
    lock.add_argument("name")
    lock.add_argument("--reason", required=True)
    lock.add_argument("--pid", type=int)
    lock.add_argument("--ttl", type=parse_duration)
    unlock = subs.add_parser("unlock")
    unlock.add_argument("name")
    unlock.add_argument("--force", action="store_true")
    subs.add_parser("release").add_argument("id")
    subs.add_parser("serve")
    args = parser.parse_args(argv)
    args.agent = args.agent or default_agent()
    if getattr(args, "exclusive", False) and getattr(args, "exclusive_lock", None):
        parser.error("--exclusive already excludes everything; drop --exclusive-lock")
    if args.action == "serve":
        serve()
        return 0
    return {
        "run": cmd_run, "hold": cmd_hold, "check": cmd_check, "status": cmd_status,
        "history": cmd_history,
        "send": cmd_send, "inbox": cmd_inbox, "agents": cmd_agents,
        "release": cmd_release, "lock": cmd_lock, "unlock": cmd_unlock,
    }[args.action](args)  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
