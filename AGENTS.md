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
