# Agent Skills

This repo's canonical skill source is `skills/`.

Agent-specific paths are adapters only:

- `.agents/skills` points at `skills/` for GitHub Copilot, Codex, Cursor, Amp, and other agents that use the shared agent-skills path.
- `.claude/skills` points at `skills/` for Claude Code.
- `.agentskills.json` and `agentskills.json` expose the same skill registry metadata for tools that prefer a manifest.
- `.claude-plugin/` packages the repo as a Claude Code plugin marketplace entry.
- `.codex-plugin/` and `.agents/plugins/marketplace.json` package the repo as a Codex plugin marketplace entry.

When adding a skill, run:

```sh
npm run skills:new -- my-skill --description "Use when ..."
```

When changing a skill, edit `skills/<skill-name>/SKILL.md`, then run:

```sh
npm run skills:sync
```

Use `npm run skills:check` to verify manifests, plugin metadata, hashes, and adapter symlinks are current.

# Machine tooling

`tools/<name>/` holds command-line tools (agent-coord, sudo-session, op-session, copilot-persistent) that `bin/skill-dump install` links into `~/.local/bin` on every computer; `install.sh` bootstraps a clone at `~/.local/share/skill-dump` with an hourly auto-update timer. Each tool declares its commands, units and requirements in `tools/<name>/tool.conf`, and may add global agent guidance in `tools/<name>/agents.md` (host-specific notes go in `agents/hosts/<hostname>.md`). Never edit installed copies in `~/.local/bin`, `~/.config/systemd/user` or the agents' instruction files; change the repo and push. Verify with `npm run tools:test` and `npm run tools:lint`.
