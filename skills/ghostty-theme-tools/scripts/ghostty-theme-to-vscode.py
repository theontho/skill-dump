#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ANSI_COLOR_KEYS = {
    0: "terminal.ansiBlack",
    1: "terminal.ansiRed",
    2: "terminal.ansiGreen",
    3: "terminal.ansiYellow",
    4: "terminal.ansiBlue",
    5: "terminal.ansiMagenta",
    6: "terminal.ansiCyan",
    7: "terminal.ansiWhite",
    8: "terminal.ansiBrightBlack",
    9: "terminal.ansiBrightRed",
    10: "terminal.ansiBrightGreen",
    11: "terminal.ansiBrightYellow",
    12: "terminal.ansiBrightBlue",
    13: "terminal.ansiBrightMagenta",
    14: "terminal.ansiBrightCyan",
    15: "terminal.ansiBrightWhite",
}


@dataclass(frozen=True)
class GhosttyTheme:
    colors: dict[str, str]
    palette: dict[int, str]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "ghostty-theme"


def parse_hex(value: str) -> str | None:
    text = value.strip().split()[0].lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return f"#{text.lower()}"
    return None


def parse_ghostty_theme(path: Path) -> GhosttyTheme:
    colors: dict[str, str] = {}
    palette: dict[int, str] = {}

    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = [part.strip() for part in line.split("=", 1)]
        if key == "palette":
            index_text, separator, color_text = value.partition("=")
            if not separator:
                continue
            try:
                index = int(index_text.strip())
            except ValueError:
                continue
            color = parse_hex(color_text)
            if color is not None:
                palette[index] = color
            continue

        color = parse_hex(value)
        if color is not None:
            colors[key] = color

    return GhosttyTheme(colors=colors, palette=palette)


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def rgb_to_hex(red: int, green: int, blue: int) -> str:
    return f"#{red:02x}{green:02x}{blue:02x}"


def clamp_channel(value: float) -> int:
    return max(0, min(255, round(value)))


def blend(left: str, right: str, amount: float) -> str:
    left_rgb = hex_to_rgb(left)
    right_rgb = hex_to_rgb(right)
    channels = [
        clamp_channel(left_channel * (1 - amount) + right_channel * amount)
        for left_channel, right_channel in zip(left_rgb, right_rgb, strict=True)
    ]
    return rgb_to_hex(*channels)


def with_alpha(color: str, alpha: int) -> str:
    return f"{color}{alpha:02x}"


def color(theme: GhosttyTheme, key: str, fallback: str) -> str:
    return theme.colors.get(key, fallback)


def palette(theme: GhosttyTheme, index: int, fallback: str) -> str:
    return theme.palette.get(index, fallback)


def build_vscode_theme(theme: GhosttyTheme, name: str) -> dict[str, object]:
    background = color(theme, "background", palette(theme, 0, "#000000"))
    foreground = color(theme, "foreground", palette(theme, 7, "#c7c7c7"))
    cursor = color(theme, "cursor-color", palette(theme, 15, foreground))
    selection = color(theme, "selection-background", palette(theme, 4, "#264f78"))
    red = palette(theme, 1, "#cd3131")
    green = palette(theme, 2, "#0dbc79")
    yellow = palette(theme, 3, "#e5e510")
    blue = palette(theme, 4, "#2472c8")
    magenta = palette(theme, 5, "#bc3fbc")
    cyan = palette(theme, 6, "#11a8cd")
    white = palette(theme, 7, "#e5e5e5")
    bright_black = palette(theme, 8, "#666666")
    bright_red = palette(theme, 9, red)
    bright_green = palette(theme, 10, green)
    bright_yellow = palette(theme, 11, yellow)
    bright_blue = palette(theme, 12, blue)
    bright_magenta = palette(theme, 13, magenta)
    bright_cyan = palette(theme, 14, cyan)
    bright_white = palette(theme, 15, white)

    surface = blend(background, foreground, 0.10)
    surface_alt = blend(background, foreground, 0.16)
    border = color(theme, "split-divider-color", blend(background, bright_black, 0.75))

    colors: dict[str, str] = {
        "foreground": foreground,
        "descriptionForeground": blend(foreground, background, 0.22),
        "errorForeground": bright_red,
        "focusBorder": border,
        "contrastBorder": border,
        "activityBar.background": background,
        "activityBar.foreground": bright_white,
        "activityBar.inactiveForeground": bright_black,
        "activityBarBadge.background": bright_red,
        "activityBarBadge.foreground": background,
        "badge.background": surface_alt,
        "badge.foreground": foreground,
        "breadcrumb.background": background,
        "button.background": blend(background, red, 0.55),
        "button.foreground": bright_white,
        "button.hoverBackground": blend(background, bright_red, 0.65),
        "dropdown.background": surface,
        "dropdown.border": border,
        "dropdown.foreground": foreground,
        "editor.background": background,
        "editor.foreground": foreground,
        "editorCursor.foreground": cursor,
        "editorGroup.border": border,
        "editorGroupHeader.tabsBackground": background,
        "editorIndentGuide.background1": with_alpha(bright_black, 0x40),
        "editorIndentGuide.activeBackground1": with_alpha(border, 0x90),
        "editorLineNumber.foreground": bright_black,
        "editorLineNumber.activeForeground": bright_yellow,
        "editorRuler.foreground": with_alpha(border, 0x60),
        "editor.selectionBackground": with_alpha(selection, 0xb0),
        "editor.inactiveSelectionBackground": with_alpha(selection, 0x66),
        "editor.lineHighlightBackground": with_alpha(surface_alt, 0x55),
        "editor.wordHighlightBackground": with_alpha(yellow, 0x28),
        "editor.wordHighlightStrongBackground": with_alpha(bright_yellow, 0x38),
        "editor.findMatchBackground": with_alpha(bright_yellow, 0x55),
        "editor.findMatchHighlightBackground": with_alpha(yellow, 0x33),
        "editorBracketMatch.background": with_alpha(bright_magenta, 0x22),
        "editorBracketMatch.border": bright_magenta,
        "editorGutter.background": background,
        "editorWidget.background": surface,
        "editorWidget.border": border,
        "input.background": surface,
        "input.border": border,
        "input.foreground": foreground,
        "input.placeholderForeground": bright_black,
        "list.activeSelectionBackground": with_alpha(selection, 0x90),
        "list.activeSelectionForeground": bright_white,
        "list.focusBackground": with_alpha(selection, 0x66),
        "list.hoverBackground": with_alpha(surface_alt, 0x88),
        "list.inactiveSelectionBackground": with_alpha(selection, 0x55),
        "menu.background": surface,
        "menu.foreground": foreground,
        "menu.selectionBackground": with_alpha(selection, 0x88),
        "panel.background": background,
        "panel.border": border,
        "panelTitle.activeBorder": bright_red,
        "peekView.border": border,
        "peekViewEditor.background": surface,
        "peekViewResult.background": background,
        "peekViewTitle.background": surface,
        "sideBar.background": background,
        "sideBar.foreground": foreground,
        "sideBar.border": border,
        "sideBarSectionHeader.background": surface,
        "sideBarTitle.foreground": bright_white,
        "statusBar.background": surface,
        "statusBar.foreground": foreground,
        "statusBar.debuggingBackground": red,
        "statusBar.debuggingForeground": bright_white,
        "statusBar.noFolderBackground": background,
        "tab.activeBackground": background,
        "tab.activeBorderTop": bright_red,
        "tab.activeForeground": bright_white,
        "tab.border": border,
        "tab.inactiveBackground": surface,
        "tab.inactiveForeground": blend(foreground, background, 0.25),
        "terminal.background": background,
        "terminal.foreground": foreground,
        "terminalCursor.foreground": cursor,
        "titleBar.activeBackground": background,
        "titleBar.activeForeground": bright_white,
        "titleBar.inactiveBackground": background,
        "titleBar.inactiveForeground": bright_black,
    }

    for index, key in ANSI_COLOR_KEYS.items():
        if index in theme.palette:
            colors[key] = theme.palette[index]

    token_colors: list[dict[str, object]] = [
        {"scope": ["comment", "punctuation.definition.comment"], "settings": {"foreground": bright_black, "fontStyle": "italic"}},
        {"scope": ["string", "constant.other.symbol"], "settings": {"foreground": bright_green}},
        {"scope": ["constant.numeric", "constant.language", "constant.character", "variable.other.enummember"], "settings": {"foreground": bright_yellow}},
        {"scope": ["keyword", "storage", "storage.type"], "settings": {"foreground": bright_magenta}},
        {"scope": ["entity.name.function", "support.function", "meta.function-call"], "settings": {"foreground": bright_blue}},
        {"scope": ["entity.name.type", "entity.name.class", "support.type", "support.class"], "settings": {"foreground": bright_cyan}},
        {"scope": ["variable", "identifier"], "settings": {"foreground": foreground}},
        {"scope": ["variable.parameter", "meta.function.parameters"], "settings": {"foreground": blend(foreground, bright_yellow, 0.35)}},
        {"scope": ["entity.name.tag", "support.type.property-name"], "settings": {"foreground": red}},
        {"scope": ["entity.other.attribute-name", "variable.other.property"], "settings": {"foreground": yellow}},
        {"scope": ["punctuation", "meta.brace", "meta.delimiter"], "settings": {"foreground": white}},
        {"scope": ["keyword.operator", "storage.modifier"], "settings": {"foreground": magenta}},
        {"scope": ["invalid", "invalid.illegal"], "settings": {"foreground": background, "background": bright_red}},
        {"scope": ["markup.heading"], "settings": {"foreground": bright_yellow, "fontStyle": "bold"}},
        {"scope": ["markup.bold"], "settings": {"foreground": bright_white, "fontStyle": "bold"}},
        {"scope": ["markup.italic"], "settings": {"fontStyle": "italic"}},
        {"scope": ["markup.inserted"], "settings": {"foreground": bright_green}},
        {"scope": ["markup.deleted"], "settings": {"foreground": bright_red}},
        {"scope": ["markup.changed"], "settings": {"foreground": bright_yellow}},
    ]

    semantic_token_colors = {
        "namespace": bright_cyan,
        "type": bright_cyan,
        "class": bright_cyan,
        "enum": bright_cyan,
        "interface": cyan,
        "struct": bright_cyan,
        "typeParameter": cyan,
        "parameter": blend(foreground, bright_yellow, 0.35),
        "variable": foreground,
        "property": yellow,
        "enumMember": bright_yellow,
        "event": bright_magenta,
        "function": bright_blue,
        "method": bright_blue,
        "macro": magenta,
        "keyword": bright_magenta,
        "modifier": magenta,
        "comment": {"foreground": bright_black, "italic": True},
        "string": bright_green,
        "number": bright_yellow,
        "regexp": green,
        "operator": magenta,
    }

    return {
        "$schema": "vscode://schemas/color-theme",
        "name": name,
        "type": "dark",
        "colors": colors,
        "tokenColors": token_colors,
        "semanticHighlighting": True,
        "semanticTokenColors": semantic_token_colors,
    }


def default_settings_path(app: str) -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        app_dirs = {
            "code": home / "Library" / "Application Support" / "Code" / "User" / "settings.json",
            "insiders": home / "Library" / "Application Support" / "Code - Insiders" / "User" / "settings.json",
            "codium": home / "Library" / "Application Support" / "VSCodium" / "User" / "settings.json",
            "cursor": home / "Library" / "Application Support" / "Cursor" / "User" / "settings.json",
        }
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        app_dirs = {
            "code": base / "Code" / "User" / "settings.json",
            "insiders": base / "Code - Insiders" / "User" / "settings.json",
            "codium": base / "VSCodium" / "User" / "settings.json",
            "cursor": base / "Cursor" / "User" / "settings.json",
        }
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        app_dirs = {
            "code": config / "Code" / "User" / "settings.json",
            "insiders": config / "Code - Insiders" / "User" / "settings.json",
            "codium": config / "VSCodium" / "User" / "settings.json",
            "cursor": config / "Cursor" / "User" / "settings.json",
        }
    return app_dirs[app]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(value, temp_file, indent=2)
            temp_file.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def strip_jsonc(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escape = False

    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""

        if in_string:
            output.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue

        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                index += 1
            index += 2
            continue

        output.append(char)
        index += 1

    without_comments = "".join(output)
    return re.sub(r",(\s*[}\]])", r"\1", without_comments)


def read_settings(path: Path) -> dict[str, object]:
    if not path.exists() or not path.read_text(encoding="utf-8", errors="ignore").strip():
        return {}

    text = path.read_text(encoding="utf-8", errors="ignore")
    settings = json.loads(strip_jsonc(text))
    if not isinstance(settings, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return settings


def theme_key(theme_name: str) -> str:
    return f"[{theme_name}]"


def build_settings_patch(
    vscode_theme: dict[str, object],
    base_theme: str | None,
    *,
    theme_scope: str | None = None,
    preferred_light_theme: str | None = None,
) -> dict[str, object]:
    token_customizations = {
        "textMateRules": vscode_theme["tokenColors"],
    }
    semantic_customizations = {
        "enabled": True,
        "rules": vscode_theme["semanticTokenColors"],
    }

    if theme_scope:
        scope = theme_key(theme_scope)
        patch: dict[str, object] = {
            "workbench.colorCustomizations": {
                scope: vscode_theme["colors"],
            },
            "editor.tokenColorCustomizations": {
                scope: token_customizations,
            },
            "editor.semanticTokenColorCustomizations": {
                scope: semantic_customizations,
            },
        }
    else:
        patch = {
            "workbench.colorCustomizations": vscode_theme["colors"],
            "editor.tokenColorCustomizations": token_customizations,
            "editor.semanticTokenColorCustomizations": semantic_customizations,
        }

    if base_theme is not None:
        patch["workbench.colorTheme"] = base_theme
        patch["workbench.preferredDarkColorTheme"] = base_theme
    if preferred_light_theme:
        patch["window.autoDetectColorScheme"] = True
        patch["workbench.preferredLightColorTheme"] = preferred_light_theme
    return patch


def apply_settings_patch(settings_path: Path, patch: dict[str, object]) -> None:
    settings = read_settings(settings_path)
    settings.update(patch)
    write_json(settings_path, settings)


def default_extension_root(app: str) -> Path:
    home = Path.home()
    roots = {
        "code": home / ".vscode" / "extensions",
        "insiders": home / ".vscode-insiders" / "extensions",
        "codium": home / ".vscode-oss" / "extensions",
        "cursor": home / ".cursor" / "extensions",
    }
    return roots[app]


def remove_generated_extension(extension_root: Path, slug: str) -> Path | None:
    extension_dir = extension_root / f"ghostty-theme-{slug}"
    package_json = extension_dir / "package.json"
    if not package_json.exists():
        return None

    try:
        package = json.loads(package_json.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None

    if package.get("name") != f"ghostty-theme-{slug}" or package.get("publisher") != "local":
        return None

    shutil.rmtree(extension_dir)
    return extension_dir


def default_base_theme(app: str) -> str:
    if app == "cursor":
        return "Default Dark Modern"
    return "Default Dark Modern"


def remove_old_workbench_theme(settings_path: Path, generated_theme_name: str) -> None:
    settings = read_settings(settings_path)
    if settings.get("workbench.colorTheme") != generated_theme_name:
        return
    settings["workbench.colorTheme"] = "Default Dark Modern"
    write_json(settings_path, settings)

    text = settings_path.read_text(encoding="utf-8", errors="ignore")
    key_pattern = re.compile(r'("workbench\.colorTheme"\s*:\s*)"(?:\\.|[^"\\])*"')
    updated, count = key_pattern.subn(rf"\1{encoded_theme}", text, count=1)
    if count:
        settings_path.write_text(updated, encoding="utf-8")
        return

    closing_brace = text.rfind("}")
    if closing_brace == -1:
        raise ValueError(f"cannot update {settings_path}: expected a JSON object")

    before = text[:closing_brace].rstrip()
    after = text[closing_brace:]
    has_existing_properties = before.strip() != "{"
    needs_comma = has_existing_properties and not before.endswith(",")
    comma = "," if needs_comma else ""
    insertion = f'{comma}\n  "workbench.colorTheme": {encoded_theme}\n'
    settings_path.write_text(f"{before}{insertion}{after}", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a Ghostty theme directly to VS Code settings.json color customizations."
    )
    parser.add_argument("theme", type=Path, help="Ghostty theme file to convert.")
    parser.add_argument("--name", help="Theme label for metadata/cleanup. Defaults to the Ghostty theme filename.")
    parser.add_argument(
        "--app",
        choices=["code", "insiders", "codium", "cursor"],
        default="code",
        help="Editor settings location to update. Default: code.",
    )
    parser.add_argument("--settings", type=Path, help="Override the VS Code settings.json path.")
    parser.add_argument(
        "--base-theme",
        help="Built-in VS Code theme to use as the base under the customizations. Default: Default Dark Modern.",
    )
    parser.add_argument(
        "--theme-scope",
        help="Theme name to scope color customizations under. Defaults to --base-theme when setting a theme.",
    )
    parser.add_argument(
        "--global-customizations",
        action="store_true",
        help="Write customizations globally instead of scoping them to a single theme.",
    )
    parser.add_argument(
        "--preferred-light-theme",
        help="Set VS Code's preferred light theme and enable automatic color scheme detection.",
    )
    parser.add_argument("--no-set-theme", action="store_true", help="Do not change workbench.colorTheme.")
    parser.add_argument("--keep-old-extension", action="store_true", help="Do not remove a previously generated local extension.")
    parser.add_argument("--preview", action="store_true", help="Print the settings patch instead of writing settings.json.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    theme_name = args.name or f"Ghostty: {args.theme.name}"
    slug = slugify(theme_name)
    ghostty_theme = parse_ghostty_theme(args.theme)
    vscode_theme = build_vscode_theme(ghostty_theme, theme_name)
    base_theme = None if args.no_set_theme else args.base_theme or default_base_theme(args.app)
    theme_scope = None if args.global_customizations else args.theme_scope or base_theme
    settings_patch = build_settings_patch(
        vscode_theme,
        base_theme,
        theme_scope=theme_scope,
        preferred_light_theme=args.preferred_light_theme,
    )

    if args.preview:
        print(json.dumps(settings_patch, indent=2))
        return 0

    settings_path = args.settings or default_settings_path(args.app)
    apply_settings_patch(settings_path, settings_patch)
    print(f"updated {settings_path}")
    if base_theme is not None:
        print(f"base theme {base_theme}")
    if theme_scope:
        print(f"theme scope {theme_scope}")
    if args.preferred_light_theme:
        print(f"preferred light theme {args.preferred_light_theme}")

    if not args.keep_old_extension:
        removed = remove_generated_extension(default_extension_root(args.app), slug)
        if removed is not None:
            print(f"removed old extension {removed}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
