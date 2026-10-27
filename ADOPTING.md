# Adopt skill-dump on a computer

skill-dump (github.com/theontho/skill-dump) installs shared agent tooling on every computer and keeps it updated:
`tools/<name>/` programs are linked into `~/.local/bin`, their systemd user units are installed, `skills/` are linked into
`~/.agents/skills` and `~/.claude/skills`, and a managed block of usage notes is written into each agent's global
instruction file. An hourly timer (launchd on macOS) fast-forwards the installed clone and reinstalls.

Adopting it on a computer has two goals:

1. Put the computer on the managed install, replacing any hand-installed copies without losing anything.
2. **Make the repo better with what this computer has.** If this computer's copy of a tool has features, fixes or
   platform support that the repo lacks, or has general agent tooling the repo doesn't have yet, port it into the repo
   so every computer gets it. Do this before replacing local copies with the repo's versions.

## Ground rules

- The installed clone (`~/.local/share/skill-dump`) is a deployment. Never edit it, `~/.local/bin` links, rendered unit
  files or the managed instruction blocks; local changes in the clone pause its updates. Make changes in a separate dev
  checkout (for example `~/src/skill-dump`).
- Commit improvements, but **do not push without the user's approval**. Every computer pulls `main` within an hour.
- Move rather than copy-and-keep: once something is in the repo and installed, delete the old copy. Don't leave
  compatibility symlinks. Back up configuration before deleting it.
- Read `README.md` and `AGENTS.md` in the repo first. They describe `tool.conf`, placeholders and the validation commands.

## 1. Survey the computer

Record what you find. Change nothing yet.

- Platform: `uname -s`, `uname -n`, whether a systemd user session works (`systemctl --user show-environment`), and
  which of `python3 tmux mise sudo git` exist. Compare with each tool's `REQUIRES`/`OS` in `tools/*/tool.conf`.
- Existing copies: `type -a skill-dump agent-coord sudo-session op-session copilot`. List `~/.local/bin`,
  `~/.local/libexec`, `~/.config/systemd/user/` and `~/Library/LaunchAgents/`. Follow wrappers to their real
  sources (for example `exec python3 /some/dir/tool.py`), which may live in a project folder.
- Agent instructions: `~/.copilot/copilot-instructions.md`, `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
  `~/.gemini/GEMINI.md`, `~/.config/opencode/AGENTS.md` and any other global instruction files. Note any sections that
  duplicate `tools/*/agents.md`, and any machine-specific rules (storage, VMs, hardware).
- Skills already in `~/.agents/skills` and `~/.claude/skills` with the same names as repo skills.
- Live state that must survive: `agent-coord status` (leases, locks, waiters), `sudo-session status`,
  `op-session status`, and tmux sessions on `tmux -L copilot-persistent ls`.
- Other general agent tooling that would help on every computer (launchers, credential helpers, coordination
  scripts). Leave out project-specific services and things another app manages (for example herdr hooks).

## 2. Compare each local copy with the repo

For each tool, `diff` the local source against `tools/<name>/` and classify it:

| Finding | Action |
| --- | --- |
| Identical, or the repo is newer | Replace it with the managed install. |
| The local copy has features or fixes the repo lacks | Port them into the repo (step 3), then replace it. |
| The local copy has platform adaptations (macOS, no systemd, other distro paths) | Port them as platform support (step 3). |
| A local-only general tool | Add it as a new `tools/<name>/` (step 3), or list it for the user. |
| Machine-specific instructions or rules | Put them in `agents/hosts/<short-hostname>.md`. |

When the two versions differ in behavior, keep both behaviors where possible. Ask the user when they conflict.

## 3. Improve the repo

Work in the dev checkout:

- Port features and fixes into `tools/<name>/` and add focused tests to the tool's existing test file.
- Generalize as you port. No hard-coded users, hosts or home paths: use `{{HOME}}`, `{{USER}}`, `{{HOST}}` and
  `{{SKILL_DUMP_HOME}}` in units and `agents.md`, and environment variables or runtime detection in code.
- Platform support: widen `OS`/`REQUIRES` in `tool.conf` only when the tool really works there. Keep scripts portable:
  `bin/skill-dump` must run on macOS bash 3.2 and BSD userland (no `mapfile`, associative arrays or GNU-only flags
  without fallbacks). If the installer lacks something this computer needs (another service manager, a different
  instruction file, a different bin directory), add it to `bin/skill-dump` behind detection, not by default.
- New tools: create `tools/<name>/` with the program, `README.md`, `tool.conf` and, if agents should know about
  it, `agents.md`. Services restart on change only if they are listed in `RESTART_ON_CHANGE`. Never restart anything
  whose restart kills user sessions.
- Update `README.md` (tool table and requirements) and `AGENTS.md` when behavior or layout changes.
- Validate:
  - `npm run tools:test`, `npm run tools:lint` and `npm run skills:check` (run `npm run skills:sync` after skill
    changes).
  - Run a sandbox install that cannot touch the real computer. Set `HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME` and
    `XDG_STATE_HOME` to a temporary directory, and `unset XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS` so the real
    systemd user manager is unreachable.
  - Test the update path with a bare clone as origin:
    `SKILL_DUMP_REPO=$T/origin.git bash install.sh`, push a change to `$T/origin.git`, then `skill-dump update`.
- Commit with a clear message, report the changes to the user, and ask before pushing. If the computer's install
  needs these changes, wait until they are pushed before step 4.

## 4. Install and migrate

1. Check `agent-coord status` for exclusive or timed runs. Installing restarts `agent-coord` when its files change.
   Clients retry for two minutes, so leases survive, but avoid restarting it in the middle of a timed run.
2. Remove the old unmanaged copies the managed install replaces: the `~/.local/bin` entries, helper files in
   `~/.local/libexec`, and old unit files with the same names. The installer refuses to overwrite files it didn't
   create. `--force` moves them aside as `NAME.skill-dump-bak.TIME` instead. Prefer deleting once step 2 confirmed
   nothing is lost.
3. Remove sections of the agent instruction files that the managed block now covers. Keep everything else.
4. Install:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/theontho/skill-dump/main/install.sh | bash
   # add: -s -- --skip copilot-persistent   (or --only ..., --no-skills, --no-agent-instructions)
   ```

   Ask before installing tools that replace a command the user already uses differently, such as the `copilot`
   wrapper.

## 5. Verify

- `skill-dump status`: correct commit, tools installed, timer enabled with a next run time.
- `systemctl --user is-active agent-coord copilot-tmux skill-dump-update.timer` (only for installed tools). Unit
  `ExecStart` lines point into `~/.local/share/skill-dump`.
- Each command works: `agent-coord status` still shows its earlier leases, `sudo-session status`, `op-session status`
  and `copilot --version`.
- Each instruction file contains exactly one skill-dump block and no duplicated old sections. Skills are linked.
- `skill-dump update` reports `up to date`.

## 6. Clean up and report

- Delete the old source folders and helpers that are now in the repo, stale `*.bak` copies of replaced tools, and
  dead helpers they replaced. Ask first about anything you can't confirm is obsolete.
- Report to the user:
  - what was installed and what was skipped (and why);
  - what moved or was deleted, and where backups are;
  - what you improved in the repo (features, fixes, platform support, new tools, host notes), and whether it is
    pushed;
  - other tooling worth adopting later.
