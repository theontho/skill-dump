#!/usr/bin/env python3
"""agent-coord: a local coordinator for the agents (and people) on this computer.

A user service keeps the state; agents talk to it with this same file as a
CLI. It hands out CPU/RAM leases first come, first served (running QEMU guests
count at their measured use), so heavy jobs queue instead of piling up, and it
carries messages between agents.

On Linux the service is agent-coord.service (systemd user unit) on a Unix
socket. On Windows it is a background process on a loopback TCP port, found
through a token-protected endpoint file, started on demand by the CLI (and at
logon after "agent-coord service install").
"""

import argparse
import hmac
import json
import math
import os
import plistlib
import re
import secrets
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

WINDOWS = sys.platform == "win32"
DARWIN = sys.platform == "darwin"
if DARWIN:
    try:
        import psutil
    except ImportError:
        raise SystemExit("agent-coord on macOS requires psutil in the Python environment running it.") from None

# Tests and throwaway trials only: AGENT_COORD_RUNTIME_DIR and
# AGENT_COORD_STATE_DIR point an instance at its own socket, state and history.
# Set for the shared service, it would start from an empty state.
if WINDOWS:
    _state_home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    _runtime = _state_home / "agent-coord"
else:
    _state_home = Path.home() / ".local/state"
    _runtime = (Path.home() / ".agent-coord" if DARWIN else
                Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agent-coord")
RUNTIME = Path(os.environ.get("AGENT_COORD_RUNTIME_DIR") or _runtime)
SOCKET = RUNTIME / ("endpoint.json" if WINDOWS else "sock")
STATE_FILE = Path(os.environ.get("AGENT_COORD_STATE_DIR") or
                  Path(os.environ.get("XDG_STATE_HOME") or _state_home) / "agent-coord") / "state.json"
# One JSON line per lease event, next to the state; rotated by size.
HISTORY_FILE = STATE_FILE.with_name("history.jsonl")
HISTORY_MAX_BYTES = 8 * 1024**2
HISTORY_KEEP = 4  # rotated files kept: history.jsonl.1 .. .4
# Left for the desktop, the agents themselves and unleased short commands.
RESERVE_MEM = 4 * 1024**3
TICK = 2.0
BUSY_SMOOTHING = 0.2  # per tick, for host-wide and per-lease CPU use
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
RESTART_GRACE = 120
TRACK_INTERVAL = 5
EXCLUSIVE_IDLE_AFTER = 15 * 60
EXCLUSIVE_SAMPLE = 30
UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}\Z")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Measured memory admission. A request that does not fit by reservation (every
# lease at its full size) may still start when the memory actually available,
# less the kept-back reserve, a safety margin, and a growth allowance for every
# running lease, covers it; and when all leases' sizes together stay within
# MEM_OVERCOMMIT times the lease capacity. Each lease keeps its hard cap
# (MemoryMax). The service reads these once at start (set them in the unit's
# Environment=); AGENT_COORD_MEASURED_MEM=0 restores reservation-only admission.
MEASURED_MEM = os.environ.get("AGENT_COORD_MEASURED_MEM", "1") != "0"
# Allowance for growth beyond a settled lease's peak, as a fraction of what it
# could still grow to its size. Leases that are young or whose peak rose in the
# last MEM_RAMP seconds count their whole remaining size.
MEM_GROWTH = min(1.0, max(0.0, _env_float("AGENT_COORD_MEM_GROWTH", 0.5)))
MEM_GROWTH_FLOOR = 1024**3  # never less (unless less is left to its size)
MEM_RAMP = 10 * 60
# A peak rise of this fraction of the lease's size (at least MEM_RAMP_MIN)
# counts as still ramping up.
MEM_RAMP_STEP, MEM_RAMP_MIN = 0.05, 256 * 1024**2
# Kept free beyond the reserve, as a fraction of RAM, with every running lease
# grown by its allowance and the new one at its full size.
MEM_MARGIN = min(0.5, max(0.0, _env_float("AGENT_COORD_MEM_MARGIN", 0.10)))
# All leases' sizes (their hard caps) together, plus unleased VMs, may reach
# this multiple of the lease capacity under measured admission.
MEM_OVERCOMMIT = max(1.0, _env_float("AGENT_COORD_MEM_OVERCOMMIT", 1.5))
# `history --peaks` suggests --mem as the largest recent peak plus this.
PEAK_SIZING_MARGIN = 0.15
# A label with at least MEM_HISTORY_RUNS successful measured runs is expected
# to grow to at most its largest recent peak plus PEAK_SIZING_MARGIN (while
# it stays below that), even while young; AGENT_COORD_MEM_HISTORY=0 turns this
# off. The last MEM_HISTORY_KEEP runs count.
MEM_HISTORY = os.environ.get("AGENT_COORD_MEM_HISTORY", "1") != "0"
MEM_HISTORY_RUNS, MEM_HISTORY_KEEP = 3, 10

HELP = """\
agent-coord - share this computer's CPU and RAM between agents, and message them

QUOTA
  agent-coord check [--io] [--cores N] [--mem SIZE] [--exclusive | --exclusive-lock NAME]
      Exit 0 if a lease that size would start now, else 1 with the reason.
  agent-coord run [--io] [--cores N] [--mem SIZE] --label TEXT [--exclusive | --exclusive-lock NAME]
                  [--headroom N] [--expect D] [--max-wait D] [--detach --unit NAME] -- CMD ...
      Wait in line until N cores and SIZE of RAM are free, then run CMD capped
      at them (systemd scope: CPUQuota, MemoryMax; exceeding MemoryMax kills
      CMD) at nice 19 and idle IO priority. On Windows CMD runs in a job object
      (hard CPU rate cap, job memory limit, idle priority; allocations past the
      limit fail). CMD sees COMPUTE_LEASE_CORES=N:
      give its tools that many threads. The lease ends when CMD exits.
  agent-coord hold [--io] [--cores N] [--mem SIZE] --label TEXT
                   [--pid PID] [--pidfile PATH] [--unit NAME] [--exclusive]
      Register a job that is already running (no waiting: it is running),
      until PID exits. Prints the lease ID. Holding a VM's qemu PID replaces
      its measured use with the lease. A PID file or unit follows restarts.
  agent-coord hold --label watch:NAME --pid PID --owner-pid PID --expect D
      Register a watcher without a compute lease. D is a hard expiry.
  agent-coord poll ID [--json]   Track a lease across process restarts.
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
  agent-coord phase [ID] (--exclusive-lock NAME | --release NAME)
      Acquire or release an exclusive lock on a running lease. Without ID,
      use the enclosing run. Acquisition fails if the lock or capacity is busy.
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

  agent-coord status [--json]              Leases, line, warnings, free capacity.
  agent-coord history [--since D] [--json] Lease events (requested, waiting,
                                           started, ended, cancelled) with
                                           wait/run times and measured peaks
                                           (memory, cores); default last 24h.
  agent-coord history --peaks [PREFIX] [--last N] [--since D] [--json]
      Per label starting with PREFIX: measured memory and CPU peaks of its
      last N (10) runs in D (30 days), and --mem suggested as peak + 15%.

SERVICE (Windows; on Linux use systemctl --user ... agent-coord)
  agent-coord service install|uninstall|start|stop|restart|status
      install starts the service at logon (HKCU Run key) and now. Without it,
      the CLI starts the service on demand.

Lease anything using more than 2 cores or 2 GB for more than a few minutes.
Running QEMU guests and other unleased load are measured and counted. --as NAME sets who you are
(default: $AGENT_COORD_AS, else your git worktree's folder name). Sizes: 512M, 8G.
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
    if not 0 < pid < 2**31:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start(pid: int) -> str | None:
    """Linux process identity, excluding exited but unreaped processes."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] in ("Z", "X") else fields[19]
    except (OSError, IndexError):
        return None


def signal_watcher(pid: int, start: str, sig: int) -> None:
    # Pin the process on kernels/Python versions with pidfds so PID reuse
    # between the identity check and signalling cannot hit a different job.
    fd = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                pass
        if process_start(pid) == start:
            if fd is None:
                os.kill(pid, sig)
            else:
                signal.pidfd_send_signal(fd, sig)
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)


def unit_name(name: str) -> str:
    if not UNIT_NAME.fullmatch(name):
        raise ValueError("unit name must contain only letters, digits, '.', '_', '@' or '-'")
    if sys.platform.startswith("linux") and not name.endswith(".service"):
        name += ".service"
    return name


def unit_pid(name: str) -> int:
    if sys.platform.startswith("linux"):
        cmd = ["systemctl", "--user", "show", "--property=MainPID", "--value", "--", name]
        pattern = r"\s*(\d+)\s*"
    elif sys.platform == "darwin":
        cmd = ["launchctl", "print", f"gui/{os.getuid()}/{name}"]
        pattern = r"(?m)^\s*pid = (\d+)\s*$"
    else:
        return 0
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.search(pattern, output)
    return int(match[1]) if match else 0


def read_pidfile(path: str) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, encoding="ascii") as f:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return 0
            text = f.read(32).strip()
        pid = int(text) if text.isascii() and text.isdigit() else 0
        return pid if 0 < pid < 2**31 else 0
    except (OSError, ValueError):
        return 0


def gpu_utilization() -> float | None:
    """Max device utilization; a busy device or unknown reading is not idle."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        )
        values = [float(line) for line in out.splitlines()]
        if values and all(0 <= value <= 100 for value in values):
            return max(values)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def mem_total() -> int:
    if DARWIN:
        return psutil.virtual_memory().total
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("no MemTotal in /proc/meminfo")


def capacity() -> tuple[float, int]:
    return float(os.cpu_count() or 1), mem_total() - RESERVE_MEM


def _cpu_seconds(pid: int, include_children: bool = False) -> float | None:
    if DARWIN:
        try:
            counts = psutil.Process(pid).cpu_times()
            return counts.user + counts.system
        except psutil.Error:
            return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return None
    end = 15 if include_children else 13
    return sum(int(ticks) for ticks in fields[11:end]) / os.sysconf("SC_CLK_TCK")


def _footprint(pid: int) -> int:
    """Resident plus swapped-out memory: what the process needs back in RAM."""
    if DARWIN:
        try:
            return psutil.Process(pid).memory_info().rss
        except psutil.Error:
            return 0
    total = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmSwap:")):
                total += int(line.split()[1]) * 1024
    except OSError:
        pass
    return total


def _tree_pids(pid: int):
    if DARWIN:
        yield pid
        try:
            yield from (child.pid for child in psutil.Process(pid).children(recursive=True))
        except psutil.Error:
            pass
        return
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


SCOPE_PREFIX = "agent-coord-"
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _cgroup_of(pid: int) -> str | None:
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line[3:]
    except OSError:
        pass
    return None


def _cgroup_int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def scope_memory(pid: int) -> tuple[int, int | None] | None:
    """(in use, kernel peak) of the agent-coord run scopes (cgroups v2) holding
    pid's process tree, or None when there are none (hold, no systemd).

    In use is what reclaim cannot take back: memory.current less reclaimable
    page cache and slab (as MemAvailable counts them as available), plus swap.
    The kernel peak (memory.peak) includes page cache, so it is an upper bound."""
    groups = set()
    for current in _tree_pids(pid):
        group = _cgroup_of(current)
        if group and Path(group).name.startswith(SCOPE_PREFIX) and group.endswith(".scope"):
            groups.add(group)
    use, peak, found = 0, None, False
    for group in groups:
        base = CGROUP_ROOT / group.lstrip("/")
        current = _cgroup_int(base / "memory.current")
        if current is None:
            continue
        found = True
        stat = {}
        try:
            for line in (base / "memory.stat").read_text().splitlines():
                key, _, value = line.partition(" ")
                stat[key] = int(value)
        except (OSError, ValueError):
            pass
        cache = max(0, stat.get("file", 0) - stat.get("shmem", 0)) + stat.get("slab_reclaimable", 0)
        use += max(0, current - cache) + (_cgroup_int(base / "memory.swap.current") or 0)
        if (high := _cgroup_int(base / "memory.peak")) is not None:
            peak = (peak or 0) + high
    return (use, peak) if found else None


def tree_cpu_seconds(pid: int) -> float | None:
    """CPU time of the live process tree, plus reaped children on Linux."""
    pids = _tree_pids(pid)
    root = _cpu_seconds(next(pids), include_children=True)
    if root is None:
        return None
    return root + sum(_cpu_seconds(child, include_children=True) or 0 for child in pids)


def _host_busy() -> tuple[float, float]:
    """(busy, total) CPU seconds since boot, summed over all cores."""
    if DARWIN:
        counts = psutil.cpu_times()
        return sum(counts) - counts.idle, sum(counts)
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    tick = os.sysconf("SC_CLK_TCK")
    return (sum(fields[:8]) - idle) / tick, sum(fields[:8]) / tick


def mem_available() -> int:
    if DARWIN:
        return psutil.virtual_memory().available
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
    if DARWIN:
        return []  # host-wide samples already account for unleased VM load
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
    if name := os.environ.get("AGENT_COORD_AS", "").strip():
        return name
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


def load_average() -> list[float] | None:
    return list(os.getloadavg()) if hasattr(os, "getloadavg") else None


# ---------------------------------------------------------------- Windows
# The same measurements from the Win32 API instead of /proc.

if WINDOWS:
    import ctypes
    import msvcrt
    import winreg
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _HANDLE, _DWORD, _BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    _U64 = ctypes.c_uint64
    _SIZE = ctypes.c_size_t

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", _DWORD), ("dwMemoryLoad", _DWORD),
                    ("ullTotalPhys", _U64), ("ullAvailPhys", _U64),
                    ("ullTotalPageFile", _U64), ("ullAvailPageFile", _U64),
                    ("ullTotalVirtual", _U64), ("ullAvailVirtual", _U64),
                    ("ullAvailExtendedVirtual", _U64)]  # fmt: skip

    class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [("cb", _DWORD), ("PageFaultCount", _DWORD),
                    ("PeakWorkingSetSize", _SIZE), ("WorkingSetSize", _SIZE),
                    ("QuotaPeakPagedPoolUsage", _SIZE), ("QuotaPagedPoolUsage", _SIZE),
                    ("QuotaPeakNonPagedPoolUsage", _SIZE), ("QuotaNonPagedPoolUsage", _SIZE),
                    ("PagefileUsage", _SIZE), ("PeakPagefileUsage", _SIZE),
                    ("PrivateUsage", _SIZE)]  # fmt: skip

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", _DWORD), ("cntUsage", _DWORD), ("th32ProcessID", _DWORD),
                    ("th32DefaultHeapID", _SIZE), ("th32ModuleID", _DWORD),
                    ("cntThreads", _DWORD), ("th32ParentProcessID", _DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", _DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]  # fmt: skip

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", _DWORD),
                    ("MinimumWorkingSetSize", _SIZE), ("MaximumWorkingSetSize", _SIZE),
                    ("ActiveProcessLimit", _DWORD), ("Affinity", _SIZE),
                    ("PriorityClass", _DWORD), ("SchedulingClass", _DWORD)]  # fmt: skip

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, _U64) for n in ("ReadOperationCount", "WriteOperationCount",
                                        "OtherOperationCount", "ReadTransferCount",
                                        "WriteTransferCount", "OtherTransferCount")]  # fmt: skip

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS), ("ProcessMemoryLimit", _SIZE),
                    ("JobMemoryLimit", _SIZE), ("PeakProcessMemoryUsed", _SIZE),
                    ("PeakJobMemoryUsed", _SIZE)]  # fmt: skip

    class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        _fields_ = [("ControlFlags", _DWORD), ("CpuRate", _DWORD)]

    def _sig(fn, restype, *argtypes):
        fn.restype, fn.argtypes = restype, list(argtypes)
        return fn

    _OpenProcess = _sig(_k32.OpenProcess, _HANDLE, _DWORD, _BOOL, _DWORD)
    _CloseHandle = _sig(_k32.CloseHandle, _BOOL, _HANDLE)
    _WaitForSingleObject = _sig(_k32.WaitForSingleObject, _DWORD, _HANDLE, _DWORD)
    _GetProcessTimes = _sig(_k32.GetProcessTimes, _BOOL, _HANDLE, *[ctypes.POINTER(_U64)] * 4)
    _GetSystemTimes = _sig(_k32.GetSystemTimes, _BOOL, *[ctypes.POINTER(_U64)] * 3)
    _GlobalMemoryStatusEx = _sig(_k32.GlobalMemoryStatusEx, _BOOL,
                                 ctypes.POINTER(_MEMORYSTATUSEX))
    _GetProcessMemoryInfo = _sig(_k32.K32GetProcessMemoryInfo, _BOOL, _HANDLE,
                                 ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), _DWORD)
    _CreateToolhelp32Snapshot = _sig(_k32.CreateToolhelp32Snapshot, _HANDLE, _DWORD, _DWORD)
    _Process32FirstW = _sig(_k32.Process32FirstW, _BOOL, _HANDLE,
                            ctypes.POINTER(_PROCESSENTRY32W))
    _Process32NextW = _sig(_k32.Process32NextW, _BOOL, _HANDLE,
                           ctypes.POINTER(_PROCESSENTRY32W))
    _GetVolumePathNameW = _sig(_k32.GetVolumePathNameW, _BOOL, wintypes.LPCWSTR,
                               wintypes.LPWSTR, _DWORD)
    _CreateJobObjectW = _sig(_k32.CreateJobObjectW, _HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _SetInformationJobObject = _sig(_k32.SetInformationJobObject, _BOOL, _HANDLE,
                                    ctypes.c_int, ctypes.c_void_p, _DWORD)
    _AssignProcessToJobObject = _sig(_k32.AssignProcessToJobObject, _BOOL, _HANDLE, _HANDLE)
    _NtResumeProcess = _sig(_ntdll.NtResumeProcess, ctypes.c_long, _HANDLE)

    _SYNCHRONIZE, _QUERY_LIMITED = 0x00100000, 0x1000
    _WAIT_TIMEOUT, _ERROR_ACCESS_DENIED = 0x102, 5
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _TICKS = 10_000_000  # FILETIME units (100 ns) per second

    def _open(pid: int, access: int = _QUERY_LIMITED):
        return _OpenProcess(access, False, pid) if pid > 0 else None

    def alive(pid: int) -> bool:  # noqa: F811 - os.kill(pid, 0) would terminate it
        handle = _open(pid, _SYNCHRONIZE | _QUERY_LIMITED)
        if not handle:
            return pid > 0 and ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            return _WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            _CloseHandle(handle)

    def _memory_status() -> "_MEMORYSTATUSEX":
        status = _MEMORYSTATUSEX(dwLength=ctypes.sizeof(_MEMORYSTATUSEX))
        if not _GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return status

    def mem_total() -> int:  # noqa: F811
        return int(_memory_status().ullTotalPhys)

    def mem_available() -> int:  # noqa: F811
        return int(_memory_status().ullAvailPhys)

    def _process_times(pid: int) -> tuple[int, float] | None:
        """(creation time, CPU seconds) of a running pid, else None."""
        handle = _open(pid, _SYNCHRONIZE | _QUERY_LIMITED)
        if not handle:
            return None
        try:
            if _WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT:
                return None
            created, ended, kernel, user = _U64(), _U64(), _U64(), _U64()
            if not _GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended),
                                    ctypes.byref(kernel), ctypes.byref(user)):  # fmt: skip
                return None
            return created.value, (kernel.value + user.value) / _TICKS
        finally:
            _CloseHandle(handle)

    def _cpu_seconds(pid: int, include_children: bool = False) -> float | None:  # noqa: F811
        if include_children:
            tree = _windows_tree(pid)
            root = next(tree, None)
            return None if root is None else root[1] + sum(cpu for _, cpu in tree)
        times = _process_times(pid)
        return times[1] if times else None

    def _footprint(pid: int) -> int:  # noqa: F811
        """Working set or private commit, whichever is larger: what the
        process needs in RAM (the counterpart of VmRSS + VmSwap)."""
        handle = _open(pid, _SYNCHRONIZE | _QUERY_LIMITED)
        if not handle:
            return 0
        try:
            if _WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT:
                return 0
            counters = _PROCESS_MEMORY_COUNTERS_EX(cb=ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX))
            if not _GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return 0
            return int(max(counters.WorkingSetSize, counters.PrivateUsage))
        finally:
            _CloseHandle(handle)

    def _processes() -> list[tuple[int, int, str]]:
        """(pid, parent pid, exe name) of every process."""
        snap = _CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == _INVALID_HANDLE:
            return []
        found = []
        try:
            entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
            ok = _Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                ok = _Process32NextW(snap, ctypes.byref(entry))
        finally:
            _CloseHandle(snap)
        return found

    def _windows_tree(pid: int):
        """Yield live (pid, CPU seconds) pairs rooted at pid."""
        root = _process_times(pid)
        if root is None:
            return
        children: dict[int, list[int]] = {}
        for child, parent, _name in _processes():
            if child != parent:
                children.setdefault(parent, []).append(child)
        seen, stack = set(), [(pid, root)]
        while stack:
            current, (created, cpu) = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            yield current, cpu
            for child in children.get(current, ()):
                times = _process_times(child)
                # A recycled parent PID must not adopt an older process.
                if times and times[0] >= created:
                    stack.append((child, times))

    def tree_pids(pid: int) -> set[int]:
        return {child for child, _ in _windows_tree(pid)}

    def tree_footprint(pid: int) -> int:  # noqa: F811
        return sum(_footprint(child) for child, _ in _windows_tree(pid))

    def tree_cpu_seconds(pid: int) -> float | None:
        """CPU time of the live process tree; reaped children are unavailable."""
        return _cpu_seconds(pid, include_children=True)

    def _host_busy() -> tuple[float, float]:  # noqa: F811
        idle, kernel, user = _U64(), _U64(), _U64()
        if not _GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        total = kernel.value + user.value  # kernel time includes idle time
        return (total - idle.value) / _TICKS, total / _TICKS

    def measure_vms(sample: float = 1.0) -> list[dict]:  # noqa: F811
        """Running QEMU guests and WSL2/Hyper-V worker processes (vmmem)."""
        guests: dict[int, dict] = {}
        for pid, _parent, exe in _processes():
            name = exe.lower()
            if name.startswith("qemu-system"):
                guests[pid] = {"pid": pid, "name": Path(exe).stem}
            elif name in ("vmmem", "vmmem.exe", "vmmemwsl", "vmmemwsl.exe"):
                guests[pid] = {"pid": pid, "name": "wsl" if "wsl" in name else "hyper-v"}
        before = {pid: _cpu_seconds(pid) for pid in guests}
        if guests:
            time.sleep(sample)
        for pid, guest in guests.items():
            after, start = _cpu_seconds(pid), before[pid]
            guest["cores"] = (round((after - start) / sample, 1)
                              if after is not None and start is not None else 0.0)  # fmt: skip
            guest["mem"] = _footprint(pid)
        # Worker processes this user may not open measure as nothing: leave
        # them to the available-memory check instead of listing zeros.
        return [g for g in guests.values() if g["mem"] or g["cores"]]


# ---------------------------------------------------------------- service


class Coordinator:
    """All state, guarded by one lock; saved to STATE_FILE on every change."""

    def __init__(self, state_file: Path, measure=measure, cap=capacity,
                 history_file: Path | None = None, footprint=tree_footprint,
                 cpu_seconds=tree_cpu_seconds, tree_pids=tree_pids,
                 scope_memory=scope_memory, measured_mem: bool | None = None,
                 gpu_sample=gpu_utilization):
        self._saved = None
        self._peak_samples: dict[tuple, list[int]] = {}
        self.scope_memory = scope_memory
        self.measured_mem = MEASURED_MEM if measured_mem is None else measured_mem
        # Per lease, sampled each tick beside the available-memory sample:
        # memory in use (attributed once per process, nested leases taken out
        # of the outer one), its peak, when the peak last rose by a ramp step,
        # the kernel's scope peak, and peak CPU use (cores, smoothed).
        self._mem_use: dict[str, int] = {}
        self._mem_peak: dict[str, int] = {}
        self._mem_step: dict[str, tuple[int, float]] = {}
        self._scope_peak: dict[str, int] = {}
        self._cores_peak: dict[str, float] = {}
        self._peak_cpu: tuple[float, dict[str, float]] | None = None
        self._peak_own: dict[str, float] = {}
        self._samples = None
        self._footprints = None
        self.disk_free = disk_free
        self.footprint = footprint
        self.cpu_seconds = cpu_seconds
        self._cpu_sample: tuple[float, dict[str, float]] | None = None
        self._measured_cores: dict[str, float] = {}
        # A lease's own CPU use, smoothed like self.busy (from zero, as the
        # host-wide figure is for a new lease's load), to take it out of the
        # host-wide measurement when the lease is counted at its full size;
        # and each measured lease's process tree, to count nested ones once.
        self._own_cores: dict[str, float] = {}
        self._trees: dict[str, set[int]] = {}
        self.tree_pids = tree_pids
        self._low_since: dict[tuple[str, str], float] = {}
        self.gpu_sample = gpu_sample
        self._track_at = -math.inf
        self._exclusive_sample_at = -math.inf
        self.state_file = state_file
        self.history_file = history_file or state_file.with_name(HISTORY_FILE.name)
        self.measure = measure
        self.capacity = cap
        self.lock = threading.Lock()
        self.vms: list[dict] = []
        # Cores busy host-wide (smoothed) and memory available, as last measured.
        self.busy = 0.0
        self.available: int | None = None
        self.state: dict = {"leases": [], "queue": [], "messages": [], "read": {},
                            "agents": {}, "locks": {}, "watchers": [], "next": 1}  # fmt: skip
        if state_file.exists():
            try:
                self.state.update(json.loads(state_file.read_text()))
                self.state.setdefault("locks", {})
            except ValueError:
                pass
        for x in self.state["leases"]:
            for usage in x.get("exclusive_usage", {}).values():
                for key in ("sample_at", "idle_since", "busy"):
                    usage.pop(key, None)
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
                 "exclusive": exclusive_mode(x), "io": bool(x.get("io"))}  # fmt: skip
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
            self._close_exclusive(x, self._exclusive_resources(x), now)
            granted = x.get("granted", now)
            self._log("ended", x, wait=round(granted - x["asked"], 1),
                      run=round(now - granted, 1), held=bool(x.get("running")),
                      **self._peaks(x["id"]), **extra)  # fmt: skip
        else:
            self._log("cancelled", x, wait=round(now - x["asked"], 1), **extra)

    def _peaks(self, lease_id: str) -> dict:
        """A lease's measured peaks so far, for its 'ended' event (sampled
        every tick, so a spike shorter than a tick can be missed; the scope's
        kernel peak catches it but includes page cache)."""
        peaks = {}
        if lease_id in self._mem_peak:
            peaks["mem_peak"] = self._mem_peak[lease_id]
        if lease_id in self._scope_peak:
            peaks["mem_peak_scope"] = self._scope_peak[lease_id]
        if lease_id in self._cores_peak:
            peaks["cores_peak"] = round(self._cores_peak[lease_id], 2)
        return peaks

    def _prune(self) -> bool:
        changed = False
        now = time.time()
        for key in ("leases", "queue"):
            live = []
            for x in self.state[key]:
                managed = x.get("run_unit")
                if managed and "missing_since" not in x and not alive(x["pid"]):
                    for name in list(x.get("phase_locks", {})):
                        self._phase_release(x, name)
                    x["missing_since"] = now
                    self._log("restarting", x)
                    changed = True
                tracked = x.get("unit") or x.get("pidfile") or managed
                if tracked and now - x.get("missing_since", now) < RESTART_GRACE:
                    live.append(x)
                elif not tracked and alive(x["pid"]):
                    live.append(x)
                else:
                    self._log_end(x, key == "leases",
                                  why="registration inactive" if tracked else "process exited")
            changed |= len(live) != len(self.state[key])
            self.state[key] = live
        changed |= self._prune_watchers(now)
        for name, lock in list(self.state["locks"].items()):
            if (lock.get("pid") and not alive(lock["pid"])) or (
                lock.get("expires") and lock["expires"] < now
            ):
                del self.state["locks"][name]
                changed = True
        return changed

    def _prune_watchers(self, now: float) -> bool:
        live = []
        changed = False
        watchers = self.state["watchers"]
        for watch in watchers:
            pid = watch["pid"]
            if process_start(pid) != watch["start"]:
                if "kill_after" not in watch:
                    self._log("watch_ended", watch, why="process exited")
                changed = True
                continue
            if "kill_after" in watch:
                if now >= watch["kill_after"]:
                    signal_watcher(pid, watch["start"], signal.SIGKILL)
                    changed = True
                    continue
                live.append(watch)
                continue
            if process_start(watch["owner_pid"]) != watch["owner_start"]:
                why = "owner exited"
            elif now >= watch["expires"]:
                why = "expired"
            else:
                live.append(watch)
                continue
            signal_watcher(pid, watch["start"], signal.SIGTERM)
            # Let agent-wait stop its child command and post a result first.
            watch["kill_after"] = now + TICK
            live.append(watch)
            self._log("watch_ended", watch, why=why)
            changed = True
        self.state["watchers"] = live
        return changed

    def _refresh_tracking(self, stamp: float) -> None:
        if stamp - self._track_at < TRACK_INTERVAL:
            return
        self._track_at = stamp
        with self.lock:
            targets = [(x["id"], x.get("unit"), x.get("pidfile"))
                       for x in self.state["leases"] if x.get("pidfile") or x.get("unit")]
        units = {name: unit_pid(name) for name in {unit for _, unit, _ in targets if unit}}
        files = {path: read_pidfile(path) for path in {file for _, _, file in targets if file}}
        found = {}
        for lease_id, unit, file in targets:
            candidates = (units.get(unit, 0), files.get(file, 0))
            found[lease_id] = next((pid for pid in candidates if pid > 0 and alive(pid)), 0)
        now = time.time()
        changed = False
        with self.lock:
            for x in self.state["leases"]:
                if x["id"] not in found:
                    continue
                pid = found[x["id"]]
                if pid != x["pid"]:
                    for name in list(x.get("phase_locks", {})):
                        self._phase_release(x, name)
                    x["pid"] = pid
                    lease_id = x["id"]
                    self._own_cores.pop(lease_id, None)
                    self._measured_cores.pop(lease_id, None)
                    self._trees.pop(lease_id, None)
                    if self._cpu_sample:
                        self._cpu_sample[1].pop(lease_id, None)
                    if self._peak_cpu:
                        self._peak_cpu[1].pop(lease_id, None)
                    changed = True
                if pid and "missing_since" in x:
                    del x["missing_since"]
                    self._log("resumed", x)
                    changed = True
                elif not pid and "missing_since" not in x:
                    x["missing_since"] = now
                    self._log("restarting", x)
                    changed = True
            if changed:
                self._save()

    def _lock_holder(self, name: str, lease_id: str | None = None) -> str | None:
        """Who holds named resource name (a lock, or another lease's --lock)."""
        if lock := self.state["locks"].get(name):
            return f"{lock['agent']} ({lock['reason']})"
        for x in self.state["leases"]:
            if name in x.get("locks", ()) and x["id"] != lease_id:
                return f"{x['agent']}'s lease {x['id']} ({x['label']})"
        return None

    @staticmethod
    def _same_run(existing: dict, new: dict) -> bool:
        phases = existing.get("phase_locks", {})
        base_locks = {name for name in existing["locks"] if phases.get(name, True)}
        base_exclusive = set(existing.get("exclusive_locks", ())) - phases.keys()
        return (base_locks == set(new["locks"])
                and base_exclusive == set(new.get("exclusive_locks", ()))
                and all(existing.get(k) == new.get(k) for k in
                        ("agent", "label", "cores", "mem", "disk", "exclusive", "headroom", "io")))

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
        leased = sum(self._cpu_claim(x) for x in self.state["leases"])
        return max(0.0, self.busy - sum(g["cores"] for g in self._vms()) - leased)

    def _cpu_claim(self, lease: dict) -> float:
        """Full size for a CPU lease; observed use for IO (zero before sampling)."""
        return self._measured_cores.get(lease.get("id"), 0.0) if lease.get("io") else lease["cores"]

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

    # -- measured memory admission

    def _mem_total(self) -> int:
        return self.capacity()[1] + RESERVE_MEM

    def _growth(self, x: dict, now: float) -> int:
        """Memory lease x may still take beyond its use at the last sample
        (the one beside the available-memory sample): back up to its peak in
        full, then a share of the rest of its size: all of it while the lease
        is young, unsampled or still ramping up (its peak rose in the last
        MEM_RAMP), else MEM_GROWTH of it but at least MEM_GROWTH_FLOOR. Never
        more than its size less its use: the hard cap stops it there."""
        size = self._mem_claim(x)
        use = min(self._mem_use.get(x["id"], 0), size)
        peak = min(max(self._mem_peak.get(x["id"], 0), use), size)
        left = size - peak
        expected = self._label_ceiling(x) if x["id"] in self._mem_use else None
        if expected is not None and peak <= expected:
            # Runs of this label peaked below expected: once sampled, count
            # growth to it (at least the floor), young or not. A lease that
            # goes past it loses this and counts as below.
            return (peak - use) + max(min(left, MEM_GROWTH_FLOOR), min(expected, size) - peak)
        rose_at = self._mem_step.get(x["id"], (0, -math.inf))[1]
        ramping = (x["id"] not in self._mem_use or now - x.get("granted", now) < MEM_RAMP
                   or now - rose_at < MEM_RAMP)  # fmt: skip
        share = left if ramping else max(min(left, MEM_GROWTH_FLOOR), int(MEM_GROWTH * left))
        return (peak - use) + share

    def _label_ceiling(self, x: dict) -> int | None:
        """Largest measured peak of the lease's label's last runs (this
        agent's, else any agent's) plus PEAK_SIZING_MARGIN, once it has
        MEM_HISTORY_RUNS successful measured runs; else None."""
        if not MEM_HISTORY or x.get("io"):
            return None
        self._runs(x["agent"], x["label"])  # loads the history once
        peaks = self._peak_samples.get((x["agent"], x["label"]), [])
        if len(peaks) < MEM_HISTORY_RUNS:
            peaks = self._peak_samples.get((None, x["label"]), [])
        if len(peaks) < MEM_HISTORY_RUNS:
            return None
        return math.ceil(max(peaks) * (1 + PEAK_SIZING_MARGIN))

    def _mem_room(self, ending=()) -> tuple[int, int] | None:
        """(room, overcommit room) for new leases under measured admission,
        with the leases in ending gone (they return what they use), or None
        when it is off or nothing has been measured yet.

        room: memory actually available (at most RAM), less every remaining
        lease's growth allowance, the reserve and MEM_MARGIN of RAM.
        overcommit room: what MEM_OVERCOMMIT times the lease capacity leaves
        beside the remaining leases' sizes and unleased VMs."""
        if not self.measured_mem or self.available is None:
            return None
        now = time.time()
        total = self._mem_total()
        gone = {x["id"] for x in ending}
        remaining = [x for x in self.state["leases"] if x["id"] not in gone]
        freed = sum(min(self._mem_use.get(x["id"], 0), self._mem_claim(x)) for x in ending)
        growth = sum(self._growth(x, now) for x in remaining)
        room = (min(self.available, total) + freed - growth - RESERVE_MEM
                - int(MEM_MARGIN * total))  # fmt: skip
        sizes = sum(self._mem_claim(x) for x in remaining) + sum(g["mem"] for g in self._vms())
        return room, int(MEM_OVERCOMMIT * self.capacity()[1]) - sizes

    def _mem_fits(self, need: int, ending=()) -> bool:
        """Whether need (new leases at their full size) fits by measured use."""
        room = self._mem_room(ending)
        return room is not None and need <= min(room)

    def _reserved_cpu(self, leases: list[dict]) -> float:
        warming = {x["id"] for x in self._warming()}
        return sum(x["cores"] for x in leases
                   if not x.get("io") and (x["id"] in warming or x.get("exclusive_locks")))

    def _covered(self) -> set[str]:
        """Measured leases whose process is inside another measured lease's
        tree (a lease started from within a lease, or two leases on one
        pid): their use is already in the outer one's measurement."""
        measured = [x for x in self.state["leases"] if x["id"] in self._own_cores]
        covered = set()
        for i, x in enumerate(measured):
            for j, outer in enumerate(measured):
                if outer is x:
                    continue
                tree = self._trees.get(outer["id"], {outer["pid"]})
                if x["pid"] in tree and (x["pid"] != outer["pid"] or j < i):
                    covered.add(x["id"])
                    break
        return covered

    def _counted_once(self) -> set[str]:
        """Leases exclusive on a lock (timing runs), unless IO: reserved at
        their full size all along, so their own measured use comes out of the
        measured load. (Warming leases keep main's conservative double count.)"""
        return {x["id"] for x in self.state["leases"]
                if not x.get("io") and x.get("exclusive_locks")}

    def _net_busy(self, released: set[str] | None = None) -> float:
        """Measured busy cores, less the own use of exclusive-lock leases, so
        a timing run counts once, at its size, not at its size plus its
        measured use. Only use up to its size comes off: use above it (a job
        in its process tree but in a scope of its own, an uncapped hold,
        noise) stays counted as load. Lease use is smoothed like the busy
        figure, and counted once per process tree."""
        once = self._counted_once() - (released or set())
        covered = self._covered()
        net = self.busy
        for x in self.state["leases"]:
            own = self._own_cores.get(x["id"])
            if own is not None and x["id"] in once and x["id"] not in covered:
                net -= min(own, x["cores"])
        return max(0.0, net)

    def _warming(self) -> list[dict]:
        """Leases started in the last WARMUP seconds: not yet at full speed,
        so not yet fully in the measurement."""
        now = time.time()
        return [x for x in self.state["leases"]
                if not x.get("running") and now - x.get("granted", now) < WARMUP]  # fmt: skip

    def _usage(self, borrow: set[str] | None = None) -> tuple[float, int]:
        """(cores, memory) in use. CPU is measured, since leases mostly use
        less than they ask and sharing CPU only slows everyone a little (each
        lease is capped at its size, at nice 19); new leases count at their
        size until they ramp up. Memory counts every lease at its size, since
        running out of it means swapping or killing."""
        # Leases exclusive on a lock (timing runs) count in full all along, so
        # their cores stay guaranteed; only the rest of the measured load
        # counts beside them (their own measured use is taken out of it).
        leases = self.state["leases"]
        cores = max(self._net_busy(borrow), sum(self._cpu_claim(x) for x in leases if x.get("io")))
        cores += self._reserved_cpu([x for x in leases if x["id"] not in (borrow or ())])
        vms = self._vms()
        mem = sum(self._mem_claim(x) for x in leases) + sum(g["mem"] for g in vms)
        return cores, mem

    @staticmethod
    def _exclusive_resources(lease: dict) -> list[str]:
        return ["machine"] if lease.get("exclusive") else lease.get("exclusive_locks", [])

    def _usage_record(self, lease: dict, resource: str) -> dict:
        return lease.setdefault("exclusive_usage", {}).setdefault(
            resource, {"since": lease["granted"], "observed": 0.0, "active": 0.0}
        )

    def _close_exclusive(self, lease: dict, resources: list[str], now: float) -> None:
        for resource in list(resources):
            usage = self._usage_record(lease, resource)
            self._log("exclusive-ended", lease, resource=resource,
                      exclusive_seconds=round(now - usage["since"], 1),
                      observed_seconds=round(usage["observed"], 1),
                      active_seconds=round(usage["active"], 1))
            del lease["exclusive_usage"][resource]

    def _exclusive_idle(self, lease: dict, resource: str, now: float) -> bool:
        usage = lease.get("exclusive_usage", {}).get(resource, {})
        return (now - usage.get("sample_at", -math.inf) <= 2 * EXCLUSIVE_SAMPLE
                and now - usage.get("idle_since", now) >= EXCLUSIVE_IDLE_AFTER)

    def _monitor_exclusive(self, now: float, gpu: float | None) -> bool:
        changed = False
        for x in self.state["leases"]:
            resources = self._exclusive_resources(x)
            if not resources:
                continue
            warnings = x.setdefault("exclusive_warnings", {})
            if (x.get("expect_source") == "explicit"
                    and now - x["granted"] > x["expect"] * 1.25
                    and "overrun" not in warnings):
                reason = (f"{x['id']} ({x['label']}) holds exclusive resources and "
                          "has exceeded --expect by more than 25%; release any unneeded locks.")
                warnings["overrun"] = reason
                if not x.get("exclusive_overrun_told"):
                    x["exclusive_overrun_told"] = True
                    self._post("agent-coord", x["agent"], reason)
                    self._log("exclusive-overrun", x, reason=reason)
                changed = True
            for resource in resources:
                usage = self._usage_record(x, resource)
                cpu = self._measured_cores.get(x["id"])
                if resource == "gpu":
                    busy = gpu > 1 if gpu is not None else None
                elif x["exclusive"] and cpu is not None:
                    busy = cpu >= .05 or (gpu is not None and gpu > 1)
                else:
                    busy = None
                before = usage.get("sample_at")
                contiguous = (before is not None and 0 <= now - before <= 2 * EXCLUSIVE_SAMPLE
                              and busy is not None and usage.get("busy") is not None)
                if contiguous:
                    elapsed = now - before
                    usage["observed"] += elapsed
                    if busy or usage["busy"]:
                        usage["active"] += elapsed
                if busy is not False:
                    usage.pop("idle_since", None)
                elif not contiguous or "idle_since" not in usage:
                    usage["idle_since"] = now
                usage.update(sample_at=now, busy=busy)
                warning_key = f"idle:{resource}"
                if self._exclusive_idle(x, resource, now):
                    if warning_key not in warnings:
                        reason = (f"{x['id']} ({x['label']}) held {resource} exclusively with "
                                  "near-zero measured use for 15 min; release it if unneeded.")
                        warnings[warning_key] = reason
                        self._post("agent-coord", x["agent"], reason)
                        self._log("exclusive-idle", x, resource=resource, reason=reason)
                else:
                    warnings.pop(warning_key, None)
                changed = True
        return changed

    def _claimed(self) -> float:
        """Cores claimed: full size for CPU leases, measured use otherwise."""
        leased = sum(self._cpu_claim(x) for x in self.state["leases"])
        return leased + sum(g["cores"] for g in self._vms()) + self._unleased()

    def _too_big(self, req: dict) -> str | None:
        cores, mem = self.capacity()
        if (max(req["cores"], self._cpu_claim(req) + req.get("headroom", 0)) > cores
                or req["mem"] > mem):
            return f"more than this computer has ({cores:g} cores, {human(mem)})"
        return None

    def _why(self, req: dict) -> tuple[str, str] | None:
        """(kind, reason) req can't start now, ignoring the line, or None.
        kind is a stable category (exclusive, lock:NAME, disk, cores, mem,
        available, headroom, too-big) for the history."""
        leases = self.state["leases"]
        now = time.time()
        small = (req["cores"] <= SMALL_CORES and req["mem"] <= SMALL_MEM
                 and not any(req.get(k) for k in ("exclusive", "locks", "headroom", "disk")))
        holder = next((x for x in leases if x["exclusive"]), None)
        if holder and not (small and self._exclusive_idle(holder, "machine", now)):
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
        borrow = set()
        if small:
            for x in leases:
                locks = x.get("exclusive_locks", ())
                if (not x.get("io") and not x.get("headroom") and locks
                        and all(self._exclusive_idle(x, name, now) for name in locks)):
                    borrow.add(x["id"])
        used_cores, used_mem = self._usage(borrow)
        req_cores = self._cpu_claim(req)
        if used_cores + req_cores > cores:
            return "cores", f"{max(cores - used_cores, 0):.1f} of {cores:g} cores free"
        # Memory: by reservation (every lease at its size, as always), or by
        # measured use (see _mem_room) when that does not fit.
        why = None
        if used_mem + req["mem"] > mem:
            why = "mem", f"{human(max(mem - used_mem, 0))} of {human(mem)} free"
        elif self.available is not None:
            # The sample does not yet include warming leases' full claims,
            # including jobs granted earlier in this admission pass.
            growth = sum(max(0, x["mem"] - self._footprint(x["pid"]))
                         for x in self._warming())
            available = self.available - RESERVE_MEM - growth
            if req["mem"] > available:
                why = "available", (f"only {human(max(available, 0))} "
                                    "of memory actually available")  # fmt: skip
        if why and not self._mem_fits(req["mem"]):
            if room := self._mem_room():
                why = why[0], (f"{why[1]} by reservation; {human(max(min(room), 0))} "
                               "by measured use (after growth allowances and a safety margin)")
            return why
        keeper = max([req, *leases], key=lambda x: x.get("headroom", 0))
        if need := keeper.get("headroom", 0):
            claimed = self._claimed()
            if claimed + req_cores + need > cores:
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
        if pid <= 0:
            return 0
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
                        if holder.get("io") and self._cpu_claim(holder) < holder["cores"] / 2:
                            continue
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
        ending = [x for x in self.state["leases"]
                  if waiting_for & (set(x.get("locks", ())) - x.get("phase_locks", {}).keys())]
        freed_cores = sum(self._cpu_claim(x) for x in ending)
        freed_mem = sum(self._mem_claim(x) for x in ending)
        need_cores = self._cpu_claim(req) + self._cpu_claim(head)
        if used_cores - freed_cores + need_cores > cores:
            return False
        need = req["mem"] + head["mem"]
        reserved = used_mem - freed_mem + need <= mem
        if reserved and self.available is not None:
            freed_available = sum(self._measured_mem(ending).values())
            reserved = need <= self.available + freed_available - RESERVE_MEM
        if not reserved and not self._mem_fits(need, ending):
            return False
        return not (head.get("headroom") and (self._claimed() - freed_cores + need_cores
                                              + head["headroom"] > cores))  # fmt: skip

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
            if type(entry.get("mem_peak")) is int:
                peaks = self._peak_samples.setdefault(key, [])
                peaks.append(entry["mem_peak"])
                del peaks[:-MEM_HISTORY_KEEP]

    def _runs(self, agent: str, label: str) -> list[float]:
        if self._samples is None:
            self._samples = {}
            self._peak_samples = {}
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
        need = head["mem"] + (req["mem"] if req else 0)
        reserved = sum(self._mem_claim(x) for x in jobs) + sum(g["mem"] for g in self._vms()) <= mem
        if reserved and self.available is not None:
            footprints = self._measured_mem(self.state["leases"])
            freed = sum(footprints[x["id"]] for x in ending)
            growth = sum(self._mem_claim(x) - footprints[x["id"]] for x in remaining)
            reserved = need <= self.available + freed - growth - RESERVE_MEM
        if not reserved and not self._mem_fits(need, ending):
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
        if used + self._cpu_claim(head) + (self._cpu_claim(req) if req else 0) > cores:
            return False
        headroom = max(x.get("headroom", 0) for x in jobs)
        if headroom and sum(self._cpu_claim(x) for x in jobs) + outside + headroom > cores:
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
        if previous is None or now <= previous[0]:
            self._own_cores = {}
            return
        own = {}
        for x in self.state["leases"]:
            lease_id = x["id"]
            if lease_id in samples and lease_id in previous[1]:
                delta = samples[lease_id] - previous[1][lease_id]
                raw = max(0, delta) / (now - previous[0])
                self._measured_cores[lease_id] = min(x["cores"], raw)
                # Smoothed like self.busy, starting from 0 as the host-wide
                # figure effectively does: never subtract more than it holds.
                before = self._own_cores.get(lease_id, 0.0)
                own[lease_id] = before + BUSY_SMOOTHING * (raw - before)
        self._own_cores = own

    def _hint_io(self, now: float) -> bool:
        low_since = {}
        changed = False
        for x in self.state["leases"]:
            if x.get("io") or x.get("io_hint") or time.time() - x["granted"] < WARMUP:
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

    def _lease_memory(self, pid: int) -> tuple[int, int | None]:
        """(in use, kernel scope peak) of pid's process tree: the larger of its
        resident-plus-swapped footprint and its run scopes' unreclaimable use."""
        use = self.footprint(pid)
        scope = self.scope_memory(pid)
        if scope is None:
            return use, None
        return max(use, scope[0]), scope[1]

    def _sample_peaks(self, wall: float, mono: float, cpu: dict[str, float],
                      memory: dict[int, tuple[int, int | None]],
                      trees: dict[int, set[int]]) -> None:
        """Each lease's memory use (beside the available sample), its peaks
        and when its peak last rose by a ramp step, and its peak CPU use."""
        leases = self.state["leases"]
        pids = {x["pid"] for x in leases}
        raw = {pid: memory.get(pid, (0, None))[0] for pid in pids}
        own = {}
        for pid in pids:
            # A lease started inside another's tree is measured on its own:
            # take the outermost such trees out of the outer lease's use.
            inner = [q for q in pids if q != pid and q in trees.get(pid, ())]
            top = [q for q in inner if not any(r != q and q in trees.get(r, ()) for r in inner)]
            own[pid] = max(0, raw[pid] - sum(raw[q] for q in top))
        use: dict[str, int] = {}
        sharing: dict[int, list[dict]] = {}
        for x in leases:
            sharing.setdefault(x["pid"], []).append(x)
        for pid, group in sharing.items():
            budget = own[pid]  # leases on one process count its use once
            for i, x in enumerate(group):
                take = budget if i == len(group) - 1 else min(x["mem"], budget)
                use[x["id"]] = take
                budget -= take
        ids = set(use)
        for table in (self._mem_use, self._mem_peak, self._mem_step,
                      self._scope_peak, self._cores_peak, self._peak_own):
            for gone in set(table) - ids:
                del table[gone]
        for x in leases:
            lease_id = x["id"]
            self._mem_use[lease_id] = use[lease_id]
            peak = self._mem_peak[lease_id] = max(self._mem_peak.get(lease_id, 0), use[lease_id])
            step = self._mem_step.get(lease_id)
            if step is None or peak - step[0] > max(MEM_RAMP_STEP * x["mem"], MEM_RAMP_MIN):
                self._mem_step[lease_id] = (peak, wall)
            scope_peak = memory.get(x["pid"], (0, None))[1]
            if scope_peak is not None:
                self._scope_peak[lease_id] = max(self._scope_peak.get(lease_id, 0), scope_peak)
        previous, self._peak_cpu = self._peak_cpu, (mono, cpu)
        if previous is None or mono <= previous[0]:
            return
        for lease_id, seconds in cpu.items():
            if lease_id in previous[1] and lease_id in ids:
                rate = max(0.0, seconds - previous[1][lease_id]) / (mono - previous[0])
                before = self._peak_own.get(lease_id, 0.0)
                smoothed = self._peak_own[lease_id] = before + BUSY_SMOOTHING * (rate - before)
                self._cores_peak[lease_id] = max(self._cores_peak.get(lease_id, 0.0), smoothed)

    def tick(self) -> None:
        sample = self.measure()
        stamp = time.monotonic()
        self._refresh_tracking(stamp)
        sample_exclusive = stamp - self._exclusive_sample_at >= EXCLUSIVE_SAMPLE
        with self.lock:
            leases = [(x["id"], x["pid"]) for x in self.state["leases"]
                      if x["pid"] > 0 and "missing_since" not in x]
            targets = [(x["id"], x["pid"]) for x in self.state["leases"]
                       if x["pid"] > 0 and "missing_since" not in x
                       and (x.get("io") or self._exclusive_resources(x)
                            or (x["cores"] >= 2 and not x.get("io_hint")))]
            watch_gpu = sample_exclusive and any(
                x["exclusive"] or "gpu" in x.get("exclusive_locks", ()) for x in self.state["leases"]
            )
        now = time.monotonic()
        pids = {pid for _, pid in leases}
        by_pid = {pid: self.cpu_seconds(pid) for pid in pids}
        samples = {lease_id: by_pid[pid] for lease_id, pid in targets if by_pid[pid] is not None}
        every = {lease_id: by_pid[pid] for lease_id, pid in leases if by_pid[pid] is not None}
        trees = {pid: self.tree_pids(pid) for pid in pids} if len(pids) > 1 else {}
        memory = {pid: self._lease_memory(pid) for pid in pids}
        gpu = self.gpu_sample() if watch_gpu else None
        with self.lock:
            self._trees = {lease_id: trees[pid] for lease_id, pid in targets
                           if pid in trees and by_pid[pid] is not None}  # fmt: skip
            self._sample_lease_cpu(now, samples)
            self._sample_peaks(time.time(), now, every, memory, trees)
            self.vms = sample["vms"]
            self.available = sample["available"]
            # Smoothed over ~10 s: a moment's burst shouldn't hold the line.
            self.busy += BUSY_SMOOTHING * (sample["busy"] - self.busy)
            changed = self._prune()
            if sample_exclusive:
                self._exclusive_sample_at = stamp
                changed |= self._monitor_exclusive(time.time(), gpu)
            changed |= self._hint_io(now)
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
            if op in {"poll", "status", "phase"}:
                self._prune()
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
        if req.get("io"):
            lease["io"] = True
        if headroom:
            lease["headroom"] = headroom
        for key in ("pidfile", "unit", "run_unit"):
            if req.get(key):
                lease[key] = str(req[key])
        if (lease.get("pidfile") or lease.get("unit")) and not req.get("running"):
            return {"error": "PID files and units require an existing hold"}
        if unit := lease.get("run_unit"):
            for existing in self.state["queue"] + self.state["leases"]:
                if existing.get("run_unit") != unit:
                    continue
                if alive(existing["pid"]):
                    return {"error": f"unit {unit} already has a live lease"}
                if time.time() - existing.get("missing_since", time.time()) >= RESTART_GRACE:
                    self._prune()
                    break
                if not self._same_run(existing, lease):
                    return {"error": f"unit {unit} has a different pending registration; release it first"}
                for name in list(existing.get("phase_locks", {})):
                    self._phase_release(existing, name)
                existing["pid"] = lease["pid"]
                existing.pop("missing_since", None)
                if self._cpu_sample:
                    self._cpu_sample[1].pop(existing["id"], None)
                if self._peak_cpu:
                    self._peak_cpu[1].pop(existing["id"], None)
                self._log("resumed", existing)
                return {"id": existing["id"], **self._ticket(existing["id"])}
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
            if lease["pid"] <= 0:
                lease["missing_since"] = lease["granted"]
            self.state["leases"].append(lease)
            self._log("started", lease, wait=0.0, held=True)
        else:
            self.state["queue"].append(lease)
            self._log("requested", lease, reason=self._blocked(lease))
        self._admit()
        return {"id": lease["id"], **self._ticket(lease["id"])}

    def op_watch(self, req: dict, agent: str) -> dict:
        if not sys.platform.startswith("linux"):
            return {"error": "watcher registration requires Linux"}
        pid, owner, expect = req["pid"], req["owner_pid"], req["expect"]
        label = str(req["label"])
        if not label.startswith("watch:") or not label[6:].strip():
            return {"error": "watcher label must start with watch: and have a name"}
        if type(expect) not in (int, float) or not math.isfinite(expect) or expect <= 0:
            return {"error": "watcher needs a positive finite --expect (hard expiry)"}
        start, owner_start = process_start(pid), process_start(owner)
        if not start or not owner_start or pid == owner:
            return {"error": "watcher needs distinct, live watcher and owner PIDs"}
        if any(w["pid"] == pid and w["start"] == start for w in self.state["watchers"]):
            return {"error": f"watcher PID {pid} is already registered"}
        watch = {"id": f"W{self.state['next']}", "agent": agent, "label": label,
                 "pid": pid, "start": start, "owner_pid": owner, "owner_start": owner_start,
                 "expires": time.time() + expect, "expect": expect, "cores": 0, "mem": 0}
        self.state["next"] += 1
        self.state["watchers"].append(watch)
        self._log("watch_registered", watch)
        return {"id": watch["id"]}

    def _ticket(self, lease_id: str) -> dict:
        for x in self.state["leases"]:
            if x["id"] == lease_id:
                return {"state": "granted", **({"restarting": True} if "missing_since" in x else {})}
        for position, x in enumerate(self.state["queue"]):
            if x["id"] == lease_id:
                reason = self._blocked(x) or x.get("backfill_reason") or x.get("reason") or "waiting"
                if position:
                    reason = f"{position} request(s) ahead in line; {reason}"
                return {"state": "waiting", "reason": reason}
        return {"state": "gone"}

    def op_poll(self, req: dict, _agent: str) -> dict:
        return self._ticket(str(req.get("id")))

    def _phase_release(self, lease: dict, name: str) -> None:
        self._close_exclusive(lease, [name], time.time())
        lease["exclusive_locks"].remove(name)
        if not lease["phase_locks"].pop(name):
            lease["locks"].remove(name)
        lease.get("exclusive_warnings", {}).pop(f"idle:{name}", None)
        if not lease["exclusive_locks"]:
            lease.get("exclusive_warnings", {}).pop("overrun", None)

    def op_phase(self, req: dict, agent: str) -> dict:
        lease_id = req.get("id")
        lease = next((x for x in reversed(self.state["leases"])
                      if (x["id"] == lease_id and x["agent"] == agent if lease_id else
                          x["pid"] > 0 and req.get("pid") in self.tree_pids(x["pid"]))), None)
        if lease is None or "missing_since" in lease:
            return {"error": "no matching running lease (use its ID and owner with --as)"}
        name = str(req.get("name", "")).strip()
        if not name or exclusive_mode(lease) == "machine":
            return {"error": "phase requires a named lock on a non-machine-exclusive lease"}
        phases = lease.get("phase_locks", {})
        if req.get("release"):
            if name not in phases:
                return {"error": f"{name} is not a phase lock on {lease['id']}"}
            self._phase_release(lease, name)
            return {"id": lease["id"], "released": name}
        if name in phases:
            return {"id": lease["id"], "locked": name}
        if name in lease.get("exclusive_locks", ()):
            return {"error": f"{name} is already exclusive for the entire lease"}
        if holder := self._lock_holder(name, lease["id"]):
            return {"error": f"{name} is held by {holder}"}
        had_lock = name in lease["locks"]
        if not had_lock and any(name in x.get("locks", ()) for x in self.state["queue"]):
            return {"error": f"{name} is requested by a queued lease"}
        scoped = lease.setdefault("exclusive_locks", [])
        scoped.append(name)
        if self._usage()[0] > self.capacity()[0]:
            scoped.remove(name)
            return {"error": f"not enough free cores for an exclusive phase on {name}"}
        if not had_lock:
            lease["locks"].append(name)
        lease.setdefault("phase_locks", {})[name] = had_lock
        self._usage_record(lease, name)["since"] = time.time()
        self._log("exclusive-started", lease, resource=name)
        return {"id": lease["id"], "locked": name}

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

    def op_ping(self, _req: dict, _agent: str) -> dict:
        return {"pid": os.getpid()}

    def op_agents(self, _req: dict, _agent: str) -> dict:
        agents = sorted(self.state["agents"].items(),
                        key=lambda kv: -kv[1].get("last_seen", 0))  # fmt: skip
        return {"agents": [{"name": k, **v} for k, v in agents]}

    def op_status(self, _req: dict, _agent: str) -> dict:
        self._prune_watchers(time.time())
        cores, mem = self.capacity()
        used_cores, used_mem = self._usage()
        now = time.time()

        def measured(x: dict) -> dict:
            if x["id"] not in self._mem_use:
                return {}
            return {"mem_used": self._mem_use[x["id"]], "mem_peak": self._mem_peak[x["id"]],
                    "mem_growth": self._growth(x, now)}  # fmt: skip

        room = self._mem_room()
        return {
            "capacity": {"cores": cores, "mem": mem, "reserve": RESERVE_MEM},
            "free": {"cores": cores - used_cores, "mem": mem - used_mem},
            "measured_mem": None if room is None else {
                "used": sum(self._mem_use.get(x["id"], 0) for x in self.state["leases"]),
                "growth": sum(self._growth(x, now) for x in self.state["leases"]),
                "margin": int(MEM_MARGIN * self._mem_total()),
                "free": max(min(room), 0), "overcommit": MEM_OVERCOMMIT,
            },
            "leases": [{**x, "measured_cores": round(self._cpu_claim(x), 2),
                        "claimed_mem": self._mem_claim(x), **measured(x)} if x.get("io")
                       else {**x, **measured(x)}
                       for x in self.state["leases"]],
            "queue": [{**x, "reason": self._blocked(x) or x.get("backfill_reason") or x.get("reason") or "waiting"}
                      for x in self.state["queue"]],
            "vms": self._vms(), "load": load_average(),
            "locks": self.state["locks"],
            "watchers": self.state["watchers"],
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
    """Serve requests until stopped. On Windows socket_path is the endpoint
    file, and a second server for the same endpoint exits at once."""
    socket_path, state_file = socket_path or SOCKET, state_file or STATE_FILE
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = None
    if WINDOWS:
        instance = _claim_instance(socket_path)
        if instance is None:
            print(f"agent-coord: already serving {socket_path}", flush=True)
            return
    else:
        socket_path.unlink(missing_ok=True)
    coordinator = Coordinator(state_file, **coordinator_args)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline()
            try:
                req = json.loads(line)
                if WINDOWS and not hmac.compare_digest(
                        str(req.pop("token", "")), self.server.token):  # fmt: skip
                    reply = {"error": "bad token (is another service using this endpoint?)"}
                elif WINDOWS and req.get("op") == "shutdown":
                    reply = {"stopping": os.getpid()}
                    threading.Thread(target=self.server.shutdown).start()
                else:
                    reply = coordinator.handle(req)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                reply = {"error": f"bad request: {e}"}
            self.wfile.write(json.dumps(reply).encode() + b"\n")

    if WINDOWS:
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False  # SO_REUSEADDR would let others share the port

        address = ("127.0.0.1", 0)
    else:
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        address = str(socket_path)

    # Measure before answering anyone: admission needs real numbers, and the
    # smoothed CPU starts from a real reading rather than zero.
    first = coordinator.measure()
    coordinator.vms, coordinator.available = first["vms"], first["available"]
    coordinator.busy = first["busy"]
    with Server(address, Handler) as server:
        if WINDOWS:
            server.token = secrets.token_hex(16)
            _write_endpoint(socket_path, {"port": server.server_address[1],
                                          "token": server.token, "pid": os.getpid()})  # fmt: skip
        else:
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
        try:
            server.serve_forever()
        finally:
            if WINDOWS:
                _remove_endpoint(socket_path)
                _release_instance(instance)
                print("agent-coord: stopped", flush=True)


def _claim_instance(endpoint: Path):
    """An open file holding the endpoint's lock, or None if a server holds it."""
    handle = open(endpoint.with_name(endpoint.name + ".lock"), "a+b")  # noqa: SIM115
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def _release_instance(handle) -> None:
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()


def _write_endpoint(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _remove_endpoint(path: Path) -> None:
    """Remove the endpoint file if it still names this process."""
    try:
        if json.loads(path.read_text()).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


# ---------------------------------------------------------------- client

_DEFAULT_SOCKET = SOCKET
START_HINT = {
    "win32": "Start it: agent-coord service start (agent-coord service status shows its log)",
    "darwin": "Start it: launchctl kickstart gui/$(id -u)/com.theontho.agent-coord",
}.get(sys.platform, "Start it: systemctl --user start agent-coord")


def call(request: dict, socket_path: Path | None = None, retry: float = 0,
         autostart: bool = True) -> dict:
    """Send request; retry for up to retry seconds while the service restarts.
    On Windows the default service is started on demand."""
    deadline = time.monotonic() + retry
    started = False
    while True:
        try:
            return _call(request, socket_path or SOCKET)
        except _ServiceError:
            raise
        except SystemExit:
            if (WINDOWS and autostart and not started and socket_path is None
                    and SOCKET == _DEFAULT_SOCKET):  # fmt: skip
                started = True
                start_service()
                deadline = max(deadline, time.monotonic() + 20)
            elif time.monotonic() >= deadline:
                raise
            time.sleep(0.5 if started else 1)


def _call(request: dict, socket_path: Path) -> dict:
    try:
        if WINDOWS:
            endpoint = json.loads(socket_path.read_text())
            request = {**request, "token": endpoint["token"]}
            sock = socket.create_connection(("127.0.0.1", int(endpoint["port"])), timeout=30)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect(str(socket_path))
        with sock:
            sock.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"agent-coord: the service isn't reachable ({e}). {START_HINT}") from e
    try:
        reply = json.loads(data)
    except ValueError as e:  # cut off mid-reply, e.g. by a service restart
        raise SystemExit(f"agent-coord: no answer from the service ({e})") from e
    if "error" in reply:
        raise _ServiceError(f"agent-coord: {reply['error']}")
    return reply


class _ServiceError(SystemExit):
    """The service answered with an error (not worth retrying)."""


# ---------------------------------------------------------------- Windows service

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SERVICE_LOG = RUNTIME / "service.log"


def _service_command() -> list[str]:
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return [str(windowless if windowless.exists() else exe), os.path.realpath(__file__), "serve"]


def start_service() -> None:
    """Start the service detached from this console and, where the job
    allows it, from the calling job (an agent's shell)."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        if SERVICE_LOG.stat().st_size > 1024**2:
            SERVICE_LOG.replace(SERVICE_LOG.with_name(SERVICE_LOG.name + ".1"))
    except OSError:
        pass
    with SERVICE_LOG.open("ab") as log:
        _spawn_windows_background(_service_command(), RUNTIME, log)


def _spawn_windows_background(command: list[str], cwd: Path | str, log) -> None:
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    flags = 0x00000008 | 0x00000200 | 0x08000000
    for breakaway in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB, if allowed
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             cwd=str(cwd), close_fds=True, creationflags=flags | breakaway)
            return
        except OSError:
            if not breakaway:
                raise


def _service_pid() -> int | None:
    try:
        return int(call({"op": "ping"}, autostart=False)["pid"])
    except SystemExit:
        return None


def _wait(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.3)
    return predicate()


def _stop_service() -> bool:
    """Stop the service; True if it was running."""
    pid = _service_pid()
    if pid is None:
        return False
    try:
        call({"op": "shutdown"}, autostart=False)
    except SystemExit:
        pass
    if not _wait(lambda: not alive(pid), 15):
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        _wait(lambda: not alive(pid), 5)
    return True


def _logon_command() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            return winreg.QueryValueEx(key, "agent-coord")[0]
    except OSError:
        return None


def cmd_service(args: argparse.Namespace) -> int:
    action = args.service_action
    if not WINDOWS:
        raise SystemExit("agent-coord service is for Windows; here use "
                         f"systemctl --user {action} agent-coord")  # fmt: skip
    if action == "install":
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, "agent-coord", 0, winreg.REG_SZ,
                              subprocess.list2cmdline(_service_command()))  # fmt: skip
        print("agent-coord: starts at logon (HKCU Run key)")
        action = "start"
    if action == "uninstall":
        _stop_service()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:  # fmt: skip
                winreg.DeleteValue(key, "agent-coord")
        except OSError:
            pass
        print("agent-coord: stopped; no longer starts at logon")
        return 0
    if action in ("stop", "restart"):
        print("agent-coord: stopped" if _stop_service() else "agent-coord: was not running")
        if action == "stop":
            return 0
    if action in ("start", "restart"):
        if (pid := _service_pid()) is None:
            start_service()
            _wait(lambda: _service_pid() is not None, 20)
            pid = _service_pid()
        if pid is None:
            raise SystemExit(f"agent-coord: the service did not start; see {SERVICE_LOG}")
        print(f"agent-coord: running (pid {pid})")
        return 0
    pid = _service_pid()
    print(f"service:  {f'running (pid {pid})' if pid else 'not running'}")
    print(f"logon:    {_logon_command() or 'not installed (starts on demand)'}")
    print(f"endpoint: {SOCKET}\nstate:    {STATE_FILE}\nlog:      {SERVICE_LOG}")
    return 0 if pid else 3


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
    if DARWIN:
        return str(next(point for point in (full, *full.parents) if point.is_mount()))
    if WINDOWS:
        buf = ctypes.create_unicode_buffer(1024)
        if _GetVolumePathNameW(str(full), buf, len(buf)):
            return buf.value
        return full.anchor or str(full)
    best = "/"
    for line in Path("/proc/mounts").read_text().splitlines():
        point = line.split()[1].replace("\\040", " ")
        if (str(full) == point or str(full).startswith(point.rstrip("/") + "/")) and len(
            point
        ) > len(best):
            best = point
    return best


def disk_free(mount: str) -> int:
    if WINDOWS:
        return shutil.disk_usage(mount).free  # free to this user, like f_bavail
    st = os.statvfs(mount)
    return st.f_bavail * st.f_frsize


# Options naming where a command works or writes (output, temp, work dirs).
_OUTPUT_OPTIONS = {"-o", "-O", "--output", "--out", "--outdir", "--output-dir",
                   "--output_vcf", "--out-bam", "--out-variants", "-T", "--tmp-dir",
                   "--TMP_DIR", "--temp-dir", "--intermediate_results_dir", "-d",
                   "--directory", "-w", "--workdir"}  # fmt: skip


def archive_mounts() -> list[str]:
    """Mount points of the HDD archive pools (ZFS): reference archives only;
    active work (alignment, sorting, calling) belongs on the SSDs.
    AGENT_COORD_ARCHIVE (paths separated by os.pathsep) adds more, e.g. a
    NAS share on Windows, which has no /proc/mounts."""
    mounts = [p for p in os.environ.get("AGENT_COORD_ARCHIVE", "").split(os.pathsep) if p]
    if WINDOWS:
        return [os.path.realpath(os.path.expanduser(m)) for m in mounts]
    if DARWIN:
        return mounts + [p.mountpoint for p in psutil.disk_partitions() if p.fstype == "zfs"]
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[2] == "zfs":
            mounts.append(fields[1].replace("\\040", " "))
    return mounts


def _on_archive(path: str, mounts: list[str]) -> str | None:
    full = os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    for m in mounts:
        root = os.path.normcase(os.path.realpath(m))
        if full == root or full.startswith(root.rstrip(os.sep) + os.sep):
            return m
    return None


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


def _low_priority(cmd: list[str]) -> list[str]:
    if DARWIN:
        return ["nice", "-n", "19", *cmd]
    return ["nice", "-n", "19", "ionice", "-c", "3", *cmd]


def _systemd_limits(cores: float, mem: int) -> list[str]:
    return ["-p", f"CPUQuota={max(int(cores * 100), 1)}%", "-p", f"MemoryMax={mem}",
            "-p", "MemorySwapMax=0"]


def limited(cmd: list[str], cores: float, mem: int, unit: str | None = None) -> list[str]:
    """Run at low priority; name Linux scopes so the service can measure them."""
    low = _low_priority(cmd)
    if DARWIN:
        return low
    bus = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "bus")
    if not shutil.which("systemd-run") or not bus.exists():
        print("agent-coord: no systemd user session; running uncapped at low "
              "priority", file=sys.stderr)  # fmt: skip
        return low
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        *([f"--unit={unit}"] if unit else []),
        *_systemd_limits(cores, mem), "--", *low,
    ]  # fmt: skip


def _job_for(cores: float, mem: int):
    """A Windows job object capping its processes at cores (hard CPU rate
    cap) and mem (job commit limit) at idle priority, killed when the last
    handle (this client's) closes: the counterpart of the systemd scope."""
    job = _CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    # PRIORITY_CLASS | JOB_MEMORY | KILL_ON_JOB_CLOSE
    limits.BasicLimitInformation.LimitFlags = 0x20 | 0x200 | 0x2000
    limits.BasicLimitInformation.PriorityClass = 0x40  # IDLE_PRIORITY_CLASS ~ nice 19
    limits.JobMemoryLimit = max(int(mem), 16 * 1024**2)
    if not _SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    total = os.cpu_count() or 1
    if cores < total:
        rate = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
            ControlFlags=0x1 | 0x4,  # ENABLE | HARD_CAP
            CpuRate=min(max(int(cores / total * 10000), 1), 10000))  # fmt: skip
        if not _SetInformationJobObject(job, 15, ctypes.byref(rate), ctypes.sizeof(rate)):
            print(f"agent-coord: no CPU rate cap ({ctypes.WinError(ctypes.get_last_error())})",
                  file=sys.stderr)  # fmt: skip
    return job


def spawn_limited(cmd: list[str], cores: float, mem: int, env: dict, *,
                  unit: str | None = None, stdout=None, stderr=None):
    """(Popen, keep-alive handle) for cmd capped at cores and mem."""
    if not WINDOWS:
        return subprocess.Popen(limited(cmd, cores, mem, unit), env=env,
                                stdout=stdout, stderr=stderr), None
    # CreateProcess finds only .exe on PATH itself: resolve .cmd/.bat etc.
    # through PATHEXT, as a shell would.
    exe = shutil.which(cmd[0], path=env.get("PATH"))
    cmd = [exe or cmd[0], *cmd[1:]]
    try:
        job = _job_for(cores, mem)
    except OSError as e:
        print(f"agent-coord: no job object ({e}); running uncapped at low priority",
              file=sys.stderr)  # fmt: skip
        return subprocess.Popen(cmd, env=env, stdout=stdout, stderr=stderr, creationflags=0x40), None
    # Start suspended, so nothing the command starts escapes the job.
    child = subprocess.Popen(cmd, env=env, stdout=stdout, stderr=stderr,
                             creationflags=0x4)  # CREATE_SUSPENDED
    if not _AssignProcessToJobObject(job, int(child._handle)):  # noqa: SLF001
        print(f"agent-coord: not capped ({ctypes.WinError(ctypes.get_last_error())})",
              file=sys.stderr)  # fmt: skip
    _NtResumeProcess(int(child._handle))  # noqa: SLF001
    return child, job


def parse_disk(text: str) -> tuple[str, int]:
    path, sep, size = text.rpartition(":")
    if not sep or not path:
        raise argparse.ArgumentTypeError(f"not PATH:SIZE: {text} (e.g. /home:150G)")
    return path, parse_size(size)


def _lease_request(args: argparse.Namespace, pid: int) -> dict:
    return {"op": "acquire", "agent": args.agent, "cwd": os.getcwd(), "pid": pid,
            "cores": args.cores, "mem": args.mem, "label": args.label, "io": args.io,
            "exclusive": args.exclusive, "locks": args.lock or [],
            "exclusive_locks": args.exclusive_lock or [], "headroom": args.headroom,
            "expect": args.expect, "disk": dict(args.disk or [])}  # fmt: skip


def detached_run(args: argparse.Namespace, command: list[str]) -> int:
    unit = unit_name(args.unit)
    worker = [sys.executable, str(Path(__file__).resolve()), f"--as={args.agent}", "run",
              f"--label={args.label}", f"--cores={args.cores}", f"--mem={args.mem}",
              f"--headroom={args.headroom}", f"--managed-unit={unit}"]
    for name in ("io", "exclusive", "allow_archive"):
        if getattr(args, name):
            worker.append("--" + name.replace("_", "-"))
    for name in ("expect", "max_wait"):
        if (value := getattr(args, name)) is not None:
            worker.append(f"--{name.replace('_', '-')}={value:f}s")
    for name in ("lock", "exclusive_lock", "disk"):
        for value in getattr(args, name) or ():
            if name == "disk":
                value = f"{Path(value[0]).expanduser().resolve()}:{value[1]}"
            worker.append(f"--{name.replace('_', '-')}={value}")
    worker.extend(["--", *command])
    cwd = os.getcwd()
    overrides = {key: os.environ[key] for key in
                 ("AGENT_COORD_RUNTIME_DIR", "AGENT_COORD_STATE_DIR") if key in os.environ}
    try:
        if sys.platform.startswith("linux"):
            supports_literal = "--expand-environment" in subprocess.check_output(
                ["systemd-run", "--help"], text=True, timeout=5
            )
            cmd = ["systemd-run", "--user", "--collect", f"--unit={unit}",
                   "--property=Type=exec", "--property=KillMode=mixed", f"--working-directory={cwd}",
                   *_systemd_limits(args.cores, args.mem)]
            if supports_literal:
                cmd.append("--expand-environment=no")
            else:
                worker = [arg.replace("$", "$$") for arg in worker]
            cmd.extend(f"--setenv={key}={value}" for key, value in overrides.items())
            subprocess.run([*cmd, "--", *worker], check=True, timeout=20)
        else:
            directory = STATE_FILE.parent / "units"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            log = directory / f"{unit}.log"
            if sys.platform == "darwin":
                plist = directory / f"{unit}.{os.getpid()}.plist"
                domain = f"gui/{os.getuid()}"
                plist.touch(mode=0o600, exist_ok=False)
                try:
                    with plist.open("wb") as f:
                        plistlib.dump({"Label": unit, "ProgramArguments": worker, "WorkingDirectory": cwd,
                                       "StandardOutPath": str(log), "StandardErrorPath": str(log),
                                       "EnvironmentVariables": overrides, "RunAtLoad": True}, f)
                    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=15)
                finally:
                    plist.unlink()
            elif sys.platform == "win32":
                with log.open("ab") as f:
                    _spawn_windows_background(worker, cwd, f)
            else:
                raise ValueError("detached runs are unsupported on this platform")
            print(f"log: {log}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise SystemExit(f"agent-coord: could not start detached unit {unit}: {e}") from e
    print(f"detached: {unit}")
    return 0


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
    if args.detach:
        return detached_run(args, command)
    pid = os.getpid()
    in_systemd = args.managed_unit and sys.platform.startswith("linux")
    if in_systemd and unit_pid(args.managed_unit) != pid:
        raise SystemExit("agent-coord: managed run must be the named systemd service's main process")
    reply = call({**_lease_request(args, pid), "run_unit": args.managed_unit})
    lease_id = reply["id"]
    child = None
    end: dict = {}
    stopping_unit = False

    def forward(signum: int, _frame: object) -> None:
        nonlocal stopping_unit
        stopping_unit = bool(args.managed_unit and signum == signal.SIGTERM)
        if child is not None and child.poll() is None:
            if not WINDOWS:
                child.send_signal(signum)
            elif signum == signal.SIGTERM:
                child.terminate()
            # Windows: Ctrl+C/Ctrl+Break reach the child through the console.
        else:
            raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), forward)
    try:
        _wait_for(lease_id, reply, args.max_wait)
        print(f"agent-coord: {lease_id} started ({args.cores:g} cores, "
              f"{human(args.mem)})", file=sys.stderr, flush=True)  # fmt: skip
        env = dict(os.environ, COMPUTE_LEASE_CORES=str(max(int(args.cores), 1)))
        # JVMs size their heap from the host's RAM, not the scope's limit, and
        # get killed at it: tell them the lease's memory (heap up to 60% of it).
        java = f"-XX:MaxRAM={args.mem} -XX:MaxRAMPercentage=60"
        env["JAVA_TOOL_OPTIONS"] = f"{env.get('JAVA_TOOL_OPTIONS', '')} {java}".strip()
        if in_systemd:
            child = subprocess.Popen(_low_priority(command), env=env)
        elif WINDOWS and args.managed_unit:
            child, _job = spawn_limited(command, args.cores, args.mem, env,
                                        stdout=sys.stdout, stderr=sys.stderr)
        else:
            child, _job = spawn_limited(command, args.cores, args.mem, env,
                                        unit=f"{SCOPE_PREFIX}{lease_id}-{pid}")
        code = child.wait()
        if code == -getattr(signal, "SIGKILL", 9):
            print(f"agent-coord: {lease_id} was killed; if it used more than "
                  f"--mem {human(args.mem)}, lease more", file=sys.stderr)  # fmt: skip
        end["code"] = 128 - code if code < 0 else code
        return end["code"]
    except KeyboardInterrupt:
        end.update(code=130, why="interrupted")
        return 130
    finally:
        if not stopping_unit:
            try:
                call({"op": "release", "id": lease_id, **end}, retry=120)
            except SystemExit:
                pass


def cmd_hold(args: argparse.Namespace) -> int:
    if args.owner_pid is not None:
        reply = call({"op": "watch", "agent": args.agent, "cwd": os.getcwd(),
                      "pid": args.pid, "owner_pid": args.owner_pid,
                      "label": args.label, "expect": args.expect})
        print(reply["id"])
        return 0
    unit = unit_name(args.unit) if args.unit else None
    pidfile = str(Path(args.pidfile).expanduser().resolve()) if args.pidfile else None
    if unit and sys.platform not in ("darwin", "linux"):
        raise SystemExit("agent-coord hold: use --pidfile on this platform")
    candidates = (unit_pid(unit) if unit else 0, read_pidfile(pidfile) if pidfile else 0, args.pid or 0)
    pid = next((p for p in candidates if p > 0 and alive(p)), 0)
    if not pid and not (unit or pidfile):
        raise SystemExit(f"agent-coord hold: no process {args.pid}")
    reply = call({**_lease_request(args, pid), "running": True, "unit": unit, "pidfile": pidfile})
    print(reply["id"])
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    result = call({"op": "poll", "id": args.id})
    print(json.dumps(result) if args.json else "restarting" if result.get("restarting") else result["state"])
    return int(result["state"] == "gone")


def cmd_phase(args: argparse.Namespace) -> int:
    reply = call({"op": "phase", "id": args.id, "pid": os.getpid(), "agent": args.agent,
                  "name": args.exclusive_lock or args.release, "release": bool(args.release)})
    print(reply["id"])
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    reply = call({"op": "check", "cores": args.cores, "mem": args.mem, "io": args.io,
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
    load = " ".join(f"{x:.1f}" for x in s["load"]) if s.get("load") else None
    print(f"{cap['cores']:g} cores, {human(cap['mem'])} for leases "
          f"({human(cap['reserve'])} kept back); free {free['cores']:.1f} cores, "
          f"{human(free['mem'])}" + (f"; load {load}" if load else ""))  # fmt: skip
    if s.get("available") is not None:
        print(f"memory actually available now: {human(s['available'])} "
              f"(leases need theirs plus {human(cap['reserve'])} of it)")
    if m := s.get("measured_mem"):
        print(f"memory by measured use: leases use {human(m['used'])} of "
              f"{human(cap['mem'] - free['mem'])} reserved, may grow {human(m['growth'])}; "
              f"free {human(m['free'])} after that, the reserve and a "
              f"{human(m['margin'])} margin (sizes may total {m['overcommit']:g}x)")
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
        if unit := x.get("unit") or x.get("run_unit"):
            flag += f" unit {unit}"
        detail = x.get("reason") if state == "waiting" else x.get("overrun_reason") or x.get("backfill_reason")
        why = f" -- {detail}" if detail else ""
        cpu = f"{x['cores']:>4g} cores"
        mem = x.get("claimed_mem", x["mem"])
        if "mem_used" in x:
            flag += f" using {human(x['mem_used'])} (peak {human(x['mem_peak'])})"
        if x.get("io"):
            flag += " io"
            cpu = f"{x['cores']:g}-core cap"
            if state == "held":
                cpu += f", {x['measured_cores']:g} used"
        tracking = " restarting" if "missing_since" in x else ""
        print(f"  {x['id']:<6} {state:<7} {cpu} {human(mem):>6} "
              f"{since:5.0f} min  {x['agent']}: {x['label']}{flag} (pid {x['pid']}){tracking}{why}")
        for warning in x.get("exclusive_warnings", {}).values():
            print(f"    warning: {warning}")
    if "busy_cores" in s:
        print(f"cores busy now (measured): {s['busy_cores']:g} of {cap['cores']:g}; "
              "CPU admission uses this; memory admission uses lease sizes, or "
              "measured use plus growth allowances when those do not fit")
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
    for w in s.get("watchers", []):
        remaining = max(0, w["expires"] - time.time())
        expiry = "reaping" if "kill_after" in w else f"expires in {remaining:.0f}s"
        print(f"  {w['id']:<6} watch   {w['agent']}: {w['label']} "
              f"(pid {w['pid']}, owner pid {w['owner_pid']}, {expiry})")
    for g in s["vms"]:
        print(f"  vm     running {g['cores']:>4g} cores {human(g['mem']):>6}"
              f"            {g['name'] or 'qemu'} (pid {g['pid']}, measured)")
    if not rows and not s["vms"] and not s.get("watchers"):
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


def peaks(events: list[dict], prefix: str = "", last: int = 10) -> dict[str, dict]:
    """Per label starting with prefix: the measured peaks of its last runs
    (ended leases with a memory sample, newest last), and --mem suggested as
    the largest of those memory peaks plus PEAK_SIZING_MARGIN."""
    runs: dict[str, list[dict]] = {}
    for e in events:
        if e.get("event") == "ended" and "mem_peak" in e and str(e.get("label", "")).startswith(prefix):
            runs.setdefault(e["label"], []).append(e)
    out = {}
    for label, ended in sorted(runs.items()):
        ended = ended[-last:]
        mem = [e["mem_peak"] for e in ended]
        cores = [e["cores_peak"] for e in ended if "cores_peak" in e]
        scope = [e["mem_peak_scope"] for e in ended if "mem_peak_scope" in e]
        out[label] = {
            "runs": len(ended), "ok": sum(1 for e in ended if e.get("code") == 0),
            "mem_peak_max": max(mem), "mem_peak_last": mem[-1],
            "mem_asked_last": ended[-1].get("mem"),
            "suggest_mem": math.ceil(max(mem) * (1 + PEAK_SIZING_MARGIN)),
            **({"cores_peak_max": max(cores)} if cores else {}),
            **({"mem_peak_scope_max": max(scope)} if scope else {}),
            "last_time": ended[-1].get("time"),
        }  # fmt: skip
    return out


def cmd_history(args: argparse.Namespace) -> int:
    if args.peaks is not None:
        since = args.since if args.since is not None else 30 * 24 * 3600.0
        found = peaks(read_history(time.time() - since), args.peaks, args.last)
        if args.json:
            print(json.dumps(found, indent=2))
            return 0
        for label, p in found.items():
            cores = f", {p['cores_peak_max']:g} cores" if "cores_peak_max" in p else ""
            scope = (f" (scope {human(p['mem_peak_scope_max'])} with cache)"
                     if "mem_peak_scope_max" in p else "")  # fmt: skip
            print(f"{label}: {p['runs']} run(s), peak {human(p['mem_peak_max'])}{scope}{cores}; "
                  f"asked {human(p['mem_asked_last'] or 0)}; "
                  f"suggest --mem {human(p['suggest_mem'])}")
        if not found:
            print("no measured runs for that label prefix")
        return 0
    events = read_history(time.time() - (args.since if args.since is not None else 24 * 3600.0))
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
        if "mem_peak" in e or "cores_peak" in e:
            peak = [human(e["mem_peak"])] if "mem_peak" in e else []
            if "cores_peak" in e:
                peak.append(f"{e['cores_peak']:g} cores")
            times.append("peak " + ", ".join(peak))
        if "exclusive_seconds" in e:
            times.extend(f"{key} {_minutes(e[key + '_seconds'])}"
                         for key in ("exclusive", "observed", "active"))
        detail = e.get("reason") or e.get("why") or e.get("backfill_reason") or ""
        if "resource" in e:
            detail = " ".join(x for x in (e["resource"], detail) if x)
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
        sub.add_argument("--cores", type=float)
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
            sub.add_argument("--pid", type=int)
            sub.add_argument("--owner-pid", type=int, help="register a watch: label with a hard --expect expiry")
            sub.add_argument("--pidfile", type=Path)
            sub.add_argument("--unit", type=unit_name)
        else:
            sub.add_argument("--allow-archive", action="store_true")
            sub.add_argument("--detach", action="store_true")
            sub.add_argument("--unit", type=unit_name)
            sub.add_argument("--managed-unit", help=argparse.SUPPRESS)
            sub.add_argument("command", nargs=argparse.REMAINDER)
    subs.add_parser("status").add_argument("--json", action="store_true")
    poll = subs.add_parser("poll")
    poll.add_argument("id")
    poll.add_argument("--json", action="store_true")
    phase = subs.add_parser("phase")
    phase.add_argument("id", nargs="?")
    choice = phase.add_mutually_exclusive_group(required=True)
    choice.add_argument("--exclusive-lock", metavar="NAME")
    choice.add_argument("--release", metavar="NAME")
    history = subs.add_parser("history")
    history.add_argument("--since", type=parse_duration)
    history.add_argument("--json", action="store_true")
    history.add_argument("--peaks", nargs="?", const="", metavar="LABEL-PREFIX")
    history.add_argument("--last", type=int, default=10)
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
    subs.add_parser("service").add_argument(
        "service_action", choices=["install", "uninstall", "start", "stop", "restart", "status"])
    args = parser.parse_args(argv)
    if args.action == "hold" and args.owner_pid is not None:
        if args.pid is None or args.pidfile is not None or args.unit:
            parser.error("watchers require --pid and cannot follow --pidfile or --unit")
        if (args.cores is not None or args.mem is not None or args.io or args.exclusive
                or args.lock or args.exclusive_lock or args.disk or args.headroom
                or args.max_wait is not None):
            parser.error("watchers do not take compute lease options")
    elif args.action in ("run", "hold", "check"):
        if not args.io and (args.cores is None or args.mem is None):
            parser.error("--cores and --mem are required without --io")
        if args.cores is None:
            args.cores = 1.0
        if args.mem is None:
            args.mem = IO_DEFAULT_MEM
    args.agent = args.agent or default_agent()
    if getattr(args, "exclusive", False) and getattr(args, "exclusive_lock", None):
        parser.error("--exclusive already excludes everything; drop --exclusive-lock")
    if args.action == "hold" and not (args.pid or args.pidfile or args.unit):
        parser.error("hold requires --pid, --pidfile or --unit")
    if args.action == "run" and args.detach != bool(args.unit):
        parser.error("run --detach and --unit NAME must be used together")
    if args.action == "serve":
        serve()
        return 0
    return {
        "run": cmd_run, "hold": cmd_hold, "check": cmd_check, "status": cmd_status,
        "history": cmd_history,
        "poll": cmd_poll, "phase": cmd_phase,
        "send": cmd_send, "inbox": cmd_inbox, "agents": cmd_agents,
        "release": cmd_release, "lock": cmd_lock, "unlock": cmd_unlock,
        "service": cmd_service,
    }[args.action](args)  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
