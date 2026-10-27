# agent-coord

A small local service for the agents (and people) sharing this computer. It
gives them one live place to:

- **queue heavy jobs** for CPU cores and RAM, so jobs wait their turn instead
  of piling onto a host that is already swapping;
- **check** whether a job of a given size may start now;
- **message** each other (one agent, or `all`).

It is a user systemd service (`agent-coord.service`) speaking JSON lines on
`$XDG_RUNTIME_DIR/agent-coord/sock`, with its state in
`~/.local/state/agent-coord/state.json`. The state survives restarts. The CLI
is the same file, installed as `~/.local/bin/agent-coord` by `skill-dump install`.

## Use

```sh
agent-coord status                       # who holds what, the line, what is free
agent-coord check --cores 4 --mem 8G     # exit 0 if it would start now

# Heavy job: waits in line, then runs capped at 4 cores / 8G, nice 19, idle IO.
agent-coord run --cores 4 --mem 8G --label "ultima chr20 realign" -- \
    wgsextract realign --threads 4 ...

# Timed/perf run: waits for other leases to end, then holds new ones back.
agent-coord run --cores 6 --mem 12G --exclusive --label "windows timing" -- ...

# Lease for a job already started (until its PID exits).
agent-coord hold --cores 6 --mem 10G --label "Windows VM full SNP" --pid 3442633

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
  line, so big jobs are not starved, except small requests (1 core or less, 1 GB
  or less, no disk reservation), which may start past a blocked head when they fit.
  Nothing passes an exclusive or lock-waiting head. A request that has waited a minute tells
  the holders (once) through their inbox.
- `--exclusive` waits until no other lease is held, then keeps new ones waiting.
- A lease ends when its process exits; dead holders and waiters are dropped.

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
