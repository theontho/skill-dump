---
name: pr-coordinator
description: "Use when the owner asks to coordinate work across machines: landing a pull request backlog, then working through the issue queue with implementer agents."
license: MIT
metadata:
  author: theontho
  version: "1.0.0"
---

# PR coordinator

Use when the owner asks to coordinate work across machines. Keep one
coordinator per project. An implementer handles one existing PR; don't open new
issues or PRs as part of a landing assignment.

## Two phases: land the backlog, then work the issue queue

Landing the PR backlog is only the first phase. When the backlog is merged or
handed to the landing train, switch to **issue-queue mode** without waiting to
be told: work the repository's issue queue in its own priority order (for
example, efficiency items, then bugs, then the roadmap's "Up next"), keeping
every machine staffed with implementer or long-run work suited to its hardware.

- A machine with no agent, no lease and no long run is a defect, not a resting
  state. When `events` reports an idle host, dispatch the next suitable item.
- At the start of every supervisor turn, run `pr-coord --project PROJECT
  agent supervisor heartbeat`. If the owner had to prompt you to continue
  or ask for status after a long gap, use `heartbeat --owner-resume` instead
  on that turn. Treat the gap as an incident: record it, check what stalled,
  and reactivate the watcher and dispatch.
- End a turn only when every managed host has an agent, a lease, or a
  registered unattended run with a failure alert, or when the queue is
  verifiably empty of eligible work. If empty, report what you checked:
  queue sources and filters, and why remaining items can't be dispatched.
  An empty PR list, an unchecked source or a failed query isn't an empty
  queue.
- "Waiting on X" means hand X to a watcher with a concrete wake trigger,
  then take the next item. Don't use a wait as a reason to leave hosts idle
  with work still available.
- Landing continues in the background: implementers hand drafts to
  `finish-draft`, and the train lands them.

## Coordinator setup

1. Copy `tools/pr-coordinator/config.example.toml` from the skill-dump checkout
   to `~/.config/pr-coordinator/<project>.toml`. Set the repo, owner, checkout,
   labels, landing gates and hosts. Leave the config and datasets outside Git.
2. Use `pr-coord --project <project> status`. For an old coordinator, stop its
   loops, then `pr-coord --project <project> migrate --from <old-dir>`. The
   default state directory is `~/.local/state/pr-coordinator/<project>/`.
3. Preview with `pr-coord --project <project> dashboard`. `run` adds read-only
   collection; use `run --landing` only with owner approval to add supervisor,
   train and runner yield/stale cancellation. Run it in a managed foreground
   session and monitor exits. The server defaults to loopback and has no access
   control of its own.
4. Keep `pr-coord --project <project> events --wait` armed as a background
   watcher for main failures, queue drops, review escalations, questions, idle
   hosts and `SUPERVISOR-IDLE` alerts. Handle each batch and rearm. The `run`
   service collects events even with no waiter; the dashboard shows recent
   events, pending counts, supervisor heartbeats and owner-resume incidents.
   Reuse the default `supervisor` cursor on handoff. Keep the configuration
   and run commands in the handoff so another coordinator can resume.
5. Start implementers on another machine with `pr-coord --project <project>
   remote-agent <host> <name> <prompt-file>` (tmux on POSIX hosts). On a
   Windows host (`windows = true`) the agent runs detached under the host's
   `agent_root`; check it with `remote-agent <host> --list`, read its log with
   `remote-agent <host> <name> --log`, and stop it with `remote-agent <host>
   <name> --stop` (only that agent's verified process tree). Never kill agent
   processes by name. See the README for the credential holder (`agent-env`)
   and its limits.

## Implementer runbook

- Follow the repository's contributor and PR policy. Confirm the assignment,
  owner and PR head; work only in the assigned worktree. Read existing
  handoffs and check local commits before touching the branch. Keep the primary
  checkout on its configured base branch. Don't push to a collaborator branch,
  act on `hands-off`, bypass protection, or push while a PR is enqueued.
- Record progress with `pr-coord --project <project> agent <name> working
  --pr '#N' --machine <host> --task '<work>'` and `pr-coord --project <project>
  pr N <stage> --note '<short status>'`. Use `waiting`, `blocked`, or `done` as
  appropriate so the coordinator sees capacity and outstanding work.
- Check mergeability, reviews, open threads and head-specific CI. Handle
  all open threads and real failures in one batch. For each finding, sweep
  related code/tests for the same bug and fix the class. Resolve threads only
  after the push or a posted rationale. A generic recommendation to
  get human review is not a request for changes unless project policy says
  otherwise. Stop for ownership, required approvals, policy, or source
  conflicts you can't resolve; don't stop for a busy runner.
- After two failures with the same command, inputs and error class, don't run
  it unchanged again. Diagnose or change approach (partial repair, another
  source or host, smaller repro), or escalate with both logs. `pr-coord`
  enforces this for `cert`, `rt` and train actions, pointing to both logs.
  Rerun CI only for confirmed infrastructure loss; file a flake issue for any
  rerun-only pass.
- Before every push, run the nearest tests for all changed files (`pr-coord
  nearest-tests <files...>` if configured; otherwise pick them manually), plus
  required local gates. Bound command timeouts; hand off long waits instead of
  polling. Send heavy authorized tests to
  `pr-coord --project <project> rt <host> <worktree> -- <command>` or take a
  local resource lease. Remote tests copy the working tree including uncommitted
  edits to that host; check the destination. Self-review the full diff as the
  reviewer, push once per round, then request re-review and certify that head.
  At three Copilot rounds, bring the recurring findings and next plan to the
  coordinator before another push; don't automatically repeat the loop.
- `pr-coord --project <project> cert <worktree> <target> --pr N` certifies the
  published head; `cert-pr N` uses a managed worktree. Wait through a foreground
  runner notification, not repeated polling. Confirm review and certification
  gates, then run `pr-coord --project <project> capacity` before marking an
  authorized draft ready. Readiness is distinct from a passing draft run.
- When checks pass, hand off with `pr-coord --project <project> train add N
  --worktree <worktree>`. Close the worktree and leave it clean; the train owns
  refresh, regeneration, quick checks, push, fresh CI and queue monitoring.
  Don't make further edits unless you remove it from the train and confirm
  its worker stopped. If manually landing under project policy, use `lane N`
  and never push after enqueue.
- Set the dashboard row to `done` on handoff and leave a brief handoff with
  the head, gates, evidence paths, blockers and next action.

## Questions and dashboard

Use non-blocking `coord-remote ask` or `pr-coord ask` with `--pr '#N'` (or
`--title`), `--default '<safe fallback>'`, and `--blocks '<parked action or none>'`
(put the blocker in the question if the remote wrapper lacks the flag). Continue
on the reversible default; otherwise park only the affected action and take
other work. Use blocking prompts only for irreversible, destructive or owner-only
actions if a live decision is essential; never hold a worker waiting.

Template: `1. <decision> — Context: <why now>. A: <option and consequence>;
B: <option and consequence>. Recommend: <A/B and why>. Blocks: <action or none>.`
Number each decision so the owner can answer by number. The coordinator answers
with `agent-questions ID ANSWER` or forwards unresolved decisions via `ask --owner`.
The owner answers via the trusted dashboard (if enabled) or `answer ID ANSWER`.

`/` shows PRs, a hoverable dependency graph and conflict matrix, host leases,
runner capacity, certifications, agent status, open question age/blockers,
review escalations and weekly review-round p90/4+ share. `/lifecycle` shows where
time went: wait vs. execution, revisits, queue drops and GitHub-linked traces.
`/tests` shows observed outcomes and test costs. Missing JUnit isn't a pass;
tests without evidence and workload reductions are recommendations, not
automatic removals. Refresh explicitly with `pr-coord --project <project>
tests --refresh`; `tests` alone rebuilds from cached evidence.

## Keep coordination costs down

Measure cost, failures and usefulness per test before proposing changes. Run
targeted, high-signal checks per change and keep broad, long-green or
cross-platform work on a scheduled or release backstop. Verify once at each
level; cancel superseded work without touching merge-group or collaborator
jobs. Avoid checked-in shared generated files where possible; otherwise split
artifacts by owner and run a conflict-aware lane. Give human contributors
capacity and control. Batch review changes, and get owner approval before
changing CI policy. Update tool code through the managed deployment, never by
editing live scripts in place.
