# skill-dump
a dumping ground for various skills I make, plus the command-line tooling my agents use on every computer.

## Install on a computer (tools, skills and agent instructions, auto-updating)

```sh
curl -fsSL https://raw.githubusercontent.com/theontho/skill-dump/main/install.sh | bash
```

This clones the repo to `~/.local/share/skill-dump` and runs `skill-dump install`, which:

- links each tool in `tools/` into `~/.local/bin` and installs its systemd user units;
- links every skill in `skills/` into `~/.agents/skills` and `~/.claude/skills`;
- writes a managed block of usage notes for the installed tools into the agents' global instruction files
  (`~/.copilot/copilot-instructions.md`, `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, ... for agents that are present),
  plus `agents/hosts/<hostname>.md` when one exists for that computer;
- sets git hooks in the clone (`core.hooksPath=githooks`), so every `git pull` or checkout there reinstalls.
  There is no background update service: sync a computer with `skill-dump update` (or `git pull` in the clone).
  Services whose tool changed are restarted.

| Tool | What it does | Needs |
| --- | --- | --- |
| `agent-coord` | CPU/RAM/disk leases, named locks and messages between agents | Linux, systemd user session, python3 |
| `sudo-session` | Share a time-limited, audited sudo credential with fresh agent shells | macOS/Linux, python3, tmux, sudo |
| `op-session` | Persistent 1Password CLI session for fresh agent shells | macOS/Linux, tmux, op, jq |
| `copilot-persistent` | `copilot` wrapper that keeps interactive sessions in a tmux service | Linux, systemd user session, tmux, mise |

Tools whose requirements are missing are skipped. Choose tools with `--skip` / `--only`
(saved in `~/.config/skill-dump/config` and reused when a pull reinstalls):

```sh
curl -fsSL https://raw.githubusercontent.com/theontho/skill-dump/main/install.sh | bash -s -- --skip copilot-persistent
skill-dump status          # what is installed and when it last synced
skill-dump update          # git pull (fast-forward only) and reinstall
skill-dump uninstall
```

The installer never overwrites files it did not create; pass `--force` to move them aside
(`NAME.skill-dump-bak.TIME`). Anything removed from the repo is removed from installed computers when they next sync.
`skill-dump update` won't pull while the installed clone has local changes.

The credential helpers share one implementation per tool across macOS and Linux:
`start --terminal` opens a real approval terminal for non-interactive agents,
pending approval is preserved, and grants default to one hour. Sudo retains
command streams, working directories and active-session extension; 1Password
keepalives use the authorized foreground TTY. Run `npm run tools:test:credentials`
on either platform; the full `npm run tools:test` also needs Linux for agent-coord.

### Adopting it on a computer that already has these tools

Have an agent follow [ADOPTING.md](ADOPTING.md). It surveys the computer,
folds any newer features, fixes or platform support from the local copies back into this repo, migrates to the
managed install and verifies it.

### Adding a tool

Create `tools/<name>/` with the program, a `README.md`, and a `tool.conf`:

```sh
DESCRIPTION="One line for skill-dump list/status"
BINS="command-name=file-in-this-dir"   # space-separated; linked into ~/.local/bin
UNITS="name.service"                   # optional systemd user units, enabled and started
RESTART_ON_CHANGE="name.service"       # optional; restarted when this tool's files change
REQUIRES="python3 tmux systemctl"      # commands needed (systemctl = a systemd user session)
OS="Linux"                             # optional; uname -s values
```

Unit files may use `{{SKILL_DUMP_HOME}}`, `{{HOME}}`, `{{USER}}` and `{{HOST}}`. An optional `agents.md`
is added to the agents' global instructions on computers that install the tool. Run `npm run tools:test` and
`npm run tools:lint` before pushing.

## Use with agent skill CLIs

This repository keeps skills in the portable `skills/<name>/SKILL.md` layout and includes adapter paths for common agents:

| Agent/tool | Project path |
| --- | --- |
| Claude Code | `.claude/skills/` |
| GitHub Copilot | `.agents/skills/` |
| Codex | `.agents/skills/` |
| Cursor, Amp, Universal agents | `.agents/skills/` |

## Marketplace-style installs

Claude Code uses plugins as the installable unit for shared skills. Add this repo as a marketplace, install the bundled plugin, then reload plugins:

```text
/plugin marketplace add theontho/skill-dump
/plugin install skill-dump@theontho-skill-dump
/reload-plugins
```

Codex also uses plugins as the installable distribution unit for reusable skills. Add this repo as a plugin marketplace, then open the plugin browser and install **Skill Dump**:

```sh
codex plugin marketplace add theontho/skill-dump
codex
```

Then run `/plugins` inside Codex.

## Generic `npx skills` installs

List the skills in this repo:

```sh
npx skills add theontho/skill-dump --list --full-depth
```

Install all skills globally for Claude Code, GitHub Copilot, and Codex:

```sh
npx skills add theontho/skill-dump --skill '*' --agent claude-code --agent github-copilot --agent codex --global -y
```

Install from a local checkout:

```sh
npx skills add . --skill '*' --agent claude-code --agent github-copilot --agent codex -y
```

Local npm shortcuts are also available:

```sh
npm run skills:discover
npm run skills:list
npm run skills:install:claude
npm run skills:install:copilot
npm run skills:install:codex
npm run skills:install:all
```

The machine-readable registry is available as both `.agentskills.json` and `agentskills.json`.

## Maintaining skills

Use the repo manager so manifests, plugin metadata, hashes, and adapter symlinks stay in sync:

```sh
npm run skills:new -- my-new-skill --description "Do X when the user asks for Y."
npm run skills:sync
npm run skills:check
```

`skills:new` creates `skills/<name>/SKILL.md` and immediately regenerates `.agentskills.json`, `agentskills.json`, `.claude-plugin/`, `.codex-plugin/`, and `.agents/plugins/marketplace.json`.
