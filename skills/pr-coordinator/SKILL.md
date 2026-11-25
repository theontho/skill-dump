---
name: pr-coordinator
description: "Use when the owner asks to coordinate or land a pull request backlog across machines."
license: MIT
metadata:
  author: theontho
  version: "1.0.0"
---

# PR coordinator

Use when the owner asks to coordinate or land a pull request backlog across
machines. Keep one coordinator per project. An implementer handles one existing
PR; don't open new issues or PRs as part of a landing assignment.

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
4. Watch `pr-coord --project <project> events` for main failures, queue drops
   and questions. On notification, take action and restart the watcher. Keep
   the configuration and run commands in the handoff so another coordinator
   can resume without changing deployment files.

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
  review fixes and actual failures in one batch; only resolve a thread once
  its fix is pushed or its rationale is posted. A generic recommendation to
  get human review is not a request for changes unless project policy says
  otherwise. Stop for ownership, required approvals, policy, or source
  conflicts you can't resolve; don't stop for a busy runner.
- Run the repository's targeted local gate. Send heavy authorized tests to
  `pr-coord --project <project> rt <host> <worktree> -- <command>` or take a
  local resource lease. Remote tests copy the working tree including uncommitted
  edits to that host; check that it is the intended destination. Push once per
  review round and recertify only the final head.
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
  and never push after enqueue. Do not retry actual failures as lost runners.
- Set the dashboard row to `done` on handoff and leave a brief handoff with
  the head, gates, evidence paths, blockers and next action.

## Questions and dashboard

Agent questions go to `pr-coord --project <project> ask 'Question and context'
--pr '#N' --machine <agent> --default '<safe fallback>'`. Proceed with a safe
default or park the PR rather than asking the owner directly. The coordinator
answers via `agent-questions ID ANSWER`; only unresolved policy decisions are
rewritten with context, options and a default for `ask --owner`. The owner can
answer via the trusted dashboard (if enabled) or `answer ID ANSWER`.

`/` shows PRs, a hoverable dependency graph and conflict matrix, host leases,
runner capacity, certifications and agent status. `/lifecycle` shows where
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
