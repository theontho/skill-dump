#!/usr/bin/env python3
"""agent-coord: a local coordinator for the agents (and people) on this computer.

A user service (agent-coord.service) keeps the state; agents talk to it with
this same file as a CLI. It hands out CPU/RAM leases first come, first served
(running QEMU guests count at their measured use), so heavy jobs queue instead
of piling up, and it carries messages between agents.
"""

import argparse
import contextlib
import json
import queue
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

# Tests and throwaway trials only: AGENT_COORD_RUNTIME_DIR and
# AGENT_COORD_STATE_DIR point an instance at its own socket, lease files, state
# and history. Set for the shared service, it would start from an empty state.
RUNTIME = Path(
    os.environ.get("AGENT_COORD_RUNTIME_DIR")
    or Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agent-coord"
)
SOCKET = RUNTIME / "sock"
STATE_FILE = (
    Path(
        os.environ.get("AGENT_COORD_STATE_DIR")
        or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-coord"
    )
    / "state.json"
)
# One JSON line per lease event, next to the state; rotated by size.
HISTORY_FILE = STATE_FILE.with_name("history.jsonl")
HISTORY_MAX_BYTES = 8 * 1024**2
HISTORY_KEEP = 4  # rotated files kept: history.jsonl.1 .. .4
# Left for the desktop, the agents themselves and unleased short commands.
RESERVE_MEM = 4 * 1024**3
TICK = 2.0
BUSY_SMOOTHING = 0.2  # per tick, for host-wide and per-lease CPU use
# Measured (smoothed) figures settle slightly off whole cores; a step this
# close to the next whole core counts as reaching it.
STEP_SLACK = 0.05
# Smoothed measurements leave hundredths of a core of noise: CPU capacity
# checks allow this much over.
CPU_EPS = 0.01
POLL = 3.0
# Holders hear about a request that has waited this long (once per request).
NUDGE_AFTER = 60
# A new lease counts at its full size this long, until its use is measured.
WARMUP = 60
IO_HINT_AFTER = 10 * 60
IO_DEFAULT_MEM = 512 * 1024**2
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

# Elastic leases (--cores MIN:MAX) and projects. Every number here can be
# changed in CONFIG_FILE (TOML, flat keys; [projects.NAME] weight = N); none
# is needed. See DEFAULT_CONFIG.
CONFIG_FILE = Path(
    os.environ.get("AGENT_COORD_CONFIG")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "agent-coord" / "config.toml"
)
DEFAULT_CONFIG = {
    # Cores no single project's fixed and exclusive leases (with headroom),
    # IO use and elastic MINs may take: kept for the other projects together.
    "reserve_cores": 2.0,
    # Weight of a project not named under [projects.NAME].
    "default_weight": 1.0,
    # Least time between two changes of one lease's size (preemption excepted).
    "resize_interval": 60.0,
    # An elastic lease using under idle_fraction of its grant for idle_after
    # seconds is offered down to max(MIN, ceil(peak use / idle_target_use)).
    "idle_fraction": 0.5,
    "idle_after": 120.0,
    "idle_target_use": 0.7,
    # It grows (toward MAX) while using at least grow_demand of its grant,
    # nothing waits for cores, and grow_margin cores stay free beyond the step.
    "grow_demand": 0.75,
    "grow_margin": 1.0,
}
# Fields a lease size change is logged and announced with.
CPU_WAIT_KINDS = ("cores", "headroom")
# Scopes are named by the service: PREFIX-LEASE-CLIENTPID.scope; the service
# resizes only scopes it named. AGENT_COORD_SCOPE_PREFIX keeps a private
# instance's names apart.
SCOPE_PREFIX = os.environ.get("AGENT_COORD_SCOPE_PREFIX") or "agent-coord"
if not re.fullmatch(r"[A-Za-z0-9_-]+", SCOPE_PREFIX):
    SCOPE_PREFIX = "agent-coord"
SCOPE_RE = re.compile(r"[A-Za-z0-9_-]+-L\d+-\d+\.scope")
RESIZE_TIMEOUT = 5.0  # seconds for one systemctl set-property
# A failed resize is retried after RESIZE_BACKOFF s, doubling; after
# RESIZE_ATTEMPTS failures in a row the lease is fixed at its size.
RESIZE_BACKOFF, RESIZE_ATTEMPTS = 30.0, 3
# A lease is resized only once its client reported its scope exists, or this
# long after its grant if the client never does.
SCOPE_READY_AFTER = 60.0
# The client protocol version new clients send; requests without it come from
# older clients, which are never refused while waiting (that would end a job
# another agent is waiting to run).
CLIENT_VERSION = 2

HELP = """\
agent-coord - share this computer's CPU and RAM between agents, and message them

QUOTA
  agent-coord check [--io] [--cores N|MIN:MAX] [--mem SIZE] [--exclusive | --exclusive-lock NAME]
      Exit 0 if a lease that size would start now, else 1 with the reason.
  agent-coord run [--io] [--cores N|MIN:MAX] [--mem SIZE] --label TEXT [--exclusive | --exclusive-lock NAME]
                  [--headroom N] [--expect D] [--max-wait D] [--notify-signal] -- CMD ...
      Wait in line until the cores and SIZE of RAM are free, then run CMD capped
      at them (systemd scope: CPUQuota, MemoryMax; exceeding MemoryMax kills
      CMD) at nice 19 and idle IO priority. CMD sees COMPUTE_LEASE_CORES=N:
      give its tools that many threads. The lease ends when CMD exits.
  --cores MIN:MAX  Elastic lease (prefer it for builds and test suites): starts
                as soon as MIN cores are free, with as many up to MAX as are free
                then. While it runs its cap may shrink toward MIN (when others
                wait, or when it uses under half for 2 min) and grow back
                toward MAX; it is never shrunk below MIN or killed for this.
                COMPUTE_LEASE_FILE names a file rewritten atomically on each
                change: line 1 the cores to use now, line 2 JSON (lease, min,
                max, granted, reason, time); poll it (the default way to
                follow changes). Also COMPUTE_LEASE_MIN/_MAX. A tool with a
                fixed thread count is only throttled when shrunk.
                A single N is a fixed lease, never resized: keep fixed and
                exclusive leases for timing runs.
  --notify-signal  Also send SIGUSR1 to CMD (only the process run starts) when
                its elastic grant changes. It TERMINATES a command that has not
                yet installed a SIGUSR1 handler, or that is a shell, make or
                another wrapper that does not handle it: prefer polling the
                lease file, and use this only for programs that handle SIGUSR1
                from their first moment.
  --project NAME   Fair-share group: counts on its own only if NAME is listed
                under [projects.NAME] in the config; otherwise the project is
                the git repository CMD runs in (named after its main checkout,
                worktrees included; for hold, the held process's; else the
                agent name).
  agent-coord hold [--io] [--cores N] [--mem SIZE] --label TEXT --pid PID [--exclusive]
      Register a job that is already running (no waiting: it is running),
      until PID exits. Prints the lease ID. Holding a VM's qemu PID replaces
      its measured use with the lease.
  agent-coord release ID
  --io          For downloads, uploads and disk streaming: count measured CPU
                use, even during warmup. Defaults to 1 core and 512M; both
                sizes are required without --io. Memory counts at least the
                requested size, or more if the process tree uses more.
  --exclusive   Whole-machine exclusive: wait until no other lease is held,
                then keep new ones waiting until this one ends. Small requests
                (<= 2 cores, 6G, no disk) may still start while it waits, for
                its first 30 min of waiting only.
  --exclusive-lock NAME
                Exclusive on resource NAME only (e.g. gpu, for a GPU timing
                run): holds NAME like --lock, waits until nobody else holds it,
                and its cores count in full (unless --io) so they stay
                guaranteed. CPU-only leases still run beside it in the rest of
                the machine. Repeatable. Prefer it to --exclusive.
  --headroom N  Start only when N cores beyond this lease are unclaimed
                (non-IO leases counted at their full size), and keep them unclaimed
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

  agent-coord status [--json]              Leases, line, VMs, free capacity,
                                           active projects and their shares.
  agent-coord history [--since D] [--json] Lease events (requested, waiting,
                                           started, resized, ended, cancelled)
                                           with wait/run times; default last 24h.

PROJECTS
  Use MIN:MAX for everything except timing runs. Elastic jobs share the
  machine fairly between active projects (the git repository; a --project
  name counts separately only if configured; fair share = cores x weight /
  active weights, weight 1 by default) and can use every core when others are
  idle: a waiting request shrinks other projects' elastic leases (never below
  MIN) as far as its project's fair share. Fixed and exclusive jobs are never
  shrunk. A project alone runs fixed jobs as before; while another project is
  active, one project's fixed jobs, timing runs with headroom, IO use and MINs
  are capped at all cores minus reserve_cores (2), counted at measured use like
  admission; a timing run asking more is refused. A project that runs only
  fixed jobs gets no fair-share protection. The line stays first come, first
  served; a request waiting for its own project's cap does not hold others.
  Optional ~/.config/agent-coord/config.toml: reserve_cores, resize timings,
  [projects.NAME] weight = 2 (bad values are ignored and shown in status).

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


def parse_cores(text: str) -> tuple[float, float | None]:
    """N (fixed) or MIN:MAX (elastic)."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?::(\d+(?:\.\d+)?))?", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"not cores: {text} (e.g. 4, or 2:8 for MIN:MAX)")
    low, high = float(match[1]), float(match[2]) if match[2] else None
    if low <= 0:
        raise argparse.ArgumentTypeError("cores must be positive")
    if high is not None and high < low:
        raise argparse.ArgumentTypeError(f"MAX below MIN in {text}")
    return low, (None if high == low else high)


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


def _cpu_seconds(pid: int, include_children: bool = False) -> float | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return None
    end = 15 if include_children else 13
    return sum(int(ticks) for ticks in fields[11:end]) / os.sysconf("SC_CLK_TCK")


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


def _tree_pids(pid: int):
    seen, stack = set(), [pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        yield current
        try:
            for task in Path(f"/proc/{current}/task").iterdir():
                stack.extend(int(x) for x in (task / "children").read_text().split())
        except OSError:
            pass  # exited, or no children file: counts as what was read so far


def tree_pids(pid: int) -> set[int]:
    return set(_tree_pids(pid))


def tree_footprint(pid: int) -> int:
    """Resident plus swapped-out memory of pid and its descendants."""
    return sum(_footprint(current) for current in _tree_pids(pid))


def tree_cpu_seconds(pid: int) -> float | None:
    """Cumulative CPU time of live and already reaped processes in the tree."""
    pids = _tree_pids(pid)
    root = _cpu_seconds(next(pids), include_children=True)
    if root is None:
        return None
    return root + sum(_cpu_seconds(child, include_children=True) or 0 for child in pids)


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


# Allowed range (low, high, low exclusive?) of each setting.
CONFIG_RANGES = {
    "reserve_cores": (0.0, 64.0, False),
    "default_weight": (0.0, 1000.0, True),
    "resize_interval": (0.0, 3600.0, False),
    "idle_fraction": (0.0, 1.0, False),
    "idle_after": (0.0, 86400.0, False),
    "idle_target_use": (0.0, 1.0, True),
    "grow_demand": (0.0, 1.0, True),
    "grow_margin": (0.0, 64.0, False),
}


def _config_number(value: object, low: float, high: float, open_low: bool) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if value > high or value < low or (open_low and value <= low):
        return None
    return float(value)


def load_config(path: Path | None) -> dict:
    """DEFAULT_CONFIG overlaid with the TOML file at path, if any. Every
    value is checked; a bad one keeps its default and is listed in
    config["errors"] (shown by status, logged once per load). Never raises."""
    config = {**DEFAULT_CONFIG, "projects": {}, "errors": []}
    try:
        if path is None or not path.exists():
            return config
        import tomllib

        data = tomllib.loads(path.read_text())
    except Exception as e:  # noqa: BLE001 - a bad file must never stop the service
        config["errors"].append(f"{path}: {e}; using defaults")
    else:
        for key, value in data.items():
            if key == "projects":
                continue
            if key not in CONFIG_RANGES:
                config["errors"].append(f"{path}: unknown setting {key}")
                continue
            low, high, open_low = CONFIG_RANGES[key]
            number = _config_number(value, low, high, open_low)
            if number is None:
                bound = f"{'above' if open_low else 'at least'} {low:g} and at most {high:g}"
                config["errors"].append(f"{path}: {key} = {value!r} is not a number {bound}; "
                                        f"using {DEFAULT_CONFIG[key]:g}")
            else:
                config[key] = number
        projects = data.get("projects", {})
        if not isinstance(projects, dict):
            config["errors"].append(f"{path}: projects must be a table ([projects.NAME]); ignored")
            projects = {}
        for name, entry in projects.items():
            weight = entry.get("weight") if isinstance(entry, dict) else None
            weight = _config_number(weight, 0.0, 1000.0, True)
            if weight is None:
                config["errors"].append(f"{path}: projects.{name} needs weight = a number above 0 "
                                        "and at most 1000; ignored")
            else:
                config["projects"][str(name)] = {"weight": weight}
    for error in config["errors"]:
        print(f"agent-coord: config {error}", file=sys.stderr, flush=True)
    return config


def default_project(agent: str, cwd: str | None = None) -> str:
    """The repository the command runs in (cwd, default the current
    directory), named after its main checkout's folder (worktrees of one
    repository are one project), else the agent."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True, timeout=5, cwd=cwd,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return agent
    if not common:
        return agent
    path = Path(common)
    name = path.parent.name if path.name == ".git" else path.name.removesuffix(".git")
    return name or agent


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
                 history_file: Path | None = None, footprint=tree_footprint,
                 cpu_seconds=tree_cpu_seconds, config_file: Path | None = None,
                 tree_pids=None, resize=None, resize_async=False):
        self.config_file = config_file
        self._config_mtime: float | None = None
        self.config = load_config(config_file)
        # Elastic leases using little of their grant: id -> (since, peak use).
        self._idle: dict[str, tuple[float, float]] = {}
        self._saved = None
        self._samples = None
        self._footprints = None
        self.disk_free = disk_free
        self.footprint = footprint
        self.cpu_seconds = cpu_seconds
        self._cpu_sample: tuple[float, dict[str, float]] | None = None
        self._measured_cores: dict[str, float] = {}
        # Uncapped per-lease use over the last sample (for idle and growth
        # decisions), and smoothed like self.busy (to take a lease's own use
        # out of the host-wide measurement when it is counted another way).
        self._raw_cores: dict[str, float] = {}
        self._own_cores: dict[str, float] = {}
        self._size_avg: dict[str, float] = {}
        self._trees: dict[str, set[int]] = {}
        self.tree_pids = tree_pids
        # Changes a running scope's CPU cap: (unit name, cores) -> success.
        # None (in-process use and tests): no scope can be resized. With
        # resize_async (the service), calls run on a worker thread, never
        # under self.lock; the result is applied when it is back.
        self.resize = resize or (lambda scope, cores: False)
        self.resize_async = resize_async
        self._pending: dict[str, tuple[float, str, str]] = {}  # id -> (cores, kind, note)
        self._jobs: "queue.Queue[tuple[str, str, float]]" = queue.Queue()
        self._low_since: dict[tuple[str, str], float] = {}
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
                 "exclusive": exclusive_mode(x), "io": bool(x.get("io")),
                 "project": self._project(x)}  # fmt: skip
        if x.get("exclusive_locks"):
            entry["exclusive_locks"] = x["exclusive_locks"]
        for key in ("headroom", "disk", "expect", "expect_source", "backfill_reason", "backfill_head",
                    "min_cores", "max_cores", "elastic"):
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
        """Cores busy with work nobody leased (agents not using agent-coord).
        A lease counted once (see _counted_once) counts here as in
        _net_busy: a just-shrunk elastic lease's use still in the smoothed
        busy figure is its own, not someone else's. For every other lease,
        and a timing run, this is its claim, as on main."""
        once, outer_of = self._counted_once(), self._nesting()
        leased = 0.0
        for x in self.state["leases"]:
            claim = self._cpu_claim(x)
            own = self._own_cores.get(x["id"])
            if own is not None and x["id"] in once and x["id"] not in outer_of:
                claim = max(claim, min(own, self._size_avg.get(x["id"], 0.0)))
            leased += claim
        return max(0.0, self.busy - sum(g["cores"] for g in self._vms()) - leased)

    def _eps(self) -> float:
        """Tolerance for CPU checks: smoothed measurements of leases counted
        once leave hundredths of a core of noise; none otherwise (as on main)."""
        return CPU_EPS if self._counted_once() else 0.0

    def _size(self, lease: dict) -> float:
        """Cores a lease may use now: its size (for an elastic lease, the
        grant its scope is capped at: the service caps it before counting)."""
        return lease["cores"]

    def _cpu_claim(self, lease: dict) -> float:
        """Full size for a CPU lease; observed use for IO (zero before sampling)."""
        return self._measured_cores.get(lease.get("id"), 0.0) if lease.get("io") else self._size(lease)

    def _mem_claim(self, lease: dict) -> int:
        if lease.get("io") and "granted" in lease:
            return max(lease["mem"], self._footprint(lease["pid"]))
        return lease["mem"]

    def _measured_mem(self, leases: list[dict]) -> dict[str, int]:
        """Attribute a PID's footprint only once, even across multiple leases."""
        remaining, footprints = {}, {}
        for x in leases:
            pid = x["pid"]
            budget = remaining.setdefault(pid, self._footprint(pid))
            footprints[x["id"]] = min(self._mem_claim(x), budget)
            remaining[pid] -= footprints[x["id"]]
        return footprints

    def _full_ids(self) -> set[str]:
        """Leases counted at their full size rather than measured use: those
        warming up, those exclusive on a lock (timing runs), and elastic ones
        (at their grant, which idle shrink keeps near their use), unless IO."""
        warming = {x["id"] for x in self._warming()}
        return {x["id"] for x in self.state["leases"]
                if not x.get("io") and (x["id"] in warming or x.get("exclusive_locks")
                                        or x.get("elastic"))}

    def _reserved_cpu(self, leases: list[dict]) -> float:
        full = self._full_ids()
        return sum(self._size(x) for x in leases if x["id"] in full)

    def _nesting(self) -> dict[str, str]:
        """For each measured lease whose process is inside another measured
        lease's tree (a lease started from within a lease, or two leases on
        one pid), the outermost such lease: its measurement includes the
        inner one's use."""
        measured = [x for x in self.state["leases"] if x["id"] in self._own_cores]
        outer_of = {}
        for i, x in enumerate(measured):
            for j, outer in enumerate(measured):
                if outer is x:
                    continue
                tree = self._trees.get(outer["id"], {outer["pid"]})
                if x["pid"] in tree and (x["pid"] != outer["pid"] or j < i):
                    outer_of[x["id"]] = outer["id"]
                    break
        for inner in list(outer_of):
            root, seen = outer_of[inner], {inner}
            while root in outer_of and root not in seen:
                seen.add(root)
                root = outer_of[root]
            outer_of[inner] = root
        return outer_of

    def _once_cap(self, x: dict) -> float:
        """How much of a counted-once lease's measured use is its own: its
        size, or the size it had over the smoothing window (a shrink is not
        yet out of the busy figure either)."""
        return max(self._size(x), self._size_avg.get(x["id"], 0.0))

    def _counted_once(self) -> set[str]:
        """Leases reserved at their full size all along, whose own measured
        use therefore comes out of the measured load: exclusive-lock leases
        (timing runs) and elastic leases (at their grant), unless IO. Warming
        leases keep main's conservative double count."""
        return {x["id"] for x in self.state["leases"]
                if not x.get("io") and (x.get("exclusive_locks") or x.get("elastic"))}

    def _net_busy(self) -> float:
        """Measured busy cores, less the own use of leases counted at full
        size (see _counted_once), so each counts once, at its size. Only use
        up to its size comes off: use above it (a job in its process tree but
        in a scope of its own, an uncapped hold, noise) stays counted as load.
        Lease use is smoothed like the busy figure, and counted once per
        process tree."""
        once = self._counted_once()
        outer_of = self._nesting()
        caps = {x["id"]: self._once_cap(x) for x in self.state["leases"] if x["id"] in once}
        inside: dict[str, float] = {}  # counted-once leases nested in each outermost tree
        for inner, root in outer_of.items():
            if inner in caps:
                inside[root] = inside.get(root, 0.0) + caps[inner]
        net = self.busy
        for x in self.state["leases"]:
            own = self._own_cores.get(x["id"])
            if own is not None and x["id"] in once and x["id"] not in outer_of:
                # Per outermost tree: its use, up to the sizes of the
                # counted-once leases in it.
                net -= min(own, caps[x["id"]] + inside.get(x["id"], 0.0))
        return max(0.0, net)

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
        # Leases exclusive on a lock (timing runs) count in full all along, so
        # their cores stay guaranteed; only the rest of the measured load
        # counts beside them (their own measured use is taken out of it).
        leases = self.state["leases"]
        cores = max(self._net_busy(), sum(self._cpu_claim(x) for x in leases if x.get("io")))
        cores += self._reserved_cpu(leases)
        vms = self._vms()
        mem = sum(self._mem_claim(x) for x in leases) + sum(g["mem"] for g in vms)
        return cores, mem

    def _claimed(self) -> float:
        """Cores claimed: full size for CPU leases, measured use otherwise."""
        leased = sum(self._cpu_claim(x) for x in self.state["leases"])
        return leased + sum(g["cores"] for g in self._vms()) + self._unleased()

    def _too_big(self, req: dict) -> str | None:
        cores, mem = self.capacity()
        if (max(req["cores"], self._cpu_claim(req) + req.get("headroom", 0)) > cores
                or req["mem"] > mem):
            return f"more than this computer has ({cores:g} cores, {human(mem)})"
        limit = cores - self._reserve()
        # A timing run or a MIN this big could never start beside another
        # project; a plain fixed request may (while its project is alone).
        never = self._timing(req) or (req.get("max_cores") and req.get("elastic", True))
        if (never and not req["exclusive"] and self._reserve() > 0
                and self._fixed_part(req) > limit + 1e-9):
            return (f"{self._fixed_part(req):g} cores that cannot be shrunk (a timing run's size "
                    f"plus headroom, or a MIN); one project may hold at most {limit:g} of "
                    f"{cores:g} while others are active (reserve_cores = {self._reserve():g}). "
                    "Ask --cores MIN:MAX, use --exclusive for a whole-machine run, or lower "
                    "reserve_cores in the config")
        return None

    def _why(self, req: dict) -> tuple[str, str] | None:
        """(kind, reason) req can't start now, ignoring the line, or None.
        kind is a stable category (exclusive, lock:NAME, disk, reserve,
        cores, mem, available, headroom, too-big) for the history."""
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
        if short := self._reserve_short(req):
            return "reserve", short
        cores, mem = self.capacity()
        used_cores, used_mem = self._usage()
        req_cores = self._cpu_claim(req)
        if used_cores + req_cores > cores + self._eps():
            return "cores", (f"{max(cores - used_cores, 0):.1f} of {cores:g} cores free"
                             + self._fixed_hint(req))
        if used_mem + req["mem"] > mem:
            return "mem", f"{human(max(mem - used_mem, 0))} of {human(mem)} free"
        if self.available is not None:
            # The sample does not yet include warming leases' full claims,
            # including jobs granted earlier in this admission pass.
            growth = sum(max(0, x["mem"] - self._footprint(x["pid"]))
                         for x in self._warming())
            available = self.available - RESERVE_MEM - growth
            if req["mem"] > available:
                return "available", (f"only {human(max(available, 0))} "
                                     "of memory actually available")  # fmt: skip
        keeper = max([req, *leases], key=lambda x: x.get("headroom", 0))
        if need := keeper.get("headroom", 0):
            claimed = self._claimed()
            if claimed + req_cores + need > cores + self._eps():
                whose = "this request" if keeper is req else (
                    f"{keeper['agent']}'s lease {keeper['id']} ({keeper['label']})")
                return "headroom", (f"{max(cores - claimed, 0):.1f} cores unclaimed; "
                                    f"{need:g} kept free for {whose}"
                                    + self._fixed_hint(req))  # fmt: skip
        return None

    # -- projects, fair shares and the reserve

    def _project(self, x: dict) -> str:
        """The project x counts under: the repository its client derived
        (repo), or a --project name only if it is that name or one listed
        under [projects.NAME] in the config; old clients' requests carry no
        repo and count under the name they give, else "default"."""
        name = x.get("project") or "default"
        repo = x.get("repo")
        if repo and name != repo and name not in self.config["projects"]:
            return repo
        return name

    def _weight(self, project: str) -> float:
        entry = self.config["projects"].get(project)
        return entry["weight"] if entry else self.config["default_weight"]

    def _projects(self) -> dict[str, dict]:
        """Active projects (a running or waiting lease): what each holds
        (CPU claims: grants, fixed sizes, measured use for IO), its
        non-preemptible cores (see _fixed_part) and its waiting requests."""
        projects: dict[str, dict] = {}
        for x in self.state["leases"]:
            entry = projects.setdefault(self._project(x), {"held": 0.0, "fixed": 0.0, "waiting": []})
            entry["held"] += self._cpu_claim(x)
            entry["fixed"] += self._fixed_part(x)
        for x in self.state["queue"]:
            projects.setdefault(self._project(x), {"held": 0.0, "fixed": 0.0, "waiting": []})[
                "waiting"].append(x)
        return projects

    def _shares(self, projects: dict[str, dict]) -> dict[str, float]:
        """Each active project's fair share of the cores: weight / sum of the
        active projects' weights. Idle projects claim nothing."""
        cores, _ = self.capacity()
        total = sum(self._weight(p) for p in projects) or 1.0
        return {p: cores * self._weight(p) / total for p in projects}

    def _reserve(self) -> float:
        """reserve_cores, but never more than half the machine."""
        return min(self.config["reserve_cores"], self.capacity()[0] / 2)

    def _timing(self, x: dict) -> bool:
        return bool(x.get("exclusive_locks") or x.get("headroom") or x.get("exclusive"))

    def _fixed_part(self, x: dict) -> float:
        """The cores of x that cannot be taken back: an elastic lease's MIN;
        a timing run's size plus its headroom; an IO lease's measured use;
        an ordinary fixed lease's size while it warms up, then its measured
        use (as admission counts it), at most its size. A MIN:MAX lease that
        cannot be resized counts as fixed."""
        if x.get("io"):
            return self._cpu_claim(x)
        if x.get("elastic") and x.get("min_cores") is not None:
            return x["min_cores"]
        if self._timing(x):
            return x["cores"] + x.get("headroom", 0)
        own = self._own_cores.get(x["id"]) if "granted" in x else None
        if own is not None and x["id"] not in {y["id"] for y in self._warming()}:
            return min(x["cores"], own)
        return x["cores"]

    def _others_active(self, mine: str) -> bool:
        return any(self._project(x) != mine for x in self.state["leases"] + self.state["queue"])

    def _reserve_short(self, req: dict) -> str | None:
        """The reserve rule: the non-preemptible cores of one project may
        never exceed the machine's cores less reserve_cores, so the other
        projects together always have that many to start in (from free
        cores, or by shrinking elastic leases). Whole-machine --exclusive is
        the one exception."""
        if req["exclusive"] or self._reserve() <= 0:
            return None  # reserve_cores = 0 turns the rule off
        mine = self._project(req)
        if not self._others_active(mine):
            return None  # alone on the machine: every core, as before
        cores, _ = self.capacity()
        limit = cores - self._reserve()
        held = sum(self._fixed_part(x) for x in self.state["leases"] if self._project(x) == mine)
        if held + self._fixed_part(req) <= limit + 1e-9:
            return None
        return (f"while other projects are active, project {mine}'s fixed and timing leases "
                f"and MINs may hold at most {limit:g} cores (reserve_cores = {self._reserve():g}); "
                f"this would make {held + self._fixed_part(req):.1f}: it waits for its own "
                "project's leases (other projects' requests may start meanwhile); "
                "--cores MIN:MAX above MIN is not limited")

    def _fixed_hint(self, req: dict) -> str:
        """For a CPU wait of a project below its fair share: what holds the
        cores (other projects' leases, fixed ones never shrunk, elastic ones
        with what they could still give), or that a fixed request is bigger
        than what elastic leases are shrunk to give it."""
        projects = self._projects()
        mine = self._project(req)
        shares = self._shares(projects)
        held = projects.get(mine, {"held": 0.0})["held"]
        if held >= shares.get(mine, 0.0) - 1e-9:
            return ""
        entitled = self._entitled(req, projects)
        if (not req.get("max_cores") and held + req["cores"] > entitled + 1e-9
                and any(x.get("elastic") and self._project(x) != mine
                        for x in self.state["leases"])):
            return (f"; other projects' elastic leases are shrunk only to give project "
                    f"{mine} its fair share ({entitled:g} of {self.capacity()[0]:g} cores); a "
                    f"fixed {req['cores']:g} waits for the rest, --cores MIN:MAX would "
                    "start now")
        holders = sorted((x for x in self.state["leases"]
                          if self._project(x) != mine and not x.get("io")),
                         key=lambda x: -x["cores"])[:4]
        if not holders:
            return ""

        def name(x):
            if x.get("elastic"):
                spare = x["cores"] - x["min_cores"]
                what = (f"elastic, {x['cores']:g} cores, {spare:g} above its MIN" if spare > 1e-9
                        else f"elastic, at its MIN {x['min_cores']:g}")
            else:
                what = (f"{'timing run' if self._timing(x) else 'fixed'} {x['cores']:g}, never "
                        "shrunk")
            return f"{x['id']} (project {self._project(x)}, {what})"

        return (f"; project {mine} is below its fair share; the cores are held by "
                + ", ".join(name(x) for x in holders))

    def _holding_back(self, mine: str, projects: dict, shares: dict) -> list[dict]:
        """Fixed (non-elastic, non-IO) leases of other projects over their
        fair share: what keeps project mine from its share."""
        return [x for x in self.state["leases"]
                if not x.get("elastic") and not x.get("io") and self._project(x) != mine
                and projects[self._project(x)]["held"] > shares[self._project(x)] + 1e-9]

    # -- elastic leases

    def _resize(self, x: dict, cores: float, kind: str, note: str) -> bool | None:
        """Set elastic lease x to cores, capping its scope (by the unit name
        the service generated) before a smaller size counts. Returns True
        when done, False when it failed (x keeps its size), None when the
        call runs on the worker (resize_async): a shrink then counts once it
        is done; a growth counts at once and is undone if it fails."""
        scope = x.get("scope")
        if not scope or not SCOPE_RE.fullmatch(scope):
            self._fix(x, "its scope name is not one the service made")
            return False
        if not self.resize_async:
            return self._resized(x, cores, kind, note, self.resize(scope, cores))
        self._pending[x["id"]] = (float(cores), kind, note)
        if cores > x["cores"]:  # count a growth at once (conservative)
            self._pending[x["id"]] = (x["cores"], "undo", "")
            self._apply(x, cores, kind, note)
        self._jobs.put((x["id"], scope, float(cores)))
        return None

    def _apply(self, x: dict, cores: float, kind: str, note: str) -> None:
        old = x["cores"]
        x["cores"] = float(cores)
        x["resized_at"] = time.time()
        x["resize_kind"] = kind
        x["resize_note"] = note
        self._idle.pop(x["id"], None)
        self._log("resized", x, kind=kind, reason=note, **{"from": old})

    def _resized(self, x: dict, cores: float, kind: str, note: str, ok: bool) -> bool:
        """The result of a resize call: count the new size, or note the
        failure: retried after RESIZE_BACKOFF (doubling) and fixed only
        after RESIZE_ATTEMPTS failures in a row."""
        if ok:
            x.pop("resize_failures", None)
            x.pop("resize_retry_at", None)
            if kind != "undo":
                self._apply(x, cores, kind, note)
            return True
        failures = x.get("resize_failures", 0) + 1
        x["resize_failures"] = failures
        if failures >= RESIZE_ATTEMPTS:
            self._fix(x, f"its scope could not be resized ({failures} attempts)")
        else:
            x["resize_retry_at"] = time.time() + RESIZE_BACKOFF * 2 ** (failures - 1)
            self._log("resized", x, kind="retry", reason=(
                f"resize to {cores:g} cores failed; retried after "
                f"{RESIZE_BACKOFF * 2 ** (failures - 1):g} s"), **{"from": x["cores"]})
        return False

    def _fix(self, x: dict, why: str) -> None:
        x.pop("elastic", None)
        x["resize_kind"] = "fixed"
        x["resize_note"] = f"{why}; fixed at {x['cores']:g} cores"
        self._idle.pop(x["id"], None)
        self._log("resized", x, kind="fixed", reason=x["resize_note"], **{"from": x["cores"]})
        self._post("agent-coord", x["agent"],
                   f"lease {x['id']} ({x['label']}) can no longer be resized ({why}); it keeps "
                   f"{x['cores']:g} cores until it ends.")

    def resize_worker(self) -> None:
        """Run queued resize calls outside the lock, one at a time, and
        apply each result under it."""
        while True:
            self.resize_job(self._jobs.get())

    def resize_job(self, job: tuple[str, str, float]) -> None:
        """One queued resize: the systemctl call without the lock, then its
        result applied (and the line re-admitted) with it."""
        lease_id, scope, cores = job
        try:
            ok = self.resize(scope, cores)
        except Exception:  # noqa: BLE001 - a failure like any other
            ok = False
        with self.lock:
            pending = self._pending.pop(lease_id, None)
            x = next((y for y in self.state["leases"] if y["id"] == lease_id), None)
            if x is not None and pending is not None and x.get("scope") == scope:
                target, kind, note = pending
                if kind == "undo" and not ok:  # a growth, counted already: take it back
                    self._apply(x, target, "retry", "growth could not be applied")
                self._resized(x, target, kind, note, ok)
            self._prune()
            self._admit()
            self._save()

    def _settled(self, x: dict, now: float) -> bool:
        return now - x.get("resized_at", x.get("granted", now)) >= self.config["resize_interval"]

    def _resizable(self, x: dict) -> bool:
        """Whether elastic lease x may be resized now: its client reported its
        scope exists (or SCOPE_READY_AFTER passed), no resize of it is under
        way, and no failed one is waiting for its retry."""
        now = time.time()
        return (bool(x.get("elastic")) and x["id"] not in self._pending
                and (x.get("scope_ready") or now - x.get("granted", now) >= SCOPE_READY_AFTER)
                and now >= x.get("resize_retry_at", 0))

    def _donor(self, x: dict) -> bool:
        return self._resizable(x)

    def _granted_room(self) -> float:
        """Cores not granted to anyone: capacity less every lease's claim
        (grants, fixed sizes, measured IO), load outside leases, and the
        largest headroom a lease keeps. Elastic sizes above MIN come only out
        of this, so they never make grants add up to more than the machine."""
        cores, _ = self.capacity()
        leases = self.state["leases"]
        granted = sum(self._cpu_claim(x) for x in leases)
        outside = self._unleased() + sum(g["cores"] for g in self._vms())
        need = max([0.0, *(x.get("headroom", 0) for x in leases)])
        return cores - granted - outside - need

    def _entitled(self, req: dict, projects: dict[str, dict]) -> float:
        """What req's project may reach by shrinking other projects' elastic
        leases: its fair share, and at least the reserve."""
        mine = self._project(req)
        projects.setdefault(mine, {"held": 0.0, "fixed": 0.0, "waiting": [req]})
        return max(self._shares(projects)[mine], self._reserve())

    def _target(self, req: dict) -> float:
        """The size a request aims for: a fixed size; a MIN:MAX request as
        much as its project's entitlement leaves (at least MIN, at most MAX)."""
        low, high = req["cores"], req.get("max_cores")
        if not high or high <= low:
            return low
        projects = self._projects()
        room = self._entitled(req, projects) - projects[self._project(req)]["held"]
        return float(max(low, min(high, math.floor(room + STEP_SLACK))))

    def _grant_size(self, req: dict) -> float:
        """The cores a MIN:MAX request starts with: as many up to its target
        (MAX within its project's entitlement) as are neither granted nor
        needed by requests waiting for CPU behind it; at least MIN."""
        low, high = req["cores"], req.get("max_cores")
        if not high or high <= low:
            return low
        waiting = 0.0
        for x in self.state["queue"]:
            why = None if x is req else self._why(x)
            if x is not req and (why is None or why[0] in CPU_WAIT_KINDS):
                waiting += self._cpu_claim(x)
        limit = min(self._target(req), self._granted_room() - waiting)
        for size in range(math.floor(limit + STEP_SLACK), math.floor(low), -1):
            if size > low and not self._why({**req, "cores": float(size)}):
                return float(size)
        return low

    def _preempt(self, req: dict, allowed=None) -> bool:
        """Shrink running elastic leases so req (allowed to start at its
        place in line, short only of CPU) can start, at its target size if
        possible and at least at its MIN or fixed size. Returns whether it
        fits now.

        - Other projects' elastic leases give only while req's project would
          stay within its entitlement (fair share, at least the reserve):
          asking for a big fixed size never gets a project more than asking
          MIN:MAX. The project furthest over its share gives first, then the
          lease with the largest surplus over its MIN, a core at a time.
        - Leases of req's own project give (toward MIN) only as far as req
          needs to reach its MIN or fixed size.
        - Never below MIN; fixed, IO and timing leases never shrink; a lease
          granted in the last few seconds is left alone. Nothing changes
          unless req then fits. Each shrink is applied to the donor's scope
          before it counts; a donor whose scope cannot be resized becomes
          fixed and the plan is made again without it."""
        if self._pending:
            # Shrinks already under way: if they will make room, wait for them.
            with self._pending_applied():
                if not self._why(req) and (allowed is None or allowed()):
                    return False
        for _ in range(4):
            with self._pending_applied():
                plan = self._plan(req, allowed)
            if plan is None:
                return False
            failed = False
            for x, new in plan:
                done = self._resize(x, new, "preempted", (
                    f"preempted by {req['id']} ({req['agent']}: {req['label']}, "
                    f"project {self._project(req)})"))
                if done is False:
                    failed = True
                    continue
                if x.get("shrink_told") != req["id"]:
                    x["shrink_told"] = req["id"]
                    self._post(
                        "agent-coord", x["agent"],
                        f"FYI: lease {x['id']} ({x['label']}) was shrunk to {new:g} cores so "
                        f"{req['id']} ({req['agent']}: {req['label']}) can start; it grows back "
                        "toward its MAX when cores free up. Nothing to do: the command reads the "
                        "current count from $COMPUTE_LEASE_FILE.",
                    )  # fmt: skip
            if not failed:
                return not self._why(req)
        return not self._why(req)

    @contextlib.contextmanager
    def _pending_applied(self):
        """For planning: leases with a shrink under way count at its size."""
        saved = {}
        for x in self.state["leases"]:
            pending = self._pending.get(x["id"])
            if pending and pending[1] != "undo" and pending[0] < x["cores"]:
                saved[x["id"]] = x["cores"]
                x["cores"] = pending[0]
        try:
            yield
        finally:
            for x in self.state["leases"]:
                if x["id"] in saved:
                    x["cores"] = saved[x["id"]]

    def _plan(self, req: dict, allowed=None) -> list[tuple[dict, float]] | None:
        """The shrinks _preempt applies (lease, new size), or None if they
        would not let req start even at its MIN or fixed size, or if allowed()
        (its place in line, judged with the shrinks in place) says no."""
        mine = self._project(req)
        projects = self._projects()
        shares = self._shares(projects)
        entitled = self._entitled(req, projects)
        held = {p: e["held"] for p, e in projects.items()}
        low = req["cores"]
        target = self._target(req)
        elastic = [x for x in self.state["leases"] if self._donor(x)]
        before = {x["id"]: x["cores"] for x in elastic}

        def fits(size):
            # Start at size, and (for a request behind the head) be allowed
            # to at its place in line, with the shrinks so far in place.
            return not self._why({**req, "cores": size}) and (allowed is None or allowed())

        def surplus(x):
            return x["cores"] - x["min_cores"]

        try:
            while not fits(target):
                others = [x for x in elastic if self._project(x) != mine and surplus(x) > 1e-9]
                own = [x for x in elastic if self._project(x) == mine and surplus(x) > 1e-9]
                if others and held[mine] + target <= entitled + 1e-9:
                    x = max(others, key=lambda x: (held[self._project(x)] - shares[self._project(x)],
                                                   surplus(x)))
                elif own and not fits(low):
                    x = max(own, key=surplus)
                elif target > low:
                    target = max(low, target - 1)
                    continue
                else:
                    break
                step = min(1.0, surplus(x))
                x["cores"] -= step
                held[self._project(x)] -= step
            if not fits(low):
                self._warned_unfair(req, projects, shares)
                return None
            return [(x, x["cores"]) for x in elastic if x["cores"] != before[x["id"]]]
        finally:
            for x in elastic:
                x["cores"] = before[x["id"]]

    def _warned_unfair(self, req: dict, projects: dict, shares: dict) -> bool:
        """Once per waiting request: record which fixed leases keep its
        project below its fair share; tell the holders of ordinary fixed
        leases (not timing runs, which must be fixed) to ask for MIN:MAX."""
        mine = self._project(req)
        if req.get("unfair_told") or mine not in projects or (
                projects[mine]["held"] >= shares[mine] - 1e-9):
            return False
        fixed = self._holding_back(mine, projects, shares)
        if not fixed:
            return False
        req["unfair_told"] = True
        reason = (f"project {mine} is below its fair share ({projects[mine]['held']:g} of "
                  f"{shares[mine]:.1f} cores) while fixed leases "
                  f"{', '.join(x['id'] for x in fixed)} hold more than theirs")
        self._log("unfair", req, reason=reason, holders=[x["id"] for x in fixed])
        plain = [x for x in fixed if not (x.get("exclusive_locks") or x.get("headroom")
                                          or x.get("exclusive"))]
        for agent in sorted({x["agent"] for x in plain}):
            self._post("agent-coord", agent,
                       f"{reason}. Fixed leases cannot be shrunk for other projects: unless it is "
                       "a timing run, ask for --cores MIN:MAX next time so the scheduler can "
                       "share them.")
        return True

    def _cpu_waiters(self) -> bool:
        """Whether a request waits for CPU (cores or headroom), or fits but
        waits its turn behind one."""
        for x in self.state["queue"]:
            why = self._why(x)
            if why is None or why[0] in CPU_WAIT_KINDS:
                return True
        return False

    def _even_out(self, elastic: list[dict], warming: set[str], now: float) -> bool:
        """Move cores toward fair shares between running elastic leases: a
        busy lease of a project under its share takes, a core at a time, from
        elastic leases of projects over theirs (furthest over first), never
        below their share or MIN. The donors' scopes are capped first; the
        taker grows only by what they gave, so grants never add up to more.
        Leases resized within resize_interval are left alone."""
        projects = self._projects()
        if len(projects) < 2:
            return False
        shares = self._shares(projects)
        held = {p: e["held"] for p, e in projects.items()}
        changed = False
        for x in sorted(elastic, key=lambda x: held[self._project(x)] / self._weight(self._project(x))):
            mine = self._project(x)
            own = self._raw_cores.get(x["id"])
            if (not self._resizable(x) or x["id"] in warming or own is None
                    or own < self.config["grow_demand"] * x["cores"]
                    or x["cores"] >= x["max_cores"] or not self._settled(x, now)):
                continue
            want = math.floor(min(x["max_cores"] - x["cores"], shares[mine] - held[mine]) + 1e-9)
            taken: dict[str, float] = {}
            while sum(taken.values()) < want:
                donors = [y for y in elastic if self._project(y) != mine and self._donor(y)
                          and y["cores"] - taken.get(y["id"], 0) - y["min_cores"] >= 1 - 1e-9
                          and held[self._project(y)] - shares[self._project(y)] >= 1 - 1e-9
                          and self._settled(y, now)]
                if not donors:
                    break
                y = max(donors, key=lambda y: (held[self._project(y)] - shares[self._project(y)],
                                               y["cores"] - taken.get(y["id"], 0) - y["min_cores"]))
                taken[y["id"]] = taken.get(y["id"], 0) + 1
                held[self._project(y)] -= 1
            gain = 0.0
            for y in elastic:
                if y["id"] in taken:
                    done = self._resize(y, y["cores"] - taken[y["id"]], "preempted", (
                        f"rebalanced: fair share for project {mine} (lease {x['id']})"))
                    if done:
                        gain += taken[y["id"]]
                    elif done is False:
                        held[self._project(y)] += taken[y["id"]]
                    changed = True
            # A shrink still under way (worker) frees its cores later; the
            # taker then gets them through growth, up to its share.
            if gain:
                if self._resize(x, x["cores"] + gain, "grew", f"grew: fair share for project {mine}"):
                    held[mine] += gain
                changed = True
        return changed

    def _rebalance(self) -> bool:
        """Offer idle elastic leases down, even out fair shares, and grow
        busy ones toward MAX when nothing waits for cores."""
        cfg = self.config
        now = time.time()
        elastic = [x for x in self.state["leases"] if x.get("elastic")]
        ids = {x["id"] for x in elastic}
        self._idle = {k: v for k, v in self._idle.items() if k in ids}
        if not elastic:
            return False
        changed = False
        warming = {x["id"] for x in self._warming()}
        for x in elastic:
            own = self._raw_cores.get(x["id"])
            if (x["id"] in warming or own is None or own >= cfg["idle_fraction"] * x["cores"]
                    or not self._resizable(x)):
                self._idle.pop(x["id"], None)
                continue
            since, peak = self._idle.get(x["id"], (now, own))
            peak = max(peak, own)
            self._idle[x["id"]] = (since, peak)
            target = max(x["min_cores"], float(math.ceil(peak / cfg["idle_target_use"])))
            if now - since >= cfg["idle_after"] and self._settled(x, now) and target < x["cores"]:
                self._resize(x, target, "idle", (
                    f"idle shrink: used at most {peak:.1f} of {x['cores']:g} cores for "
                    f"{(now - since) / 60:.0f} min"))  # fmt: skip
                changed = True
        changed |= self._even_out(elastic, warming, now)
        if self._cpu_waiters():
            return changed
        cores, _ = self.capacity()
        used, _ = self._usage()
        # Both what is measured free and what is not granted to anyone, so
        # grants never add up to more than the machine.
        spare = min(cores - used, self._granted_room())
        need = max([0.0, *(x.get("headroom", 0) for x in self.state["leases"])])
        if need:
            spare = min(spare, cores - self._claimed() - need)
        projects = self._projects()
        shares = self._shares(projects)
        for x in sorted(elastic, key=lambda x: x["cores"] - x["min_cores"]):
            own = self._raw_cores.get(x["id"])
            if (not self._resizable(x) or x["id"] in warming or own is None
                    or own < cfg["grow_demand"] * x["cores"]
                    or x["cores"] >= x["max_cores"] or not self._settled(x, now)):
                continue
            mine = self._project(x)
            # Up to its project's share, every free core; beyond it, keep
            # grow_margin free (so a newcomer starts without preempting).
            below = max(0.0, shares[mine] - projects[mine]["held"]) if len(projects) > 1 else 0.0
            room = max(min(spare, below), spare - cfg["grow_margin"])
            step = math.floor(min(x["max_cores"] - x["cores"], room) + STEP_SLACK)
            if step >= 1 and self._resize(x, x["cores"] + step, "grew", (
                    f"grew: using {own:.1f} of {x['cores']:g} cores and cores are free")) is not False:
                spare -= step
                projects[mine]["held"] += step
                changed = True
        return changed

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
        freed = sum(self._measured_mem(self.state["leases"]).values())
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

    def _refuse_too_big(self) -> bool:
        """Refuse queued requests that can never start (the machine, or the
        reserve for a timing run, changed under them: a config reload). Only
        from clients that understand a refusal; older clients' requests keep
        waiting and the line passes them."""
        changed = False
        for x in list(self.state["queue"]):
            if x.get("client", 0) >= CLIENT_VERSION and (reason := self._too_big(x)):
                self.state["queue"].remove(x)
                self.state.setdefault("refused", {})[x["id"]] = f"asks for {reason}"
                for old in list(self.state["refused"])[:-100]:
                    del self.state["refused"][old]
                self._log_end(x, False, why="refused: too big")
                changed = True
        return changed

    def _admit_pass(self) -> bool:
        """Start requests in order, backfilling around a blocked head without
        postponing its projected start (exclusive windows keep their rules)."""
        changed = self._refuse_too_big()
        while self.state["queue"]:
            head = self.state["queue"][0]
            if "head_since" not in head:
                head["head_since"] = time.time()
                changed = True
            why = self._why(head)
            if why and why[0] in CPU_WAIT_KINDS and self._preempt(head):
                why = self._why(head)
                changed = True
            if not why:
                changed |= self._top_up(head)
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
                        if holder.get("io") and self._cpu_claim(holder) < holder["cores"] / 2:
                            continue
                        if holder.get("elastic"):
                            continue  # told when it is shrunk instead
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
            self._grant(head, self._grant_size(head))
            changed = True
        return changed

    def _top_up(self, req: dict) -> bool:
        """Before a MIN:MAX request starts: if it would start below its target
        (its fair share, up to MAX), shrink other projects' elastic leases
        for it as a waiting request would. So asking MIN:MAX never gets a
        project less than asking a fixed size."""
        if not req.get("max_cores") or self._grant_size(req) >= self._target(req):
            return False
        before = {x["id"]: x["cores"] for x in self.state["leases"]}
        self._preempt(req)
        return any(x["cores"] != before.get(x["id"]) for x in self.state["leases"])

    def _grant(self, req: dict, cores: float | None = None) -> None:
        """Start req; a MIN:MAX request with cores (else at its MIN, as when
        it passes a waiting head, whose projection counted it at MIN)."""
        if cores is not None:
            req["cores"] = float(cores)
        req["granted"] = time.time()
        if not req.get("backfill_reason", "").startswith("backfilled:"):
            req.pop("backfill_reason", None)
            req.pop("backfill_head", None)
        req.pop("reason", None)
        req.pop("reason_kind", None)
        for key in ("head_since", "projected_start", "projected_ending", "unfair_told"):
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
        freed_cores = sum(self._cpu_claim(x) for x in ending)
        freed_mem = sum(self._mem_claim(x) for x in ending)
        need_cores = self._cpu_claim(req) + self._cpu_claim(head)
        if used_cores - freed_cores + need_cores > cores + self._eps():
            return False
        if used_mem - freed_mem + req["mem"] + head["mem"] > mem:
            return False
        if head.get("headroom") and (self._claimed() - freed_cores + need_cores
                                     + head["headroom"] > cores + self._eps()):  # fmt: skip
            return False
        if self.available is None:
            return True
        freed_available = sum(self._measured_mem(ending).values())
        return req["mem"] + head["mem"] <= self.available + freed_available - RESERVE_MEM

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
        if sum(self._mem_claim(x) for x in jobs) + sum(g["mem"] for g in self._vms()) > mem:
            return False
        io_used = sum(self._cpu_claim(x) for x in remaining if x.get("io"))
        outside = self._unleased() + sum(g["cores"] for g in self._vms())
        once = self._counted_once()  # in _reserved_cpu, out of _net_busy
        net = self._net_busy()
        if req is None:
            # Measured use finds the head's likely start, but subtracting a
            # full ending claim can undercount holders that remain.
            used = net - sum(self._cpu_claim(x) for x in ending if x["id"] not in once)
        else:
            used = outside + min(sum(self._cpu_claim(x) for x in remaining
                                     if x["id"] not in once),
                                 max(0, net - outside))
        used = max(outside + io_used, used) + self._reserved_cpu(remaining)
        if used + self._cpu_claim(head) + (self._cpu_claim(req) if req else 0) > cores + self._eps():
            return False
        headroom = max(x.get("headroom", 0) for x in jobs)
        if headroom and sum(self._cpu_claim(x) for x in jobs) + outside + headroom > cores + self._eps():
            return False
        if self.available is not None:
            footprints = self._measured_mem(self.state["leases"])
            freed = sum(footprints[x["id"]] for x in ending)
            growth = sum(self._mem_claim(x) - footprints[x["id"]] for x in remaining)
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
            if why and why[0] in CPU_WAIT_KINDS and self._preempt(
                    req, allowed=lambda req=req: self._may_pass(req)):
                why = None  # elastic leases were shrunk for it (not only for the head)
            if not why and self._may_pass(req):
                self._top_up(req)
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
          It takes its turn again as soon as it would fit.
        - Nor does a request that waits only for its own project (the reserve:
          its project's fixed cores while others are active) or that can never
          fit (too big, from an older client): holding others back gains it
          nothing, and others pass it by the ordinary rules."""
        queue = self.state["queue"]
        ahead = queue[: queue.index(req)] if req in queue else queue
        if not ahead:
            return True

        def hold(reason):
            req["backfill_reason"] = f"backfill held: {reason}"
            return False

        if req["exclusive"]:
            return hold("exclusive requests never overtake")

        def own_wait(x):
            why = self._why(x)
            return why is not None and why[0] in ("reserve", "too-big")

        ahead = [x for x in ahead if x["exclusive"] or not own_wait(x)]
        if not ahead:
            return True
        for x in ahead:
            if not self._stuck(x) and set(req.get("locks", ())) & set(x.get("locks", ())):
                return hold(f"lock order behind {x.get('id', 'queued request')}")
        now = time.time()
        req_cores = self._cpu_claim(req)
        small = req_cores <= SMALL_CORES and req["mem"] <= SMALL_MEM and not req.get("disk")
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
            return (req_cores <= BACKFILL_CORES and req["mem"] <= BACKFILL_MEM
                    and not req.get("disk")) or hold("ordering behind a stuck head")
        if req_cores <= BACKFILL_CORES and req["mem"] <= BACKFILL_MEM and not req.get("disk"):
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

    def _sample_lease_cpu(self, now: float, samples: dict[str, float]) -> None:
        previous = self._cpu_sample
        self._cpu_sample = now, samples
        self._measured_cores = {}
        self._raw_cores = {}
        if previous is None or now <= previous[0]:
            self._own_cores = {}
            return
        own, sizes = {}, {}
        for x in self.state["leases"]:
            lease_id = x["id"]
            if lease_id in samples and lease_id in previous[1]:
                delta = samples[lease_id] - previous[1][lease_id]
                raw = max(0, delta) / (now - previous[0])
                self._raw_cores[lease_id] = raw
                self._measured_cores[lease_id] = min(self._size(x), raw)
                # Smoothed with the same factor as self.busy (see tick).
                # Starts from 0, as the host-wide figure effectively does for
                # a new lease's load: never subtract more than busy holds.
                before = self._own_cores.get(lease_id, 0.0)
                own[lease_id] = before + BUSY_SMOOTHING * (raw - before)
                # Its size, smoothed the same way: after a shrink, the busy
                # figure still holds its earlier use for a while.
                size = self._size(x)
                was = self._size_avg.get(lease_id, size)
                sizes[lease_id] = was + BUSY_SMOOTHING * (size - was)
        self._own_cores = own
        self._size_avg = sizes

    def _hint_io(self, now: float) -> bool:
        low_since = {}
        changed = False
        for x in self.state["leases"]:
            if (x.get("io") or x.get("io_hint") or x.get("elastic")
                    or time.time() - x["granted"] < WARMUP):
                continue
            lease_id = x["id"]
            cpu = self._measured_cores.get(lease_id)
            mem = self._footprint(x["pid"]) if x["mem"] else 0
            low = []
            if x["cores"] >= 2 and cpu is not None and cpu < 0.15 * x["cores"]:
                low.append(("cpu", f"{cpu:.2f} of {x['cores']:g} cores"))
            if mem < 0.15 * x["mem"]:
                low.append(("mem", f"{human(mem)} of {human(x['mem'])} RAM"))
            due = []
            for resource, detail in low:
                key = lease_id, resource
                low_since[key] = self._low_since.get(key, now)
                if now - low_since[key] >= IO_HINT_AFTER:
                    due.append(detail)
            if due:
                x["io_hint"] = True
                self._post(
                    "agent-coord", x["agent"],
                    f"{lease_id} ({x['label']}) is using {', '.join(due)} held. "
                    "If this is a download or other IO-bound job, use --io "
                    "(or --cores 1); if it needs less RAM, use a smaller --mem.",
                )
                changed = True
        self._low_since = low_since
        return changed

    def tick(self) -> None:
        sample = self.measure()
        with self.lock:
            targets = [(x["id"], x["pid"]) for x in self.state["leases"]
                       if x.get("io") or x.get("exclusive_locks") or x.get("min_cores")
                       or (x["cores"] >= 2 and not x.get("io_hint"))]
            walk = any(x.get("exclusive_locks") or x.get("elastic") for x in self.state["leases"])
        now = time.monotonic()
        by_pid = {pid: self.cpu_seconds(pid) for pid in {pid for _, pid in targets}}
        # Process trees matter only for leases counted once: walk them only
        # when one exists (and more than one lease is measured).
        trees = {pid: (self.tree_pids or tree_pids)(pid) for pid in by_pid
                 if by_pid[pid] is not None} if len(targets) > 1 and walk else {}  # fmt: skip
        samples = {lease_id: by_pid[pid] for lease_id, pid in targets if by_pid[pid] is not None}
        with self.lock:
            self._trees = {lease_id: trees[pid] for lease_id, pid in targets if pid in trees}
            self._sample_lease_cpu(now, samples)
            self.vms = sample["vms"]
            self.available = sample["available"]
            # Smoothed over ~10 s: a moment's burst shouldn't hold the line.
            self.busy += BUSY_SMOOTHING * (sample["busy"] - self.busy)
            self._reload_config()
            changed = self._prune()
            changed |= self._hint_io(now)
            changed |= self._admit()
            changed |= self._rebalance()
            if changed:
                self._save()

    def _reload_config(self) -> None:
        if self.config_file is None:
            return
        try:
            mtime = self.config_file.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._config_mtime:
            self._config_mtime = mtime
            try:
                self.config = load_config(self.config_file)
            except Exception as e:  # noqa: BLE001 - keep the previous settings
                print(f"agent-coord: config reload failed: {e!r}", file=sys.stderr, flush=True)

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
        if req.get("io"):
            req = {"cores": 1, "mem": IO_DEFAULT_MEM, **req}
        for key in ("cores", "mem", "pid", "label"):
            if key not in req:
                return {"error": f"missing {key}"}
        if req["cores"] <= 0 or req["mem"] < 0:
            return {"error": "cores must be positive and mem not negative"}
        high = req.get("max_cores")
        if high is not None:
            if type(high) not in (int, float) or not math.isfinite(high) or high < req["cores"]:
                return {"error": "max cores must be a number no smaller than min cores"}
            if (req.get("exclusive") or req.get("exclusive_locks") or req.get("headroom")
                    or req.get("io") or req.get("running")):
                return {"error": "MIN:MAX cores are for ordinary run leases; timing runs "
                                 "(--exclusive, --exclusive-lock, --headroom), --io and hold "
                                 "take a fixed --cores"}  # fmt: skip
            high = min(float(high), self.capacity()[0])
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
        project = str(req.get("project") or "") or None
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
        if req.get("io"):
            lease["io"] = True
        if headroom:
            lease["headroom"] = headroom
        if project:
            lease["project"] = project
        if req.get("repo"):
            lease["repo"] = str(req["repo"])
        if type(req.get("client")) is int:
            lease["client"] = req["client"]
        if req.get("resizable") and not req.get("running"):
            # The client runs the command in a scope of this name; the service
            # resizes only scopes it named, by that exact name.
            lease["scope"] = f"{SCOPE_PREFIX}-{lease['id']}-{lease['pid']}.scope"
        if high is not None and high > lease["cores"]:
            # Queued at MIN; granted up to MAX. Resized while running only if
            # it runs in a scope (else fixed at the size it starts with).
            lease["min_cores"], lease["max_cores"] = lease["cores"], high
            if lease.get("scope"):
                lease["elastic"] = True
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
        if lease_id in self.state.get("refused", {}):
            return {"error": self.state["refused"][lease_id]}
        for x in self.state["leases"]:
            if x["id"] == lease_id:
                return {"state": "granted", "cores": x["cores"],
                        "min_cores": x.get("min_cores", x["cores"]),
                        "max_cores": x.get("max_cores", x["cores"]),
                        "elastic": bool(x.get("elastic")), "scope": x.get("scope"),
                        "reason": x.get("resize_note", "granted")}  # fmt: skip
        for position, x in enumerate(self.state["queue"]):
            if x["id"] == lease_id:
                reason = self._blocked(x) or x.get("backfill_reason") or x.get("reason") or "waiting"
                if position:
                    reason = f"{position} request(s) ahead in line; {reason}"
                return {"state": "waiting", "reason": reason}
        return {"state": "gone"}

    def op_poll(self, req: dict, _agent: str) -> dict:
        """A client's poll; a running MIN:MAX client also reports, once, that
        its scope exists (only the lease's own client, by its pid)."""
        lease_id = str(req.get("id"))
        if req.get("scope_ready"):
            for x in self.state["leases"]:
                if x["id"] == lease_id and req.get("pid") == x["pid"]:
                    x["scope_ready"] = True
        return self._ticket(lease_id)

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
        io = bool(req.get("io"))
        probe = {"cores": float(req.get("cores", 1)), "io": io,
                 "mem": int(req.get("mem", IO_DEFAULT_MEM if io else 0)),
                 "exclusive": bool(req.get("exclusive")),
                 "locks": list(dict.fromkeys([*req.get("locks", []), *scoped])),
                 "exclusive_locks": scoped, "headroom": float(req.get("headroom") or 0),
                 "disk": req.get("disk", {}), "expect": expect}  # fmt: skip
        if req.get("project"):
            probe["project"] = str(req["project"])
        if req.get("repo"):
            probe["repo"] = str(req["repo"])
        if req.get("max_cores"):
            probe["max_cores"] = min(float(req["max_cores"]), self.capacity()[0])
            probe["min_cores"] = probe["cores"]
        reason = self._blocked(probe)
        if not reason and self.state["queue"] and not self._may_pass(probe):
            reason = probe.get("backfill_reason") or f"{len(self.state['queue'])} request(s) already waiting"
        reply = {"ok": reason is None, "reason": reason}
        if reason is None:
            reply["cores"] = self._grant_size(probe)
        return reply

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
        projects = self._projects()
        shares = self._shares(projects)
        project_rows = []
        holding_back: dict[str, list[str]] = {}
        for name, entry in sorted(projects.items()):
            row = {"name": name, "weight": self._weight(name), "share": round(shares[name], 2),
                   "held": round(entry["held"], 2), "fixed": round(entry["fixed"], 2),
                   "waiting": len(entry["waiting"]),
                   "waiting_cores": sum(x["cores"] for x in entry["waiting"])}
            cpu_wait = [x for x in entry["waiting"]
                        if (why := self._why(x)) is not None and why[0] in CPU_WAIT_KINDS]
            if cpu_wait and entry["held"] < shares[name] - 1e-9:
                fixed = self._holding_back(name, projects, shares)
                if fixed:
                    row["warning"] = (f"below its fair share while fixed leases "
                                      f"{', '.join(x['id'] for x in fixed)} hold more than "
                                      "their share (fixed leases are never shrunk)")
                    for x in fixed:
                        holding_back.setdefault(x["id"], []).append(name)
            project_rows.append(row)

        limit = cores - self._reserve()

        def lease_row(x):
            row = {**x, "project": self._project(x)}
            mine = self._project(x)
            if (self._reserve() > 0 and self._others_active(mine)
                    and projects[mine]["fixed"] > limit + 1e-9 and not x.get("elastic")
                    and not x.get("io")):
                row["over_cap"] = limit
            if x["id"] in holding_back:
                row["holding_back"] = holding_back[x["id"]]
            if x.get("io"):
                row.update(measured_cores=round(self._cpu_claim(x), 2),
                           claimed_mem=self._mem_claim(x))
            elif x["id"] in self._raw_cores:
                row["measured_cores"] = round(self._raw_cores[x["id"]], 2)
            if x.get("max_cores") and x["cores"] < x["max_cores"] and x.get(
                    "resize_kind") in ("preempted", "idle"):
                row["shrunk"] = True
            return row

        return {
            "capacity": {"cores": cores, "mem": mem, "reserve": RESERVE_MEM},
            "free": {"cores": cores - used_cores, "mem": mem - used_mem},
            "leases": [lease_row(x) for x in self.state["leases"]],
            "projects": project_rows,
            "queue": [{**x, "project": self._project(x),
                       "reason": self._blocked(x) or x.get("backfill_reason") or x.get("reason") or "waiting"}
                      for x in self.state["queue"]],
            "vms": self._vms(), "load": os.getloadavg(),
            "locks": self.state["locks"],
            "disks": {m: {"free": self.disk_free(m), "reserved": self._reserved(m)}
                      for m in sorted({m for x in self.state["leases"]
                                       for m in x.get("disk", {})})},
            "unleased_cores": round(self._unleased(), 1),
            "busy_cores": round(self.busy, 1),
            "available": self.available,
            "config_errors": self.config.get("errors", []),
            "reserve": self._reserve(),
        }


def serve(
    socket_path: Path | None = None, state_file: Path | None = None, **coordinator_args
) -> None:
    socket_path, state_file = socket_path or SOCKET, state_file or STATE_FILE
    coordinator_args.setdefault("config_file", CONFIG_FILE)
    coordinator_args.setdefault("resize", resize_scope)
    coordinator_args.setdefault("resize_async", True)
    coordinator = Coordinator(state_file, **coordinator_args)
    if coordinator.resize_async:
        threading.Thread(target=coordinator.resize_worker, daemon=True).start()
    sweep_lease_files(socket_path.parent / "leases")
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path.unlink(missing_ok=True)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline()
            try:
                reply = coordinator.handle(json.loads(line))
            except (ValueError, TypeError, KeyError) as e:
                reply = {"error": f"bad request: {e}"}
            except Exception as e:  # noqa: BLE001 - one bad request must not end the service
                print(f"agent-coord: request failed: {e!r}", file=sys.stderr, flush=True)
                reply = {"error": f"internal error: {e!r}"}
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


def _wait_for(lease_id: str, reply: dict, max_wait: float | None) -> dict:
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
    return reply


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


def has_scopes() -> bool:
    """Whether commands can run in systemd user scopes (and be resized)."""
    bus = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "bus")
    return bool(shutil.which("systemd-run")) and bus.exists()


def scope_name(lease_id: str) -> str:
    """This client's scope for lease_id: unique per client process, and the
    prefix (AGENT_COORD_SCOPE_PREFIX) keeps private instances' scopes apart."""
    prefix = os.environ.get("AGENT_COORD_SCOPE_PREFIX") or "agent-coord"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix) or not re.fullmatch(r"L\d+", lease_id):
        raise SystemExit(f"agent-coord: bad scope name {prefix}-{lease_id}")
    return f"{prefix}-{lease_id}-{os.getpid()}.scope"


def cpu_quota(cores: float) -> str:
    return f"CPUQuota={max(int(cores * 100), 1)}%"


def limited(cmd: list[str], cores: float, mem: int, scope: str | None = None) -> list[str]:
    """cmd in a systemd user scope capped at cores and mem, at low priority."""
    low = ["nice", "-n", "19", "ionice", "-c", "3", *cmd]
    if not has_scopes():
        print("agent-coord: no systemd user session; running uncapped at low "
              "priority", file=sys.stderr)  # fmt: skip
        return low
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        *(["--unit", scope] if scope else []),
        "-p", cpu_quota(cores), "-p", f"MemoryMax={mem}",
        "-p", "MemorySwapMax=0", "--", *low,
    ]  # fmt: skip


def resize_command(scope: str, cores: float) -> list[str]:
    """The command that changes a running scope's CPU cap (runtime only)."""
    return ["systemctl", "--user", "set-property", "--runtime", scope, cpu_quota(cores)]


def resize_scope(scope: str, cores: float) -> bool:
    try:
        return subprocess.run(resize_command(scope, cores), capture_output=True,
                              timeout=RESIZE_TIMEOUT, check=False).returncode == 0  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False


def lease_text(info: dict) -> str:
    """COMPUTE_LEASE_FILE: line 1 the whole cores to use now (at least 1),
    line 2 the same as JSON with the lease id, min, max, exact grant, reason
    and time."""
    return f"{max(int(info['granted']), 1)}\n{json.dumps(info, sort_keys=True)}\n"


def write_lease_file(path: Path, info: dict) -> None:
    """Replace path atomically: readers see the old or the new file, whole."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(lease_text(info))
    tmp.replace(path)


def scope_exists(pid: int, scope: str) -> bool:
    """Whether process pid already runs in systemd unit scope (its cgroup)."""
    try:
        return f"/{scope}" in Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return False


def report_scope_ready(lease_id: str, child: subprocess.Popen, scope: str,
                       stop: threading.Event, poll=None) -> None:
    """Tell the service, once, that the command's scope exists (so it may
    resize it); waits up to SCOPE_READY_AFTER for systemd-run to set it up."""
    poll = poll or (lambda request: call(request, retry=120))
    deadline = time.monotonic() + SCOPE_READY_AFTER
    while not stop.is_set() and time.monotonic() < deadline:
        if scope_exists(child.pid, scope):
            try:
                poll({"op": "poll", "id": lease_id, "pid": os.getpid(), "scope_ready": True})
            except (SystemExit, Exception):  # noqa: BLE001 - the service falls back on a timeout
                pass
            return
        stop.wait(0.2)


def follow_grant(lease_id: str, child: subprocess.Popen, path: Path, info: dict,
                 notify: bool, stop: threading.Event, poll=None,
                 interval: float | None = None) -> None:
    """While child runs, learn grant changes on the normal poll (the service
    has already capped the scope), rewrite the lease file, then (if notify)
    send SIGUSR1 to child, the process this client started, and nothing else.
    Informational only: no failure here affects the lease or its accounting,
    and errors (a closed stderr pipe, a full disk) never end the loop."""
    poll = poll or (lambda request: call(request, retry=120))
    interval = POLL if interval is None else interval
    while not stop.wait(interval):
        try:
            reply = poll({"op": "poll", "id": lease_id})
            cores = reply.get("cores")
            if reply.get("state") != "granted" or not cores or cores == info["granted"]:
                continue
            info.update(granted=cores, reason=reply.get("reason", ""), time=round(time.time(), 1))
            if stop.is_set():
                return  # the command has ended: its lease file is being removed
            write_lease_file(path, info)
            if notify and child.pid > 1 and child.poll() is None:
                child.send_signal(signal.SIGUSR1)
            print(f"agent-coord: {lease_id} now {cores:g} cores ({info['reason']})",
                  file=sys.stderr, flush=True)  # fmt: skip
        except (SystemExit, Exception):  # noqa: BLE001 - keep following; nothing depends on it
            continue


def sweep_lease_files(directory: Path) -> int:
    """Remove lease files whose client (the pid in the name) has exited."""
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        match = re.fullmatch(r"\.?(L\d+)-(\d+)(?:\.tmp)?", entry.name)
        if match and not alive(int(match[2])):
            entry.unlink(missing_ok=True)
            removed += 1
    return removed


def parse_disk(text: str) -> tuple[str, int]:
    path, sep, size = text.rpartition(":")
    if not sep or not path:
        raise argparse.ArgumentTypeError(f"not PATH:SIZE: {text} (e.g. /home:150G)")
    return path, parse_size(size)


def _lease_request(args: argparse.Namespace, pid: int) -> dict:
    return {"op": "acquire", "agent": args.agent, "cwd": os.getcwd(), "pid": pid,
            "cores": args.cores, "max_cores": args.max_cores, "project": args.project,
            "repo": args.repo, "client": CLIENT_VERSION,
            "resizable": has_scopes(), "mem": args.mem, "label": args.label, "io": args.io,
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
    stop = threading.Event()
    follower = None
    path = RUNTIME / "leases" / f"{lease_id}-{os.getpid()}"

    def forward(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)
        else:
            raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    try:
        reply = _wait_for(lease_id, reply, args.max_wait)
        granted = float(reply.get("cores") or args.cores)
        low, high = args.cores, args.max_cores or args.cores
        print(f"agent-coord: {lease_id} started ({granted:g} cores"
              + (f" of {low:g}:{high:g}" if args.max_cores else "")
              + f", {human(args.mem)})", file=sys.stderr, flush=True)  # fmt: skip
        info = {"lease": lease_id, "min": low, "max": high, "granted": granted,
                "reason": "granted", "time": round(time.time(), 1)}
        write_lease_file(path, info)
        env = dict(os.environ, COMPUTE_LEASE_CORES=str(max(int(granted), 1)),
                   COMPUTE_LEASE_MIN=f"{low:g}", COMPUTE_LEASE_MAX=f"{high:g}",
                   COMPUTE_LEASE_FILE=str(path))  # fmt: skip
        # JVMs size their heap from the host's RAM, not the scope's limit, and
        # get killed at it: tell them the lease's memory (heap up to 60% of it).
        java = f"-XX:MaxRAM={args.mem} -XX:MaxRAMPercentage=60"
        env["JAVA_TOOL_OPTIONS"] = f"{env.get('JAVA_TOOL_OPTIONS', '')} {java}".strip()
        elastic = bool(reply.get("elastic"))
        scope = None
        if has_scopes():
            # The service names the scope (and resizes it by that name).
            scope = reply.get("scope") or scope_name(lease_id)
            if not SCOPE_RE.fullmatch(scope):
                raise SystemExit(f"agent-coord: bad scope name {scope!r}")
        if args.max_cores and not elastic:
            print(f"agent-coord: {lease_id} cannot be resized (no systemd scope); it keeps "
                  f"{granted:g} cores", file=sys.stderr, flush=True)  # fmt: skip
        child = subprocess.Popen(limited(command, granted, args.mem, scope), env=env)
        if elastic:
            if scope:
                threading.Thread(target=report_scope_ready, daemon=True,
                                 args=(lease_id, child, scope, stop)).start()
            follower = threading.Thread(target=follow_grant, daemon=True, args=(
                lease_id, child, path, info, args.notify_signal, stop))
            follower.start()
        code = child.wait()
        stop.set()
        if code == -signal.SIGKILL:
            print(f"agent-coord: {lease_id} was killed; if it used more than "
                  f"--mem {human(args.mem)}, lease more", file=sys.stderr)  # fmt: skip
        end["code"] = 128 - code if code < 0 else code
        return end["code"]
    except KeyboardInterrupt:
        end.update(code=130, why="interrupted")
        return 130
    finally:
        stop.set()
        if follower is not None:
            follower.join(20)  # at most one resize (15 s timeout) in flight
        path.unlink(missing_ok=True)
        path.with_name(f".{path.name}.tmp").unlink(missing_ok=True)
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
    reply = call({"op": "check", "cores": args.cores, "max_cores": args.max_cores,
                  "project": args.project, "repo": args.repo, "mem": args.mem, "io": args.io,
                  "exclusive": args.exclusive, "locks": args.lock or [],
                  "exclusive_locks": args.exclusive_lock or [], "headroom": args.headroom,
                  "agent": args.agent or default_agent(), "label": args.label,
                  "expect": args.expect, "disk": {mount_of(p): n for p, n in args.disk or []}})  # fmt: skip
    if reply["ok"]:
        print(f"ok ({reply['cores']:g} cores)" if args.max_cores and reply.get("cores") else "ok")
    else:
        print(f"no: {reply['reason']}")
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
        cpu = f"{x['cores']:>4g} cores"
        if x.get("max_cores"):
            cpu = f"{x['cores']:>4g} of {x['min_cores']:g}:{x['max_cores']:g} cores"
            if state == "held" and not x.get("elastic"):
                flag += (" fixed: scope not resizable" if x.get("resize_kind") == "fixed"
                         else " fixed at its start size (no systemd scope)")
            if x.get("shrunk"):
                flag += f" SHRUNK ({x.get('resize_note', '')})"
            elif x.get("resize_note") and state == "held":
                flag += f" ({x['resize_note']})"
        if x.get("over_cap") is not None:
            flag += (f" (its project's fixed cores exceed {x['over_cap']:g} while others are "
                     "active: no new fixed lease of it starts until they are back under)")
        if x.get("holding_back"):
            flag += f" HOLDS BACK project {', '.join(x['holding_back'])} (fixed: never shrunk)"
        mem = x.get("claimed_mem", x["mem"])
        if x.get("io"):
            flag += " io"
            cpu = f"{x['cores']:g}-core cap"
            if state == "held":
                cpu += f", {x['measured_cores']:g} used"
        if x.get("project"):
            flag += f" [project {x['project']}]"
        print(f"  {x['id']:<6} {state:<7} {cpu} {human(mem):>6} "
              f"{since:5.0f} min  {x['agent']}: {x['label']}{flag} (pid {x['pid']}){why}")
    if len(s.get("projects", [])) > 1 or any(p.get("warning") for p in s.get("projects", [])):
        print("projects (active; fair share = cores x weight / active weights; one project's "
              f"fixed cores stay {s.get('reserve', 0):g} below all):")
        for p in s["projects"]:
            print(f"  {p['name']:<24} weight {p['weight']:g}  share {p['share']:.1f}  "
                  f"holds {p['held']:g} ({p['fixed']:g} fixed)  waiting {p['waiting']}"
                  + (f" ({p['waiting_cores']:g} cores)" if p["waiting"] else "")
                  + (f"  WARNING: {p['warning']}" if p.get("warning") else ""))
    for error in s.get("config_errors", []):
        print(f"config: {error}")
    if "busy_cores" in s:
        print(f"cores busy now (measured): {s['busy_cores']:g} of {cap['cores']:g}; "
              "CPU admission uses this; memory admission reserves at least lease sizes")
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
        if e.get("io"):
            flag += " io"
        times = []
        for key in ("wait", "waited", "run"):
            if key in e:
                times.append(f"{'wait' if key == 'waited' else key} {_minutes(e[key])}")
        if "code" in e:
            times.append(f"exit {e['code']}")
        if e.get("max_cores"):
            flag += f" {e['min_cores']:g}:{e['max_cores']:g}"
        if e.get("project"):
            flag += f" [project {e['project']}]"
        if e["event"] == "resized":
            times.append(f"{e.get('from', 0):g}->{e['cores']:g} cores")
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
        sub.add_argument("--io", action="store_true", help="IO-bound job (defaults: 1 core, 512M)")
        sub.add_argument("--cores", type=parse_cores)
        sub.add_argument("--project")
        sub.add_argument("--mem", type=parse_size)
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
            sub.add_argument("--notify-signal", action="store_true")
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
    if args.action in ("run", "hold", "check"):
        if not args.io and (args.cores is None or args.mem is None):
            parser.error("--cores and --mem are required without --io")
        args.cores, args.max_cores = args.cores or (1.0, None)
        if args.mem is None:
            args.mem = IO_DEFAULT_MEM
        if args.max_cores and (args.action == "hold" or args.io or args.exclusive
                               or args.exclusive_lock or args.headroom):
            parser.error("--cores MIN:MAX is for ordinary run leases; timing runs (--exclusive, "
                         "--exclusive-lock, --headroom), --io and hold take a fixed --cores")
    args.agent = args.agent or default_agent()
    if args.action in ("run", "hold", "check"):
        cwd = None
        if args.action == "hold":
            try:  # the held process's working directory, where readable
                cwd = os.readlink(f"/proc/{args.pid}/cwd")
            except OSError:
                pass
        # The repository is what fair shares go by; --project names count as
        # their own project only when listed in the service's config.
        args.repo = default_project(args.agent, cwd)
        args.project = args.project or args.repo
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
