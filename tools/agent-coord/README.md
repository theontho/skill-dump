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

# Heavy job, elastic: starts once 2 cores are free, with up to 8 if free; may
# be shrunk toward 2 and grow back while it runs (nice 19, idle IO).
agent-coord run --cores 2:8 --mem 8G --label "full test suite" -- \
    sh -c 'make -j"$COMPUTE_LEASE_CORES" test'

# Fixed size (never resized): waits for 4 cores, runs capped at 4 cores / 8G.
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

## How admission works

- Capacity is all cores, and RAM minus 4 GB kept for the desktop and the agents
  themselves.
- **CPU is admitted on measured use**: host-wide busy cores (smoothed over ~10 s),
  plus each non-IO lease started in the last minute at its full size while it ramps up.
  `--exclusive-lock` leases, and elastic leases at their grant, count once:
  their own measured use, up to their size (smoothed like the busy figure,
  from zero, so a shrink leaves it as the busy figure does; counted once when
  one lease runs inside another's process tree), is taken out of the busy
  cores, so only the rest of the load counts beside their full size. Use above
  their size (a job in the tree but in a scope of its own, an uncapped hold)
  still counts as load. Warming leases are counted as before.
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
- **Memory is admitted on reservations**: every lease at its requested size (or
  its measured tree footprint if larger for `--io`), plus
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
- First come, first served (across projects too): nothing overtakes the request
  at the head of the line, so big jobs are not starved, with these exceptions
  for requests that fit now:
  - **EASY backfill past a capacity wait**: a later job that fits now may start
    if its expected end precedes the head's projected start, or if it leaves
    enough cores, memory and headroom for the head at that time. Remaining
    memory claims count in full; CPU follows measured admission, with warmup
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
- `--exclusive` (whole machine) waits until no other lease is held, then keeps
  new ones waiting until it ends. Use it only when the whole machine must be quiet.
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
- A lease ends when its process exits; dead holders and waiters are dropped.

## Elastic leases, projects and the reserve

In short: **use `--cores MIN:MAX` for everything except timing runs.** Elastic
jobs share the machine fairly between active projects and can use every core
when the others are idle. Fixed and exclusive jobs are never shrunk. A project
alone on the machine runs fixed jobs exactly as before; while another project
is active, one project's fixed and timing jobs are capped at all cores minus a
small reserve, and a project that runs only fixed jobs gets no fair-share
protection: it gets cores only as they free up.

### Elastic leases

`run --cores MIN:MAX` asks for at least MIN cores and at most MAX (clamped to the
machine). It is admitted as soon as MIN fits; the line's backfill and `--expect`
projections count it at MIN. It starts with as many cores up to its target as
are not granted to anyone (grants, fixed sizes, load outside leases, headroom)
and not needed by requests waiting for CPU behind it; its target is MAX within
its project's fair share. At least MIN.

The command runs in a systemd scope the service names
(`agent-coord-LEASE-CLIENTPID.scope`). **The service resizes it itself**, with
`systemctl --user set-property --runtime SCOPE CPUQuota=N%` (5 s timeout), on a
worker thread so that a slow user bus never holds up other requests:

- A shrink counts only after the call succeeded (a request waiting for those
  cores starts then, usually within a second); a growth counts at once and is
  taken back if the call fails.
- A lease is resized only after its `run` client reported that its command
  runs in the scope (or 60 s after its grant if the client never does).
- A failed call is retried after 30 s, then 60 s; after 3 failures in a row the
  lease is fixed at the size it has (`status`: "fixed: scope not resizable",
  and its holder is told). Meanwhile preemption uses other leases.
- The service acts only on scopes it named, by that exact name; it never
  signals a process. Without a systemd user session a MIN:MAX request gets one
  size at its start and keeps it.

An elastic lease counts at its grant (its own measured use is taken out of the
host's busy cores), like a timing run; idle shrink keeps grants near real use.

- **Preemption.** A request that waits only for CPU (cores or headroom) and is
  allowed to start at its place in line (the head, or a request the backfill
  rules let pass; behind a lock-waiting head, also leaving that head its room)
  shrinks running elastic leases until it fits:
  - other projects' elastic leases, only while the requesting project stays
    within its entitlement: its fair share, and at least `reserve_cores`. So
    asking for a big fixed size never gets a project more than asking MIN:MAX:
    on 12 cores with another project's busy 2:12 at 12, a fixed 10 waits ("other
    projects' elastic leases are shrunk only to give project B its fair share
    (6 of 12 cores) ... --cores MIN:MAX would start now") while a 2:10 starts at
    6 and the other lease is shrunk to 6. The project furthest over its share
    gives first, then the lease with the largest surplus over its MIN;
  - the requesting project's own elastic leases, toward MIN, only as far as it
    needs to reach its MIN or fixed size.

  Nothing changes unless the request then fits. Never below MIN; fixed, `--io`
  and timing leases (`--exclusive`, `--exclusive-lock`, `--headroom`) are never
  shrunk; nothing is killed. A shrunk lease's holder gets an inbox note. A
  MIN:MAX request that would start below its target shrinks others the same
  way before it starts.
- **Evening out.** About once a minute, a busy elastic lease of a project under
  its share takes cores from elastic leases of projects over theirs.
- **Idle shrink.** Using under half its grant for 2 minutes, an elastic lease
  is offered down to max(MIN, ceil(peak use / 0.7)).
- **Growth.** Using at least 75% of its grant, it grows toward MAX when nothing
  waits for cores, into cores that are neither measured busy nor granted to
  anyone: every such core up to its project's share, then keeping a core
  free. At most one change a minute per lease, except when preempted.
- **The command learns its grant** from `COMPUTE_LEASE_CORES` at start, and
  from `COMPUTE_LEASE_FILE`, rewritten atomically (temp file + rename) after
  every change: line 1 is the whole cores to use now (at least 1), line 2 a
  JSON object with `lease`, `min`, `max`, `granted`, `reason` and `time`
  (`read -r cores < "$COMPUTE_LEASE_FILE"`). Polling this file is the
  recommended way to follow changes; the scope is already capped by then, so a
  command that ignores it is throttled, not broken. `COMPUTE_LEASE_MIN` and
  `COMPUTE_LEASE_MAX` are set too. The `run` client's follower only reads the
  grant and rewrites the file; nothing in the lease depends on it, and it
  survives errors. The client removes the file when the command ends; the
  service removes files of dead clients at start.
- **`--notify-signal`** also sends SIGUSR1 to the process `run` started (only
  that pid, from its own `Popen`; never a process group) after each change.
  SIGUSR1 terminates a process that has not installed a handler: a command
  still starting, or a wrapper such as `sh -c`, `make` or `uv run`, is killed by
  it. Use it only for programs that handle SIGUSR1 from their first moment.

Memory stays fixed (`--mem`): it cannot be taken back safely.

### Projects and fair shares

Each lease belongs to a project: the git repository `run` or `check` is called
in (for `hold`, the held process's working directory where readable), named
after its main checkout's folder, so all worktrees of one repository are one
project (`git rev-parse --git-common-dir`). Outside git it is the agent name.
`--project NAME` counts as its own project only if NAME is listed under
`[projects.NAME]` in the config; otherwise it shares its repository's project,
so inventing names gains nothing. Two repositories in folders of the same name
are one project unless one of them uses a configured name. Requests from older
clients count under the name they give, or `default`. A project is active while
it has a running or waiting lease; idle projects claim nothing.

Each active project's **fair share** is cores x weight / (sum of the active
projects' weights), weight 1 by default. Shares decide how far a waiting
request may shrink other projects' elastic leases, how big a MIN:MAX lease
starts and how elastic leases even out. They do not reorder the line, which
stays first come, first served with the backfill rules above.

### The reserve

While **another project is active**, a project's **non-preemptible** cores
may not exceed the machine's cores minus `reserve_cores` (2). They are counted
the way admission counts them: elastic leases at their MIN; timing runs
(`--exclusive-lock`, `--headroom`) at size plus headroom; `--io` leases at
measured use; ordinary fixed leases at their size while they warm up, then at
measured use (at most their size). A project alone on the machine is not
capped: its fixed jobs are admitted exactly as before.

- A request that would take its project past the cap waits for its own
  project's leases, with a reason that says so. It does not hold back other
  projects' requests: they pass it by the ordinary rules (as they pass a stuck
  request).
- A timing run whose size plus headroom alone exceeds the cap (10 + 1 on 12
  cores), or a MIN that does, is refused at once with a message naming
  `reserve_cores` and the ways out (`--cores MIN:MAX`, whole-machine
  `--exclusive`, or a lower `reserve_cores`). A plain fixed request that big
  is not refused: it can run while its project is alone. A waiting request
  that becomes too big (a config change) is refused if its client is current;
  an older client's request keeps waiting, and others pass it.
- When another project arrives while one holds fixed leases above the cap,
  nothing is shrunk or killed: the newcomer gets what is free and what elastic
  leases give back; `status` marks the fixed leases (over the cap, holding the
  newcomer back), and no new fixed lease of the first project starts until it
  is back under.
- Whole-machine `--exclusive` keeps its own rules and is exempt.
  `reserve_cores = 0` turns the rule off.

What it guarantees: while several projects are active, the one holding the
most non-preemptible cores leaves at least `reserve_cores` cores that are free
or held by elastic leases above MIN, for all the other projects together (one
pool, not one per project). A project asking for that many can use them
through the ordinary line rules, by preemption if need be.

What it does not guarantee: another project may already hold the pool (an
elastic lease gives back only down to its MIN: beside an 8-core timing run with
headroom 1, a 2:4 lease of one project can hold the 3 remaining cores, and a
third project's 2-core request then waits for it; the waiting reason names it);
several projects with fixed leases can between them fill the machine; fixed
leases that were already running when another project arrived are not
shrunk; a project that runs only fixed jobs gets cores only as they free up,
never its fair share; and load outside leases still counts. When leases of
other projects keep a waiting project below its share, its reason names them
(fixed and timing ones "never shrunk", elastic ones with what they could still
give), `status` marks fixed ones `HOLDS BACK`, history records an `unfair`
event, and holders of ordinary fixed leases (not timing runs) get one inbox
note suggesting MIN:MAX.

**Accounting invariant**: the sizes in force (fixed sizes, elastic grants after
their scope is capped, headroom) add up to at most the machine, except where
fixed sizes and MINs were admitted on measured use of leases using less than
they hold (as before this change); elastic sizes above MIN come only from
cores granted to no one.

`agent-coord status` lists the active projects with weight, share, cores held
(and how many are non-preemptible) and requests waiting, when more than one is
active.

### Configuration

Optional: `~/.config/agent-coord/config.toml` (or `$AGENT_COORD_CONFIG`), re-read
when it changes. Every value is checked: an unknown key, a value of the wrong
type or out of range, or a file that is not valid TOML keeps the default, is
logged once per load and is listed in `status`; a bad file never stops the
service. Defaults and ranges:

```toml
reserve_cores = 2        # 0..64 (0 turns the reserve off), used up to half the machine
default_weight = 1       # > 0, <= 1000
resize_interval = 60     # 0..3600 s between changes of one lease (preemption excepted)
idle_fraction = 0.5      # 0..1: idle shrink below this share of the grant ...
idle_after = 120         # 0..86400 s ...
idle_target_use = 0.7    # (0..1]: ... down to ceil(peak / this)
grow_demand = 0.75       # (0..1]: grow while using at least this share of the grant
grow_margin = 1          # 0..64 cores kept free beyond a project's share when growing

[projects.some-repo]
weight = 2               # > 0, <= 1000
```

## History

Every lease event is appended as one JSON line to
`~/.local/state/agent-coord/history.jsonl`, rotated at 8 MB (four older files,
`history.jsonl.1` .. `.4`, are kept). Events: `requested`, `waiting` (the head
of the line's reason, logged when its kind changes: `exclusive`, `lock:NAME`,
`cores`, `mem`, `available`, `disk`, `headroom`, `reserve`; later requests also log changed
backfill hold reasons), `started` (with `wait` seconds and any backfill reason),
`resized` (an elastic lease's grant changed: `from`, new `cores`, `kind`
`preempted`, `grew`, `idle` or `fixed`, and the `reason`, e.g. which request
preempted it), `unfair` (fixed leases keep a waiting project below its share),
`overrun` (with the delaying lease's notice), `ended` (with `wait`, `run` seconds
and the command's exit `code` when `run` reports it) and `cancelled` (released or died while waiting). Each
carries the lease id, agent, project, label, cores (and `min_cores`/`max_cores` for
MIN:MAX), mem, IO flag, locks, expected seconds and
estimate source, and exclusive mode
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
# Opt-in: also run commands in real transient scopes (agent-coord-test-*) and
# resize one; by default a seam stands in for systemd.
AGENT_COORD_SYSTEMD_TESTS=1 python3 -m unittest -v test_agent_coord
```

The tests run private instances only. `AGENT_COORD_RUNTIME_DIR`,
`AGENT_COORD_STATE_DIR`, `AGENT_COORD_CONFIG` and `AGENT_COORD_SCOPE_PREFIX` are for
tests and throwaway trial instances only (their own socket, lease files, state,
history, config and scope names, for both `serve` and the client commands).
Never set them in an agent's normal shell or in the user manager's environment:
clients would miss the shared service, and the shared service itself would
start from an empty state, forgetting every live lease.
