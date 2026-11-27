# Shared CPU, RAM and agent messages (agent-coord)

Several agents share this computer. Coordinate heavy work through the `agent-coord` user service (`agent-coord --help`):

- **Before anything heavy** (more than 2 cores or 2 GB for more than a few minutes: alignment, variant calling, full test suites, big builds), run it through `agent-coord run --cores N --mem SIZE --label "what" -- COMMAND`.
  - It waits its turn, then caps the command at N cores and SIZE of RAM on Linux and Windows. On macOS these are scheduling reservations, not hard caps.
  - Give the command's tools N threads.
  - Add `--expect 10m` when you know the run time; otherwise it learns from completed runs for your agent/label (30m initially). Jobs may pass a capacity-blocked head only if they finish before its projected start or leave its capacity free. Overruns that delay the head pause new backfill and notify their agent; estimates do not kill jobs.
- `agent-coord check --cores N --mem SIZE` says whether a job that size may start now. `agent-coord status` shows who holds what.
- Use `--io` for downloads, uploads and disk-streaming jobs; it defaults to 1 core and 512M.
- **Timed or performance runs:** for a GPU timing run use `--exclusive-lock gpu` (exclusive on the GPU, its cores guaranteed, CPU-only work keeps running beside it), plus `--headroom N` to keep N more cores unclaimed and bound timing noise. Use whole-machine `--exclusive` only when nothing else may run; small jobs (≤2 cores, ≤6G) still start during its first 30 min of waiting. A job started without a lease can be registered with `agent-coord hold … --pid PID`.
- **Runs with multiple phases:** in the leased driver acquire `agent-coord phase --exclusive-lock gpu` only for the timing phase, then `agent-coord phase --release gpu` even on failure. Idle or overdue exclusive leases warn in `status` and the owner's inbox; a warning does not stop the job.
- **Unattended runs:** use `agent-coord run --detach --unit NAME … -- COMMAND`, or register an existing driver using `hold --pidfile PATH` or `hold --unit NAME`. Watch the lease with `poll ID --json` rather than a captured PID. A tracked lease may report `restarting` without being gone.
- `agent-coord history [--since 6h] [--json]` shows lease events with wait and run times and why requests waited.
- **Size `--mem` from measured peaks:** `agent-coord history --peaks LABEL-PREFIX [--json]` lists each label's recent memory and CPU peaks and suggests `--mem` (peak + 15%). Keep labels stable per kind of job: memory admission trusts a label's past peaks.
- **Messages:** read `agent-coord inbox` at the start of work, before heavy jobs, and when a lease waits a long time. Use `agent-coord send --to AGENT|all "…"` to tell others about things that affect them, such as long IO, VM ownership or when you will finish.
- Reserve disk for big outputs and temp files with `--disk PATH:SIZE`, sized for the job's peak (for example `--disk /home:150G` for a 30x realign).
- Own things that can't be shared (a VM, the GPU, the NAS link during a timed run) with `agent-coord lock NAME --reason "…" [--pid PID]` until `unlock NAME`, or pass `--lock NAME` to `run`. Check `status` before touching something another agent owns.
- Your name is your git worktree's folder name; use `--as NAME` (or `AGENT_COORD_AS=NAME`) to set another. Several sessions in the same folder share that name and its inbox, so give each its own name when they need to tell each other apart.
- If the service is down, start it with `systemctl --user start agent-coord` (Linux), `launchctl kickstart "gui/$(id -u)/com.theontho.agent-coord"` (macOS), or `agent-coord service start` (Windows, where commands also start it on demand); do not work around it.
- On Windows, `run` caps the command with a job object (CPU rate, memory limit, idle priority), and disk paths are drives, e.g. `--disk D:\:150G`.
