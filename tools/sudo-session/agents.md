# Privileged sessions (sudo-session)

- For repeated trusted administrative work, authenticate in a private SSH terminal with `ssh -t {{USER}}@{{HOST}} sudo-session start --ssh`, then detach with Ctrl-b, d. This retains a normal audited sudo credential in a dedicated tmux PTY (default one hour), without a desktop popup or a passwordless sudoers grant. A user-requested longer session can use `start --duration 12h` or `extend --duration 12h` for at least twelve hours remaining; extension uses the active credential and cannot revive an expired session. Shortening a lifetime requires stopping and authenticating again. Never request or send the password through agent chat. `sudo-session start` also works directly in a private local terminal. Run `sudo-session --help` for the full command reference.
- Run privileged commands from any fresh agent shell with `sudo-session run -- COMMAND [ARG ...]` (or `sudo-session COMMAND [ARG ...]`). Do not invoke mutating privileged commands bare.
- Check or revoke access with `sudo-session status` and `sudo-session stop`.
- Keep untrusted downloaded code outside privileged sessions and never execute it through `sudo-session`.
