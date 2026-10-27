# Persistent Copilot sessions

- Launch interactive Copilot CLI sessions with `copilot`. The wrapper runs them in the `copilot-tmux.service` user service and reattaches by working directory, so desktop logout or display-manager restart does not terminate them.
- Copilot sessions use `--remote` by default. Reopen a terminal in the same directory and run `copilot` to reattach.
