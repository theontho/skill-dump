# op-session

Keeps a time-limited 1Password CLI grant in an authenticated tmux pane on macOS
and Linux, so fresh agent shells can reuse it without another sign-in.

```sh
op-session status                # 0 active, 2 approval pending, 1 inactive
op-session start                 # authenticate once in an interactive terminal
op-session start --terminal      # open a real terminal from an agent harness
op-session extend -d 12h         # at least 12h remaining; starts if inactive
op-session vault list            # ordinary op arguments
op-session run -- vault list     # explicit op-argument form
op-session exec op vault list    # backward-compatible verbatim command form
op-session stop
```

Default leases last one hour. Explicitly authorized longer leases accept
`-d`/`--duration` with `s`, `m`, `h` or `d` (e.g. `30m`, `12h`, `1d`).
`start --duration` on an active grant extends it to at least that long from
now, and `extend` on an inactive grant starts one; neither shortens a grant,
and `start` without `--duration` leaves an active grant unchanged. If the
1Password desktop app is unreachable (e.g. it exited after a package upgrade),
`start` launches it (`AGENT_SESSION_OP_APP` overrides the launcher).

Over SSH (or with `--cli`), `start` skips the desktop app and signs in with the
account password in the terminal (`OP_BIOMETRIC_UNLOCK_ENABLED=false`). The
first time, it runs `op account add`, which asks for the sign-in address,
email, Secret Key and password. `--app` forces desktop-app approval. `status` never cancels pending approval; resume its existing
terminal rather than opening another. `shell` attaches to the authorized pane.

The worker selects a configured account, signs in once, validates it, and
dispatches keepalives through the same foreground TTY. Commands are serialized
until completion, with quoted arguments, working directory and exit status
preserved. Command output is returned and deleted, not retained as diagnostics.
Expiry or failed keepalive ends the session and records the reason without
starting another sign-in. State and credential-safe audit records live in
`~/.local/state/copilot-agent-sessions/`.

On macOS, enable **Keep 1Password in the menu bar** and leave the app running.
Desktop lock, quit or restart can revoke authorization before the local lease
ends. Neither keepalive nor longer leases override desktop security policy.
Verify reuse with two non-secret `op-session whoami` calls in separate shells.

`AGENT_SESSION_STATE_HOME`, `AGENT_SESSION_TMUX_SOCKET` and
`AGENT_SESSION_WORKER` isolate tests. `AGENT_SESSION_TERMINAL` selects a
launcher such as `alacritty -e`; macOS defaults to Terminal.app. Terminal
approval waits up to `AGENT_SESSION_TERMINAL_WAIT` seconds (default 300),
returning 2 if still pending. The old unbounded tmux session and
`OP_SESSION_NAME`/`OP_SESSION_TIMEOUT` settings are replaced by audited leases;
requests hold the dispatch lock until completion.

Run `npm run tools:test:credentials` for fake-credential PTY regression tests.
