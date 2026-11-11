# Shared CPU, RAM and agent messages (agent-coord)

Several agents share this computer. Coordinate heavy work through the `agent-coord` user service (`agent-coord --help`):

- **Before anything heavy** (more than 2 cores or 2 GB for more than a few minutes: alignment, variant calling, full test suites, big builds), run it through `agent-coord run --cores N --mem SIZE --label "what" -- COMMAND`.
  - It waits its turn, then caps the command at N cores and SIZE of RAM.
  - Give the command's tools N threads.
  - Add `--expect 10m` when you know the run time; otherwise it learns from completed runs for your agent/label (30m initially). Jobs may pass a capacity-blocked head only if they finish before its projected start or leave its capacity free. Overruns that delay the head pause new backfill and notify their agent; estimates do not kill jobs.
- `agent-coord check --cores N --mem SIZE` says whether a job that size may start now. `agent-coord status` shows who holds what.
- **Timed or performance runs:** for a GPU timing run use `--exclusive-lock gpu` (exclusive on the GPU, its cores guaranteed, CPU-only work keeps running beside it), plus `--headroom N` to keep N more cores unclaimed and bound timing noise. Use whole-machine `--exclusive` only when nothing else may run; small jobs (≤2 cores, ≤6G) still start during its first 30 min of waiting. A job started without a lease can be registered with `agent-coord hold … --pid PID`.
- `agent-coord history [--since 6h] [--json]` shows lease events with wait and run times and why requests waited.
- **Messages:** read `agent-coord inbox` at the start of work, before heavy jobs, and when a lease waits a long time. Use `agent-coord send --to AGENT|all "…"` to tell others about things that affect them, such as long IO, VM ownership or when you will finish.
- Reserve disk for big outputs and temp files with `--disk PATH:SIZE`, sized for the job's peak (for example `--disk /home:150G` for a 30x realign).
- Own things that can't be shared (a VM, the GPU, the NAS link during a timed run) with `agent-coord lock NAME --reason "…" [--pid PID]` until `unlock NAME`, or pass `--lock NAME` to `run`. Check `status` before touching something another agent owns.
- Your name is your git worktree's folder name; use `--as NAME` to set another.
- If the service is down, start it with `systemctl --user start agent-coord`; do not work around it.
