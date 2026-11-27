After two failures with the same command, inputs and error class, stop retrying
unchanged. Diagnose or change approach (partial repair, another source or host,
smaller repro), or escalate with both logs. `pr-coord` refuses a third identical
`cert`, `rt` or train attempt and points to both logs. Rerun CI only for confirmed
infrastructure loss; file a flake issue for any rerun-only pass.

For explicit multi-machine coordination (landing a PR backlog, then working
the issue queue across machines), use the `pr-coordinator` skill and
`pr-coord --project PROJECT status`. After the backlog lands, keep going on the
issue queue and keep every machine staffed; an empty PR list is not the end. Do not start landing automation
without owner authorization. Keep checkout, host, and CI policy in the private
project config; keep snapshots and logs in the separate state directory.

Start every supervisor turn with `pr-coord --project PROJECT agent supervisor
heartbeat`; add `--owner-resume` if the owner had to nudge you after a long gap.
Hand waits to a watcher (`events --wait` for coordinator alerts), then take the
next item. End a turn only if every managed host has an agent, lease, or
registered unattended run with a failure alert, or you've verified that no
eligible work remains and reported which sources and filters you checked.

Before each push, sweep related code/tests for every instance of each review
finding, run nearest tests for changed files (`pr-coord nearest-tests` or
manual selection), and self-review the whole diff. Bound command timeouts;
batch all threads and CI fixes into one push per round. Escalate at three
Copilot rounds instead of repeating the loop.

Base new PRs on the project's base branch or deliberately register a native
stack when they depend on an unmerged PR. Hand stack members to the train from
the bottom up; never enable auto-merge on them.

Queue numbered decisions with `coord-remote ask` or `pr-coord ask`: give context,
options with consequences, a recommendation, a default and what it blocks.
Continue on a reversible default or park only the affected action and do other
work. Reserve blocking prompts for essential irreversible or owner-only decisions.
