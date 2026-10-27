# Sudo sessions on macOS and Linux

Use `sudo-session --help` for a complete command reference, security scope,
examples, and exit status information. `sudo-session start --help` documents
authentication options.

Authenticate from a private SSH terminal, not Copilot chat:

```sh
ssh -t USER@HOST sudo-session start --ssh
```

Enter the sudo password at sudo's terminal prompt. Once it reports an active
session, press **Ctrl-b**, then **d** to detach. SSH can then disconnect.
Use the host's reachable name or address for `USER@HOST`.

From a non-interactive agent harness, open a real terminal for approval:

```sh
sudo-session status
sudo-session start --terminal
```

macOS uses Terminal.app. On Linux, set `AGENT_SESSION_TERMINAL` to a launcher
such as `alacritty -e`. `AGENT_SESSION_TERMINAL_WAIT` controls the approval wait
(default 300 seconds). Status exits 0 when active, 2 when approval is pending,
or 1 when stopped. Resume pending approval; do not restart it.

## Session lifetime

Sessions default to **one hour**. Request **twelve hours** when authenticating:

```sh
sudo-session start --duration 12h
# Or remotely:
ssh -t USER@HOST sudo-session start --ssh --duration 12h
```

Durations accept positive whole numbers with `s`, `m`, `h`, or `d` suffixes
(seconds, minutes, hours, days). Bare numbers mean seconds. Examples: `90s`,
`30m`, `12h`, `1d`. `-d` is short for `--duration` (e.g. `sudo-session start -d 24h`).

To give an active session **at least twelve hours remaining from now**:

```sh
sudo-session extend --duration 12h
```

This uses the current valid sudo credential without another password prompt,
logs the change, and never shortens an already-longer session. It cannot revive
a stopped or expired session. Starting an already-active session does **not**
reset or extend its deadline; use `extend` explicitly. To shorten a lifetime,
run `sudo-session stop`, then authenticate again with the desired `--duration`.
Stopping does not terminate already-running commands. Longer lifetimes let other
processes running as your user use the session for longer, too. No lifetime is
changed automatically.

Sessions started before extension support was installed need a one-time stop
and fresh authentication; new sessions accept `extend`.

## Running commands and revoking access

Fresh agent shells can use:

```sh
sudo-session status
sudo-session extend --duration 12h
sudo-session run -- id -u
sudo-session exec COMMAND ARGUMENTS
sudo-session stop
```

`sudo-session COMMAND ARGUMENTS` also works. Arguments, working directory, standard
input, standard output, standard error, and command exit status are preserved.
Commands run through normal noninteractive sudo on the authenticated terminal.
Sudo's configured command restrictions and auditing still apply.

The dedicated tmux terminal retains a normal sudo credential for the requested
lifetime, refreshing it every 30 seconds while the session is active. The password
is not sent to the agent, placed in arguments, saved to a file, or read by this
helper. Only sudo reads it. The local control socket is restricted to this user;
other processes running as this user share access to the authenticated session.
Stopping or expiry invalidates the credential and rejects new commands, but
does not kill commands already running.

Bare `sudo` in unrelated terminals is not unlocked. Reboot or loss of the
authentication terminal ends the session. Reattach with
`tmux -L sudo-session attach -t auth`; detach with Ctrl-b, d instead of Ctrl-c
to keep it alive. Lifecycle records are in
`~/.local/state/copilot-agent-sessions/audit.log`, and additionally
`journalctl -t sudo-session` on Linux.

The command broker uses framed Unix stream sockets and descriptor passing on
both systems, preserving separate stdin/stdout/stderr without shell evaluation.
Existing Linux packet-socket sessions remain usable during upgrades.
`XDG_RUNTIME_DIR` selects its runtime root; otherwise Linux uses `/run/user/UID`
and macOS uses the private state directory. `SUDO_SESSION_TMUX_SOCKET` isolates
test sessions.

This replaces the previous desktop `pkexec` helper and does not create a
passwordless sudoers rule. If the old helper's expiry timer is active, startup
removes its passwordless grant and requires fresh terminal authentication.
Its existing expiry timer can safely finish later. `start` without `--ssh`
uses the same terminal-based authentication and requires a real terminal.

Source: `sudo_session.py`. Installed as `~/.local/bin/sudo-session` by `skill-dump install`.
No desktop session or browser authentication endpoint is required.
