# Wait outside the model (agent-wait)

- For a wait with no decisions to make, launch **one attached async `agent-wait` command**, then yield the turn. Use the completion notification to resume and read the result once. Don't loop on `read_bash`, GitHub status, or `sleep` in model turns. Don't run a shell polling loop that can last more than two minutes, regardless of how short each `sleep` is.
- Always choose a hard `--timeout` and leave the watcher attached to its owner. Use `--owner-pid` for the real owning process if an async shell wrapper could outlive it; otherwise put `exec` before `agent-wait` so the watcher keeps the launching process's parent as its owner. Never make a disposable shell PID the owner of a handoff.
- `agent-coord status` lists Linux watchers with owner and expiry. For a handoff, name a living owner and use `--as WATCHER --notify TARGET` to deliver the same result to the receiving agent's inbox. Exit `124` means timeout, `125` means the owner exited, and `130` means interrupted. macOS can watch locally without agent-coord; in that case use the async command's result rather than an inbox.

Examples (launch each as its own async command):

```sh
exec agent-wait --timeout 2h run 23456789012 -R owner/repo
exec agent-wait --timeout 1h pr 42 -R owner/repo --checks
exec agent-wait --timeout 30m systemd project-build.service
exec agent-wait --timeout 30m launchd "gui/$(id -u)/org.example.build"
exec agent-wait --timeout 6h --owner-pid "$supervisor_pid" --as build-watch --notify supervisor file out/result.json
```
