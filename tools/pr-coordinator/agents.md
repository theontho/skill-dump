For explicit multi-machine coordination (landing a PR backlog, then working
the issue queue across machines), use the `pr-coordinator` skill and
`pr-coord --project PROJECT status`. After the backlog lands, keep going on the
issue queue and keep every machine staffed; an empty PR list is not the end. Do not start landing automation
without owner authorization. Keep checkout, host, and CI policy in the private
project config; keep snapshots and logs in the separate state directory.
