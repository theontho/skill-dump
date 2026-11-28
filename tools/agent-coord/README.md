# agent-coord

A small local service for the agents (and people) sharing this computer. It
gives them one live place to:

- **queue heavy jobs** for CPU cores and RAM, so jobs wait their turn instead
  of piling onto a host that is already swapping;
- **check** whether a job of a given size may start now;
- **message** each other (one agent, or `all`).

On Linux it is a user systemd service (`agent-coord.service`) speaking JSON
lines on `$XDG_RUNTIME_DIR/agent-coord/sock`. On macOS the LaunchAgent
`com.theontho.agent-coord` listens on `~/.agent-coord/sock`. Both keep state in
`~/.local/state/agent-coord/state.json`. On [Windows](#windows) it runs as a
background process using an authenticated loopback endpoint. The state survives restarts: held
leases and waiting requests are kept, and waiting `run` clients retry their
polls for two minutes while the service restarts. The CLI
is the same file, installed as `~/.local/bin/agent-coord` by `skill-dump install`.

On macOS, give the LaunchAgent's Python 3 interpreter `psutil`, then log in
again or load the installed plist:

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.theontho.agent-coord.plist
```

macOS applies scheduling reservations and low priority, not Linux-style hard
CPU and RAM caps.

On macOS and Windows, CPU accounting uses the live process tree. Time from
children that exit between samples may not be attributed to the lease. Linux
also counts CPU time from reaped children.

## Use

```sh
agent-coord status                       # who holds what, the line, what is free
agent-coord check --cores 4 --mem 8G     # exit 0 if it would start now

# Heavy job: waits in line, then runs capped at 4 cores / 8G, nice 19, idle IO.
agent-coord run --cores 4 --mem 8G --label "ultima chr20 realign" -- \
    wgsextract realign --threads 4 ...

# Download: capped at 1 core and 512M by default; override either size if needed.
agent-coord run --io --label "reference download" -- curl -fLO https://example.org/reference

# GPU timing run: exclusive on the GPU only, with its 6 cores guaranteed and
# 2 more kept unclaimed; CPU-only leases still run in the rest.
agent-coord run --cores 6 --mem 40G --exclusive-lock gpu --headroom 2 --label "gpu timing" -- ...

# Whole-machine timed run: waits for every other lease to end, then holds new ones back.
agent-coord run --cores 6 --mem 12G --exclusive --label "windows timing" -- ...

# Lease for a job already started (until its PID exits).
agent-coord hold --cores 6 --mem 10G --label "Windows VM full SNP" --pid 3442633

# Follow an unattended driver across restarts by its service or PID file.
agent-coord hold --io --label "existing driver" --pidfile ./out/driver.pid --expect 8h
agent-coord hold --io --label "existing service" --unit driver.service --expect 8h
agent-coord poll L42 --json

# Launch under the OS service manager so it survives closing this session.
agent-coord run --detach --unit benchmark --cores 4 --mem 8G \
    --label "mixed benchmark" --expect 8h -- ./benchmark.sh

# Reserve disk for the job's peak (temp + outputs); waits until it's free.
agent-coord run --cores 4 --mem 8G --disk /home:150G --label "30x realign" -- ...

# Named resources: a VM, the GPU, the NAS link... (until unlock, PID exit or TTL)
agent-coord lock windows-vm --reason "full SNP correctness until Tue AM" --pid 3442633
agent-coord run --lock gpu --cores 2 --mem 8G --label "parabricks" -- ...
agent-coord unlock windows-vm

agent-coord send --to worktree-ultima "done with the VM at 15:00"
agent-coord send --to all "NAS scrub starting; IO will be slow"
agent-coord inbox [--wait 10m]           # unread messages, then marked read
agent-coord agents                       # who has been seen
agent-coord history --since 6h           # lease events with wait/run times (--json)
```

Agents are named after their git worktree's folder (`worktree-ultima`), or
`--as NAME`.

### Unattended drivers

`hold` can track a systemd user service on Linux or a launchd GUI label on
macOS. On Windows use `--pidfile` for an existing driver. If you give both
`--unit` and `--pidfile`, a live unit PID takes
precedence. The PID file must hold one PID and should be replaced atomically
on restart; remove it when the driver stops. `--pid` alone keeps the original
process-exit behavior. With a tracking source it only supplies an initial PID.
Tracked holds remain active for a two-minute restart
grace period, retaining the same lease ID with `restarting: true` in
`poll ID --json`. `poll` exits 1 only when the lease is gone; treat a service
connection error separately from a successful JSON reply of `{"state":"gone"}`.

`run --detach` submits the normal queued run to a named user service or
LaunchAgent, or a named background process on Windows. The launcher returns
on submission, not job success. On Linux/macOS the child inherits the service
manager's environment, not the terminal's;
use a driver script or absolute executable paths as needed. On Linux, use
`journalctl --user -u benchmark.service` for output and `systemctl --user stop
benchmark.service` to stop it. On macOS, the launch command prints the log
path; use `launchctl bootout "gui/$(id -u)/benchmark"` before reusing the label.
On Windows the printed log path and lease name identify the detached process.
The lease is visible in `status --json` as `run_unit` while waiting or held.
If the managed process is stopped and restarted within the grace period, it
reattaches to the original lease. Use a driver that reports its own exit
status or failure to its watcher.

### Exclusive phases

In a leased driver, run `agent-coord phase --exclusive-lock gpu` just before
the measured GPU phase, and `agent-coord phase --release gpu` when it ends
(including on the driver's failure path). From outside the driver, pass the
lease ID as `phase ID ...` and use the lease owner's `--as NAME` if needed.
Acquisition fails immediately if the resource or cores are unavailable; it
does not wait while the driver holds other reservations. Phase release frees
just the named exclusivity. Lease exit also frees phase locks. Use
`--exclusive-lock` on `run` for a lock that must cover the whole command.

Exclusive holders receive an inbox and status warning after 15 minutes of
near-zero sampled use (GPU device utilization from `nvidia-smi` where
available, or holder CPU for whole-machine leases) or after exceeding an
explicit `--expect` by more than 25%. Unknown utilization is not evidence of
idleness. Small unrelated work can backfill during measured idle periods
subject to queue order, measured capacity, memory and headroom. It never
receives a held lock. This is not preemption: any backfill already admitted
can finish if the holder resumes. Release the phase lock rather than relying
on idle backfill.

## How admission works

- Capacity is all cores, and RAM minus 4 GB kept for the desktop and the agents
  themselves.
- **CPU is admitted on measured use**: host-wide busy cores (smoothed over ~10 s),
  plus each non-IO lease started in the last minute at its full size while it ramps up.
  An `--exclusive-lock` lease counts once: its own measured use, up to its
  size (smoothed like the busy figure, from zero, and counted once when one
  lease runs inside another's process tree), is taken out of the busy cores,
  so only the rest of the load counts beside its full size. Use above its size
  (a job in its tree but in a scope of its own, an uncapped hold) still counts
  as load.
  Leases usually use less than they ask, and sharing CPU only slows jobs a
  little, since each is capped at its size at nice 19. CPU should therefore stay
  busy, not idle behind reservations. Time spent waiting on disk is not CPU use.
- **`--io`** on `run`, `hold`, and `check` marks downloads, uploads, and disk
  streaming. CPU counts at measured process-tree use, up to `--cores`, even
  during startup, headroom checks, and backfill. Before the first sample it
  counts as zero. With `--io`, `--cores` defaults to 1 and `--mem` to 512M;
  both are still caps for `run`. Use `--lock wan` if large transfers should
  take turns; `--io` does not take a lock on its own. `status --json` shows
  `io`, `measured_cores`, and `claimed_mem` for running IO leases.
- **Memory is admitted on reservations, or on measured use when they are
  full.** A request starts if either rule lets it:
  - *By reservation* (as always): every lease at its requested size (or its
    measured tree footprint if larger for `--io`), plus running QEMU guests
    (resident + swapped) that no lease holds, fits the lease capacity, and the
    request's memory is *actually available* now (MemAvailable minus the 4 GB),
    so nothing is admitted into swap.
  - *By measured use*: memory actually available (at most RAM), less the 4 GB
    reserve, less a margin of 10% of RAM, less a **growth allowance** for every
    running lease, covers the request at its full size; and all leases' sizes
    together (plus unleased VMs) stay within 1.5 times the lease capacity.
    A lease's allowance is what it may still take beyond its use at the last
    sample: back up to its peak so far in full, then, of the rest of its size,
    all of it while the lease is young (under 10 minutes), unsampled, or still
    ramping up (its peak rose by 5% of its size, at least 256 MB, in the last
    10 minutes), and otherwise half of it but at least 1 GB. Never more than
    its size less its use: the hard cap stops it there.
    **Label history:** once a lease has been sampled, if its label has at
    least 3 successful runs with measured peaks (this agent's, else any
    agent's), it is expected to grow to at most the largest of the last 10
    peaks plus 15%, young or not, while it stays at or below that; a lease
    that goes past it is counted as above. `AGENT_COORD_MEM_HISTORY=0` turns
    this off. Without it, leases sized at twice their use gain little: half
    of every settled lease's unused size is still held back.
  Each lease keeps its hard cap either way (`MemoryMax` of its scope), so a
  lease never gets more than it declared; only the line moves faster when
  leases are sized well above what they use.
  A lease's use is its process tree's resident plus swapped memory, or, when
  larger, the unreclaimable use (memory.current less reclaimable page cache
  and slab, plus swap) of the `agent-coord-*` scopes `run` puts it in; a lease
  started inside another's tree is taken out of the outer one, and leases on
  one process count it once. Samples come every 2 s, beside the
  MemAvailable sample they are set against.

  **Safety bound and trade-off.** An admission by measured use leaves at least
  the reserve plus 10% of RAM free (10.2 GB on a 62 GB machine) as long as
  every running lease grows by no more than its allowance at that moment;
  leases that end only add to it. Admissions by reservation keep today's
  guarantee (every lease at its cap fits the lease capacity), and a machine
  is only ever overcommitted (sizes above the capacity) after an admission by
  measured use, so while it is, the bound above holds. What it does not
  cover: a lease that has held steady for 10 minutes and then grows by more
  than its allowance (a late phase that needs much more memory), all at once
  across leases, eats into the margin first, then the reserve, and past that
  the kernel reclaims, swaps, and on Linux systemd-oomd may kill the
  largest cgroup under pressure, which can be a long-running service. The
  worst case is bounded by the hard caps and the 1.5x limit: all leases at
  their caps can need at most 1.5 times the lease capacity. Lower
  `AGENT_COORD_MEM_OVERCOMMIT` (1.0 is reservation-only in effect) or raise
  `AGENT_COORD_MEM_GROWTH` (fraction, default 0.5; 1.0 counts every settled
  lease's whole remaining size) or `AGENT_COORD_MEM_MARGIN` (fraction of RAM,
  default 0.10) for more safety, at the cost of throughput;
  `AGENT_COORD_MEASURED_MEM=0` turns measured admission off. The service
  reads them at start (set them with `Environment=` in its unit). Sizing
  leases from measured peaks (`history --peaks`, below) makes reservations
  honest, which helps both rules.
  On Windows (once agent-coord runs there) the same rule applies with a
  lease's use measured as its job's commit (private bytes), since commit is
  what fails there.
- `hold` registers work that is already running: it counts at once, without
  waiting. Holding a VM's qemu PID replaces its measured memory with the lease.
- Docker containers run outside `run`'s caps (they live in dockerd's cgroup):
  lease them anyway, and pass `--cpus N --memory SIZE` to `docker run`.
- **Disk** (`--disk PATH:SIZE`): a request waits until PATH's disk has SIZE free
  beyond all held reservations on it and a 20 GB margin. Reservations count in
  full until the lease ends, so reserve the space the job still needs at its
  peak (sort temp + outputs), not the inputs it already has.
- **Named resources** (`lock`, `--lock`): one holder at a time. Use them for
  things that can't be shared (a VM's console/ownership, the GPU, a timed run's
  quiet NAS). They show in `status`; `unlock --force` takes one over and tells
  its holder.
- **The HDD archive pool (ZFS, `/mnt/nas`) holds reference archives only.**
  `run` refuses a command whose working directory, or whose output/temp
  options (`-o`, `--outdir`, `-T`, `--tmp-dir`, ...), are on it; reading inputs
  from it is fine. Active work goes on the SSDs (`/home`, `/mnt/Windows`).
  `--allow-archive` overrides, e.g. to archive a finished result.
- First come, first served: nothing overtakes the request at the head of the
  line, so big jobs are not starved, with these exceptions for requests that fit now:
  - **EASY backfill past a capacity wait**: a later job that fits now may start
    if its expected end precedes the head's projected start, or if it leaves
    enough cores, memory and headroom for the head at that time. Remaining
    memory claims count in full (or by measured use, as above, when that
    lets both start); CPU follows measured admission, with warmup
    and scoped exclusive guarantees. Outside load stays. Ending leases return
    only measured memory (at most their reservation), and non-ending leases'
    unmeasured memory growth is reserved against available memory.
    The head's first projected start is pinned and may only move earlier.
    Holder ends use the median successful duration (156 seconds initially),
    capped by their requested duration; overdue holders get a 60-second grace.
    Tiny jobs (at most 1 core/1 GiB, no disk) retain their pass rule even
    without a projection. After a capacity head waits 10 minutes, no new
    capacity backfill passes it. Jobs reserving disk do not pass capacity waits,
    nor do jobs pass a capacity-waiting head with named locks. Queued lock
    users keep their order, except for the existing stuck-head exception.
    Use `--expect 10m` on `run` or `hold` to supply an estimate, not a time
    limit. Without it, the service uses the 90th percentile of the last 20
    explicitly successful, non-held runs for the same agent and label, falls
    back to the same label across agents, or uses 30m without samples.
    Estimates are cached and updated as runs finish. Holder projections use
    the median; candidate lengths use the conservative estimate.
    `check --label TEXT` uses the same learned estimate as `run`;
    `check --expect 10m` overrides it. Estimates start when granted; for `hold`,
    supply the expected remaining time.
    If an overdue backfill job is delaying its head, no new capacity backfill
    starts; the overrunning agent is told once through its inbox. Jobs are
    never killed for exceeding estimates. `status` and `history` show backfill
    decisions, hold reasons and overrun notices.
  - **Past a lock-waiting head** (a GPU job while another holds the GPU), any
    request that doesn't need that lock may start if it leaves the head room
    (cores, memory, headroom) for when the lock frees.
  - **Past a waiting `--exclusive` request** (anywhere ahead in line), small
    requests (2 cores or less, 6 GB or less, no disk, none of its locks) may
    start, but only during its first 30 minutes of waiting. An exclusive request
    waits for every lease to end anyway, so a small one only adds its own run
    time; after the window nothing new starts past it, so its wait is bounded
    by 30 minutes plus the longest lease already running. Nothing bigger passes
    it, and exclusive requests never overtake anything.

  - **Past a stuck request**, any request that fits may start, in order. A
    request is stuck once it has waited 2 minutes short of actually available
    memory and would not fit even if every lease ended (each counted at what it
    really holds, at most its size): the memory is held outside the line, so
    holding others back gains it nothing. Its owner is told once, and it takes
    its turn again as soon as it would fit. Plain `--exclusive` requests are
    never stuck. Once a stuck request is skipped, the existing ordering rules
    behind it remain: only tiny jobs (at most 1 core/1 GB, no disk) may pass
    the next capacity-blocked request without a named lock.

  A request that has waited a minute tells the holders (once) through their inbox.
  IO holders only get these messages when using at least half their CPU cap.
  A non-IO holder gets one sizing hint if a lease uses under 15% of its CPU
  (for caps of at least 2 cores) or memory for 10 minutes after warmup.
- `--exclusive` (whole machine) waits until no other lease is held, then
  keeps new ones waiting except for the idle backfill described above.
- `--exclusive-lock NAME` (e.g. `gpu`) is exclusive on that resource only: it
  holds NAME like `--lock` (waiting while anyone else holds it, and keeping
  others from taking it), its cores count at their full size rather than
  measured use (unless marked `--io`), so they stay guaranteed, and CPU-only
  leases keep running in the rest of the machine. This is what GPU timing runs
  should use.
- `--headroom N` keeps N cores unclaimed beside the lease: it starts only when
  its CPU claim plus N fit with non-IO leases counted at their full size and IO
  leases at measured use. While it runs, admission keeps those N cores free
  under the same accounting. IO jobs can still burst up to their cap.
- `--reserve` (on `run` and `hold`) marks a lease that holds capacity for a
  later handover and uses little itself: its whole size less its measured use
  stays reserved for its whole life (not only while it ramps up), its cores
  count in full, and its runs never feed the label's run estimates or memory
  ceiling (`history --peaks` skips them). `status` shows it as `RESERVED`.
- A lease ends when its process exits, subject to the restart grace for
  tracked holds and detached runs.

## History

Every lease event is appended as one JSON line to
`~/.local/state/agent-coord/history.jsonl`, rotated at 8 MB (four older files,
`history.jsonl.1` .. `.4`, are kept). Events: `requested`, `waiting` (the head
of the line's reason, logged when its kind changes: `exclusive`, `lock:NAME`,
`cores`, `mem`, `available`, `disk`, `headroom`; later requests also log changed
backfill hold reasons), `started` (with `wait` seconds and any backfill reason),
`overrun` (with the delaying lease's notice), `ended` (with `wait`, `run` seconds
and the command's exit `code` when `run` reports it) and `cancelled` (released or died while waiting). Each
carries the lease id, agent, label, cores, mem, IO flag, locks, expected seconds and
estimate source, and exclusive mode
(`machine`, `lock` or null); never commands, paths or environment.
`restarting` and `resumed` describe tracking gaps. `exclusive-started` marks
phase acquisition, and `exclusive-idle` / `exclusive-overrun` record warnings.
Each `exclusive-ended` event reports the resource, total hold seconds,
seconds with usable samples, and approximate active seconds within those
samples. A zero observed duration means there was no utilization evidence.
`agent-coord history [--since 6h] [--json]` prints it (default: last 24 h).

`ended` events also carry the lease's measured peaks: `mem_peak` (bytes, the
largest sampled use as admission measures it), `mem_peak_scope` (the scope's
kernel `memory.peak`, which includes page cache, when `run` made the scope)
and `cores_peak` (CPU use smoothed over ~10 s). Samples come every 2 s, so a
shorter spike can be missed by `mem_peak`. To size `--mem` from them:

```sh
agent-coord history --peaks "gate:"          # labels starting with "gate:"
agent-coord history --peaks "gate:" --json   # {label: {runs, ok, mem_peak_max, mem_peak_last,
                                             #   mem_asked_last, suggest_mem, cores_peak_max, ...}}
```

It looks at each label's last 10 measured runs (`--last N`) in the last 30
days (`--since`), and suggests `--mem` as the largest memory peak plus 15%.

On Linux, `run` enforces the lease in a systemd user scope (a service when
detached) with
`CPUQuota` (N cores) and `MemoryMax` (exceeding it kills the command, exit 137),
and sees `COMPUTE_LEASE_CORES=N` to size its thread pools. Java programs (GATK,
Picard) get `JAVA_TOOL_OPTIONS=-XX:MaxRAM=SIZE -XX:MaxRAMPercentage=60`, so their
heap fits the lease (JVMs otherwise size it from the host's RAM and get killed). Without a systemd
user session it still runs, uncapped, at nice 19 / idle IO.

## Windows

The same file runs on Windows (Python 3.10+, standard library only). Leasing,
admission, backfill, locks, messages and history behave as on Linux; only the
platform layer differs:

- **Service**: a background `pythonw` process, not a systemd unit. It listens
  on a random loopback TCP port and writes the port and a random token to
  `%LOCALAPPDATA%\agent-coord\endpoint.json` (your profile, so only you can
  read it); requests without the token are refused. A lock file allows one
  server per endpoint. Any command starts the service if it isn't running.
  `agent-coord service install` also starts it at logon (an HKCU `Run` entry);
  `skill-dump install` does this and restarts it when the tool changes.
  `agent-coord service status|start|stop|restart|uninstall` manage it; its log
  is `%LOCALAPPDATA%\agent-coord\service.log`.
- **State and history**: `%LOCALAPPDATA%\agent-coord\` (`XDG_STATE_HOME` wins
  if set).
- **Measurement**: Win32 APIs instead of `/proc` (system times, memory status,
  process memory counters, a process snapshot for descendants). A process's
  footprint is the larger of its working set and private commit. QEMU guests
  and readable WSL2/Hyper-V `vmmem` processes count as VMs. `status` shows no
  load average (Windows has none).
- **`run`**: the command starts suspended in a job object with a hard CPU rate
  cap of N cores, a job memory limit of SIZE, and idle priority (the
  counterpart of nice 19); the job dies with `agent-coord run`. Past the memory
  limit, allocations fail rather than the command being killed (out-of-memory
  errors rather than exit 137). Commands resolve through `PATHEXT`, so
  `npm`/`.cmd` work; run shell scripts as `bash script.sh`.
- **Disks**: `--disk` takes a path on a volume (`--disk D:\:150G`,
  `--disk C:\Users\me\work:20G`); reservations are per volume.
- **Archives**: no ZFS detection; set `AGENT_COORD_ARCHIVE` (paths separated
  by `;`) to protect archive shares the same way.

`AGENT_COORD_AS` sets the agent name on every platform. That helps when several
sessions share a folder, since they would otherwise share a name and its inbox.

## Operate

```sh
systemctl --user status agent-coord
journalctl --user -u agent-coord
python3 -m unittest -v test_agent_coord     # in tools/agent-coord
agent-coord service status                  # Windows
```

`AGENT_COORD_RUNTIME_DIR` and `AGENT_COORD_STATE_DIR` are for tests and
throwaway trial instances only: they point `serve` and the client commands at
another socket, state and history. Never set them in an agent's normal shell or
in the user manager's environment: clients would miss the shared service, and
the shared service itself would start from an empty state, forgetting every
live lease.
