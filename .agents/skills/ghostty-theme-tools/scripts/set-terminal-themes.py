#!/usr/bin/env python3
from __future__ import annotations

import copy
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_LIGHT_THEME = "Claude Code Light"
DEFAULT_DARK_THEME = "Apple Classic Black"


@dataclass(frozen=True)
class Options:
    light_theme: str
    dark_theme: str
    reload_cmux: bool
    use_cmux_cli: bool
    update_terminal: bool


def usage(program: str) -> str:
    return f"""Usage:
  {program} [--light THEME] [--dark THEME] [--no-reload] [--no-cmux-cli] [--no-terminal]
  {program} "LIGHT THEME" "DARK THEME"

Defaults:
  light: {DEFAULT_LIGHT_THEME}
  dark:  {DEFAULT_DARK_THEME}
"""


def parse_args(argv: list[str]) -> Options:
    light_theme = DEFAULT_LIGHT_THEME
    dark_theme = DEFAULT_DARK_THEME
    reload_cmux = True
    use_cmux_cli = True
    update_terminal = True

    program = Path(sys.argv[0]).name
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--light":
            if index + 1 >= len(argv):
                print("--light requires a theme name", file=sys.stderr)
                raise SystemExit(2)
            light_theme = argv[index + 1]
            index += 2
        elif arg == "--dark":
            if index + 1 >= len(argv):
                print("--dark requires a theme name", file=sys.stderr)
                raise SystemExit(2)
            dark_theme = argv[index + 1]
            index += 2
        elif arg == "--no-reload":
            reload_cmux = False
            index += 1
        elif arg == "--no-cmux-cli":
            use_cmux_cli = False
            index += 1
        elif arg == "--no-terminal":
            update_terminal = False
            index += 1
        elif arg in ("-h", "--help"):
            print(usage(program), end="")
            raise SystemExit(0)
        elif len(argv) - index == 2:
            light_theme = argv[index]
            dark_theme = argv[index + 1]
            index += 2
        else:
            print(f"Unknown argument: {arg}\n", file=sys.stderr)
            print(usage(program), end="", file=sys.stderr)
            raise SystemExit(2)

    return Options(
        light_theme=light_theme,
        dark_theme=dark_theme,
        reload_cmux=reload_cmux,
        use_cmux_cli=use_cmux_cli,
        update_terminal=update_terminal,
    )


def xdg_config_home() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured:
        return Path(configured)
    return Path.home() / ".config"


def update_theme_file(path: Path, theme_value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    theme_line = f"theme = {theme_value}\n"

    if not path.exists():
        path.write_text(theme_line, encoding="utf-8")
        print(f"wrote {path}")
        return

    original_mode = path.stat().st_mode
    wrote = False
    output: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True):
        if re.match(r"^[ \t]*theme[ \t]*=", line):
            if not wrote:
                output.append(theme_line)
                wrote = True
            continue
        output.append(line)

    if not wrote:
        output.append(theme_line)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.writelines(output)
        os.chmod(temp_path, stat.S_IMODE(original_mode))
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    print(f"updated {path}")


def parse_hex(value: str) -> tuple[int, int, int] | None:
    value = value.strip().split()[0].lstrip("#")
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def read_ghostty_theme(path: Path) -> tuple[dict[str, tuple[int, int, int]], dict[int, tuple[int, int, int]]]:
    colors: dict[str, tuple[int, int, int]] = {}
    palette: dict[int, tuple[int, int, int]] = {}

    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = [part.strip() for part in line.split("=", 1)]
        if key == "palette":
            index_text, _, color_text = value.partition("=")
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

    return colors, palette


def sync_terminal_profile(profile_name: str) -> None:
    try:
        from AppKit import NSColor
        from Foundation import NSKeyedArchiver
    except Exception as exc:
        print(f"skipped Terminal.app profile: PyObjC/AppKit unavailable: {exc}", file=sys.stderr)
        return

    home = Path.home()
    theme_paths = [
        xdg_config_home() / "ghostty" / "themes" / profile_name,
        Path("/Applications/Ghostty.app/Contents/Resources/ghostty/themes") / profile_name,
        Path("/Applications/cmux.app/Contents/Resources/ghostty/themes") / profile_name,
    ]

    theme_path = next((path for path in theme_paths if path.is_file()), None)
    if theme_path is None:
        print(f"skipped Terminal.app profile: Ghostty theme not found: {profile_name}", file=sys.stderr)
        return

    colors, palette = read_ghostty_theme(theme_path)

    def archive_color(rgb: tuple[int, int, int]) -> bytes:
        red, green, blue = (channel / 255.0 for channel in rgb)
        color = NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 1.0)
        data, error = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(color, False, None)
        if error is not None:
            raise RuntimeError(error)
        return bytes(data)

    pref_path = home / "Library" / "Preferences" / "com.apple.Terminal.plist"
    if pref_path.exists():
        prefs = plistlib.loads(pref_path.read_bytes())
    else:
        prefs = {}

    window_settings = dict(prefs.get("Window Settings", {}))
    base_name = prefs.get("Default Window Settings") or prefs.get("Startup Window Settings") or "Pro"
    base_profile = window_settings.get(base_name) or window_settings.get("Pro") or window_settings.get("Basic") or {}
    profile = copy.deepcopy(base_profile)
    profile.update(
        {
            "name": profile_name,
            "type": "Window Settings",
            "ProfileCurrentVersion": profile.get("ProfileCurrentVersion", 2.09),
        }
    )

    ansi_keys = {
        0: "ANSIBlackColor",
        1: "ANSIRedColor",
        2: "ANSIGreenColor",
        3: "ANSIYellowColor",
        4: "ANSIBlueColor",
        5: "ANSIMagentaColor",
        6: "ANSICyanColor",
        7: "ANSIWhiteColor",
        8: "ANSIBrightBlackColor",
        9: "ANSIBrightRedColor",
        10: "ANSIBrightGreenColor",
        11: "ANSIBrightYellowColor",
        12: "ANSIBrightBlueColor",
        13: "ANSIBrightMagentaColor",
        14: "ANSIBrightCyanColor",
        15: "ANSIBrightWhiteColor",
    }
    for index, key in ansi_keys.items():
        if index in palette:
            profile[key] = archive_color(palette[index])

    if "background" in colors:
        profile["BackgroundColor"] = archive_color(colors["background"])
    if "foreground" in colors:
        profile["TextColor"] = archive_color(colors["foreground"])
        profile["TextBoldColor"] = archive_color(colors["foreground"])
    if "cursor-color" in colors:
        profile["CursorColor"] = archive_color(colors["cursor-color"])
    if "selection-background" in colors:
        profile["SelectionColor"] = archive_color(colors["selection-background"])

    window_settings[profile_name] = profile
    prefs["Window Settings"] = window_settings
    prefs["Default Window Settings"] = profile_name
    prefs["Startup Window Settings"] = profile_name

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            plistlib.dump(prefs, temp_file, fmt=plistlib.FMT_BINARY)
        subprocess.run(["defaults", "import", "com.apple.Terminal", str(temp_path)], check=True)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    print(f"updated Terminal.app profile {profile_name}")


def main(argv: list[str]) -> int:
    options = parse_args(argv)
    theme_value = f"light:{options.light_theme},dark:{options.dark_theme}"
    cmux_path = shutil.which("cmux")

    if cmux_path and options.use_cmux_cli:
        subprocess.run(
            ["cmux", "themes", "set", "--light", options.light_theme, "--dark", options.dark_theme],
            check=True,
        )

    home = Path.home()
    files = [
        xdg_config_home() / "ghostty" / "config",
        home / "Library" / "Application Support" / "com.mitchellh.ghostty" / "config",
        home / "Library" / "Application Support" / "com.mitchellh.ghostty" / "auto" / "theme.ghostty",
        home / "Library" / "Application Support" / "com.cmuxterm.app" / "config.ghostty",
    ]

    for path in files:
        update_theme_file(path, theme_value)

    if options.update_terminal:
        sync_terminal_profile(options.dark_theme)

    if cmux_path and options.reload_cmux:
        subprocess.run(["cmux", "reload-config"], check=True)

    print("\nTheme set everywhere:")
    print(f"  light: {options.light_theme}")
    print(f"  dark:  {options.dark_theme}")
    if options.update_terminal:
        print(f"  Terminal.app profile: {options.dark_theme}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
