# skill-dump
a dumping ground for various skills I make

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
