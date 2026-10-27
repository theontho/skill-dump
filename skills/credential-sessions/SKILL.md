---
name: credential-sessions
description: "Use for op-session or sudo-session authentication, persistent credential grants across fresh agent shells, repeated 1Password approval prompts, authorization loss, and credential-helper diagnosis or installation."
license: MIT
metadata:
  author: theontho
  version: "1.0.0"
---

# Persistent credential sessions

Use the installed `op-session` and `sudo-session` helpers on macOS and Linux.
Their canonical sources and tests are `tools/op-session/` and
`tools/sudo-session/` in the skill-dump repository. Read its `AGENTS.md` before
changing them. The clone at `~/.local/share/skill-dump` is a deployment; edit a
development checkout, not installed links or the deployment.
Never capture or reuse an administrator or 1Password account password.

## One grant for related work

1. Check `op-session status` or `sudo-session status` before authenticating.
   Exit 0 means a local lease is active, 2 means approval is already pending,
   and 1 means inactive. Resume pending approval without stopping or restarting it.
2. If inactive, run `op-session start` or `sudo-session start` once in an
   interactive terminal. From a non-interactive harness, add `--terminal` to
   open a real terminal for the person's approval. Do not emulate a terminal
   inside a short-lived harness: its process cleanup may revoke the grant.
3. Default leases last one hour. Longer leases or sudo extensions require
   explicit authorization. `sudo-session extend --duration 12h` can extend an
   active grant without another prompt, but cannot revive an expired one.
4. Route every 1Password CLI call, including scripts, through
   `op-session OP_ARGUMENT ...`. The legacy `op-session exec op ...` form also
   works. Route every privileged command through `sudo-session COMMAND ...`.
   Do not rely on environment variables or sudo timestamps surviving shells.
5. Verify actual 1Password reuse with two non-secret `op-session whoami` calls
   from separate fresh shells; status alone does not prove vault access.
6. Keep the same grant throughout related work, side questions and waits.
   Stop once at the end, not between queries.

## macOS desktop integration

Enable **Keep 1Password in the menu bar** in General settings and leave the app
running. Closing its last window with that option off can quit the app and
immediately revoke CLI access. Close or hide the window rather than quitting.

Desktop authorization is bound to the terminal's TTY and start time, expires
after ten idle minutes, refreshes on use, and has a twelve-hour hard limit.
Local leases do not override app lock, quit, restart or authorization policy.
Do not disable security locks to make a grant appear persistent.

## When reuse fails

Inspect stderr, status, and the credential-safe files under
`~/.local/state/copilot-agent-sessions/`: `audit.log` and `op/auth-status`.
Never print secrets as diagnostics or repeatedly restart sign-in.
Distinguish pending approval, local expiry, app lock or exit, and helper
failure. On macOS, inspect only relevant app lifecycle/error log lines.

1Password keepalives must run through the authorized foreground pane.
Background jobs normally lose stdin's TTY. The dispatch lock must cover the
entire command, not just keystroke delivery. Failures must be recorded rather
than leaving stale `authenticated` status.

## Changing helpers

Preserve installed fixes when merging sources. Run isolated tests with fake
credentials before requesting real approval:

```sh
npm run tools:test:credentials
npm run tools:lint
npm run skills:check
```

`npm run tools:test` additionally exercises the Linux-only coordinator.
Tests cover same-TTY keepalives and fresh-shell requests, pending approval,
revocation without another sign-in, expiry, command streams and sudo extension.
Install committed changes with `skill-dump update` after they are pushed.
After a real authentication fix, verify actual reuse; mocked tests and lease
status alone are not proof that desktop authorization works.

Source: https://developer.1password.com/docs/cli/app-integration-security/
