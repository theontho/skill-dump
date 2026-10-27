# op-session

Keeps one 1Password CLI shell alive in a tmux session (`copilot-op`) so fresh
agent shells can reuse its `op` sign-in instead of prompting each time.

```sh
op-session start                 # create the session (sign in there once if needed)
op-session exec op vault list    # run a command in it; output and exit status come back
op-session status                # running | stopped
op-session stop
```

Commands are serialized with a lock and time out after `OP_SESSION_TIMEOUT`
seconds (default 120). `OP_SESSION_NAME` changes the tmux session name.
