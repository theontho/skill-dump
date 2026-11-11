# agent-coord

A small local service for the agents (and people) sharing this computer. It
gives them one live place to:

- **queue heavy jobs** for CPU cores and RAM, so jobs wait their turn instead
  of piling onto a host that is already swapping;
- **check** whether a job of a given size may start now;
- **message** each other (one agent, or `all`).

It is a user systemd service (`agent-coord.service`) speaking JSON lines on
`$XDG_RUNTIME_DIR/agent-coord/sock`, with its state in
`~/.local/state/agent-coord/state.json`. The state survives restarts: held
leases and waiting requests are kept, and waiting `run` clients retry their
polls for two minutes while the service restarts. The CLI
is the same file, installed as `~/.local/bin/agent-coord` by `skill-dump install`.

## Use

```sh
agent-coord status                       # who holds what, the line, what is free
agent-coord check --cores 4 --mem 8G     # exit 0 if it would start now

# Heavy job: waits in line, then runs capped at 4 cores / 8G, nice 19, idle IO.
agent-coord run --cores 4 --mem 8G --label "ultima chr20 realign" -- \
    wgsextract realign --threads 4 ...

# GPU timing run: exclusive on the GPU only, with its 6 cores guaranteed and
# 2 more kept unclaimed; CPU-only leases still run in the rest.
agent-coord run --cores 6 --mem 40G --exclusive-lock gpu --headroom 2 --label "gpu timing" -- ...

# Whole-machine timed run: waits for every other lease to end, then holds new ones back.
agent-coord run --cores 6 --mem 12G --exclusive --label "windows timing" -- ...

# Lease for a job already started (until its PID exits).
agent-coord hold --cores 6 --mem 10G --label "Windows VM full SNP" --pid 3442633

# Reserve disk for the job's peak (temp + outputs); waits until it's free.
agent-coord run --cores 4 --mem 8G --disk /home:150G --label "30x realign" -- ...

# Named resources: a VM, the GPU, the NAS link... (until unlock, PID exit or TTL)
agent-coord lock windows-vm --reason "full SNP correctness until Tue AM" --pid 3442633
agent-coord run --lock gpu --cores 2 --mem 8G --label "parabricks" -- ...

agent-coord unlock windows-vm

# Announce a failure (to an agent, or all) instead of leaving it in a log.
agent-coord run --cores 4 --mem 12G --notify-on-fail all --label "overnight call" -- ...

agent-coord send --to worktree-ultima "done with the VM at 15:00"
agent-coord send --to all "NAS scrub starting; IO will be slow"
agent-coord inbox [--wait 10m]           # unread messages, then marked read
agent-coord agents                       # who has been seen
agent-coord history --since 6h           # lease events with wait/run times (--json)
```

Agents are named after their git worktree's folder (`worktree-ultima`), or
`--as NAME`.

## How admission works

- Capacity is all cores, and RAM minus 4 GB kept for the desktop and the agents
  themselves.
- **CPU is admitted on measured use**: host-wide busy cores (smoothed over ~10 s),
  plus each lease started in the last minute at its full size while it ramps up.
  Leases usually use less than they ask, and sharing CPU only slows jobs a
  little, since each is capped at its size at nice 19. CPU should therefore stay
  busy, not idle behind reservations. Time spent waiting on disk is not CPU use.
- **Memory is admitted on reservations**: every lease at its requested size, plus
  running QEMU guests (resident + swapped) that no lease holds. A request also
  needs its memory *actually available* now (MemAvailable minus the 4 GB), so
  nothing is admitted into swap.
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
  - **Tiny requests** (1 core or less, 1 GB or less, no disk reservation) may
    start past a head waiting for capacity, unless that head names a lock.
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
    never stuck.

  A request that has waited a minute tells the holders (once) through their inbox.
- `--exclusive` (whole machine) waits until no other lease is held, then keeps
  new ones waiting until it ends. Use it only when the whole machine must be quiet.
- `--exclusive-lock NAME` (e.g. `gpu`) is exclusive on that resource only: it
  holds NAME like `--lock` (waiting while anyone else holds it, and keeping
  others from taking it), its cores count at their full size rather than
  measured use, so they stay guaranteed, and CPU-only leases keep running in the
  rest of the machine. This is what GPU timing runs should use.
- `--headroom N` keeps N cores unclaimed beside the lease: it starts only when
  its cores plus N fit with every lease counted at its full size (each is capped
  at that by CPUQuota), and while it runs nothing is admitted into those N
  cores. That bounds CPU contention, and so timing noise, without stopping all
  other work.
- A lease ends when its process exits; dead holders and waiters are dropped.

## History

Every lease event is appended as one JSON line to
`~/.local/state/agent-coord/history.jsonl`, rotated at 8 MB (four older files,
`history.jsonl.1` .. `.4`, are kept). Events: `requested`, `waiting` (the head
of the line's reason, logged when its kind changes: `exclusive`, `lock:NAME`,
`cores`, `mem`, `available`, `disk`, `headroom`), `started` (with `wait`
seconds), `ended` (with `wait`, `run` seconds and the command's exit `code`
when `run` reports it) and `cancelled` (released or died while waiting). Each
carries the lease id, agent, label, cores, mem, locks and exclusive mode
(`machine`, `lock` or null); never commands, paths or environment.
`agent-coord history [--since 6h] [--json]` prints it (default: last 24 h).

`run` enforces the lease: the command runs in a systemd user scope with
`CPUQuota` (N cores) and `MemoryMax` (exceeding it kills the command, exit 137),
and sees `COMPUTE_LEASE_CORES=N` to size its thread pools. Java programs (GATK,
Picard) get `JAVA_TOOL_OPTIONS=-XX:MaxRAM=SIZE -XX:MaxRAMPercentage=60`, so their
heap fits the lease (JVMs otherwise size it from the host's RAM and get killed). Without a systemd
user session it still runs, uncapped, at nice 19 / idle IO.

## Operate

```sh
systemctl --user status agent-coord
journalctl --user -u agent-coord
python3 -m unittest -v test_agent_coord     # in tools/agent-coord
```
