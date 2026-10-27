# copilot-persistent

`copilot` wrapper that runs interactive Copilot CLI sessions inside the
`copilot-tmux.service` user service (tmux socket `copilot-persistent`), one
session per working directory. Logging out of the desktop or restarting the
display manager does not end them; run `copilot` again in the same directory
to reattach.

Non-interactive uses (`-p`, `--help`, `--acp`, pipes, `copilot update`, ...)
go straight to `copilot-real`, which runs the mise-managed `copilot` with
`--remote`. Requires `tmux`, `mise` and a systemd user session.

Updates never restart `copilot-tmux.service`, since that would end every
running session.
