# agent-wait

Wait for one event in an attached background process rather than polling through
an agent's model. The tool prints one result line and exits.

```text
agent-wait --timeout D [--interval D] [--owner-pid PID] [--as NAME --notify TARGET] CONDITION
```

Durations accept seconds (the default unit), `s`, `m`, `h`, and `d`, up to 30 days.
The default interval is ten seconds for GitHub and one second for local probes.

| Condition | Finishes when |
| --- | --- |
| `run ID [-R OWNER/REPO]` | The GitHub Actions run completes. Only `success` exits zero, like `gh run watch --exit-status`. |
| `pr NUMBER [-R OWNER/REPO] [--state STATE]` | The PR is merged or closed, or reaches the requested exact `OPEN`, `CLOSED`, or `MERGED` state. |
| `pr NUMBER [-R OWNER/REPO] --checks` | At least one check is reported and none are pending. Failed or cancelled checks exit nonzero; skipped checks are neutral. |
| `systemd UNIT` | A user unit becomes inactive, failed, or unloads after being seen. A failed unit exits nonzero; an unknown unit on the first probe is an error. |
| `launchd DOMAIN/LABEL` | The qualified launchd job is stopped or unloaded. A stopped job's nonzero last exit code is reported as failure. |
| `pid PID` | The process exits, including an unreaped exit. An already gone PID is done. |
| `pidfile PATH` | The file contains a PID and that process exits. The first PID read is tracked even if the file is later removed or replaced. |
| `lease L123` | The compute lease is neither queued nor active. A lease already absent is done. |
| `file PATH` | The path exists. |

Exit codes: `0` satisfied, `1` unsuccessful terminal result, `2` usage or probe
error, `124` deadline, `125` orphaned, `130` interrupted. The timeout covers
registration and all probes; an optional inbox delivery gets at most one
additional second. A notification failure is reported on stderr and turns an
otherwise successful result into exit `2`.

On Linux, the helper requires the local agent-coord service with watcher support.
Registration does not claim compute. The coordinator records `watch_registered`
and `watch_ended` in its history and stops an expired or orphaned watcher.
The helper also enforces both checks locally on Linux, macOS, and native Windows.
Lease waits and inbox delivery are Linux-only. Use `systemd` on Linux and
`launchd` on macOS. GitHub waits require authenticated `gh` (`gh.exe` on Windows).
On native Windows, run `python tools/agent-wait/agent-wait --timeout 30s file PATH`
from the checkout; a timed-out or orphaned watcher uses Windows process-tree
termination for a running probe. To run just the portable waiter tests from the
repository root, use `python -m unittest discover -s tools/agent-wait`.

The default owner is the parent PID. Set `--owner-pid`
when the command runner's parent is not the actual owner. Use `--as NAME` on
its own to set the registration name, or with `--notify TARGET` to send the
result to a different inbox.
