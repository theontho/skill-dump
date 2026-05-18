---
name: ghostty-theme-tools
description: "Transform Ghostty theme colors, apply Ghostty/cmux/Terminal.app light/dark themes, and convert Ghostty themes into VS Code/Cursor/VSCodium color customizations. WHEN: Ghostty theme, VS Code theme from Ghostty, terminal theme, cmux theme, Apple Classic Black, red night tint, color palette transform."
license: MIT
metadata:
  author: theontho
  version: "1.0.0"
---

# Ghostty Theme Tools

Use this skill when working with Ghostty-compatible theme files, terminal theme switching, or VS Code color customizations derived from Ghostty palettes.

## Included scripts

- `scripts/transform-ghostty-theme.py` — transforms every `#RRGGBB` color in a Ghostty theme using a preset or custom RGB channel formulas.
- `scripts/ghostty-theme-to-vscode.py` — converts a Ghostty theme into VS Code-compatible workbench, TextMate, and semantic token color customizations.
- `scripts/set-terminal-themes.py` — sets light/dark themes across Ghostty config files, cmux, and macOS Terminal.app.

## Theme resources

- `themes/Apple Classic Black` — Ghostty theme file.
- `themes/Apple Classic Black.vscode-color-theme.json` — standalone VS Code color theme generated from the current Apple Classic Black customizations.

## Common workflows

### Preview a Ghostty color transform

```bash
python3 .agents/skills/ghostty-theme-tools/scripts/transform-ghostty-theme.py \
  ".agents/skills/ghostty-theme-tools/themes/Apple Classic Black" \
  --preset soft-red-night \
  --preview
```

Use `--no-render` if ANSI color swatches make the output hard to read.

### Write a transformed Ghostty theme

```bash
python3 .agents/skills/ghostty-theme-tools/scripts/transform-ghostty-theme.py \
  input-theme \
  output-theme \
  --preset soft-red-night
```

For in-place updates, use `--in-place`; the script creates `INPUT.bak` by default. Use `--no-backup` only when the user explicitly does not want a backup.

### Convert a Ghostty theme to VS Code settings customizations

Preview the patch first:

```bash
python3 .agents/skills/ghostty-theme-tools/scripts/ghostty-theme-to-vscode.py \
  ".agents/skills/ghostty-theme-tools/themes/Apple Classic Black" \
  --preview
```

Apply it to VS Code:

```bash
python3 .agents/skills/ghostty-theme-tools/scripts/ghostty-theme-to-vscode.py \
  ".agents/skills/ghostty-theme-tools/themes/Apple Classic Black" \
  --app code
```

Use `--app cursor`, `--app insiders`, or `--app codium` for those editors. Use `--settings PATH` when the user provides a specific settings file. Add `--no-set-theme` to avoid changing `workbench.colorTheme`.

### Use the standalone VS Code theme file

Copy or reference:

```text
.agents/skills/ghostty-theme-tools/themes/Apple Classic Black.vscode-color-theme.json
```

This file is a complete VS Code color theme JSON. It can be placed in a VS Code extension theme contribution or imported by tooling that accepts `.vscode-color-theme.json`.

### Set terminal themes everywhere

```bash
python3 .agents/skills/ghostty-theme-tools/scripts/set-terminal-themes.py \
  --light "Claude Code Light" \
  --dark "Apple Classic Black"
```

This updates Ghostty config locations, optionally uses `cmux themes set`, reloads cmux, and syncs a macOS Terminal.app profile from the dark Ghostty theme.

Use safety flags when needed:

```bash
python3 .agents/skills/ghostty-theme-tools/scripts/set-terminal-themes.py \
  --dark "Apple Classic Black" \
  --no-terminal \
  --no-cmux-cli \
  --no-reload
```

## Notes

- Scripts use only the Python standard library except Terminal.app syncing, which requires PyObjC/AppKit on macOS.
- `transform-ghostty-theme.py` supports custom formulas with variables `r`, `g`, and `b` in 0-255 channel space.
- Prefer preview/dry-run modes before modifying user settings or terminal profiles.
