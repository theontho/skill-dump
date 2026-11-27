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
