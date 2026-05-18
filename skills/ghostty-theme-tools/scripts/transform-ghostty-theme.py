#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import operator
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


HEX_COLOR_RE = re.compile(r"#([0-9a-fA-F]{6})\b")
HEX_COLUMN_WIDTH = 8
SWATCH_COLUMN_WIDTH = 8

PRESETS = {
    "identity": (
        "r",
        "g",
        "b",
        "No change.",
    ),
    "soft-red-night": (
        "1.08*r + 0.05*g + 0.03*b",
        "0.86*g + 0.03*r",
        "0.72*b + 0.02*r",
        "Mild red night tint used for the softened Apple Classic Black pass.",
    ),
    "strong-red-night": (
        "r + 0.38*g + 0.28*b",
        "0.08*r + 0.32*g",
        "0.03*r + 0.12*b",
        "Aggressive red filter; useful as an upper bound, usually too strong.",
    ),
    "amber-bias": (
        "1.06*r + 0.10*g + 0.04*b",
        "0.92*g + 0.05*r",
        "0.82*b + 0.03*r",
        "Warms colors toward amber while preserving more of the original hue.",
    ),
}

INVERSE_PRESETS = {
    "identity": (
        "r",
        "g",
        "b",
    ),
    "soft-red-night": (
        "0.928141*r - 0.053962*g - 0.038673*b",
        "0.032377*r + 1.164673*g + 0.001349*b",
        "0.025782*r + 0.001499*g + 1.389963*b",
    ),
    "strong-red-night": (
        "1.197605*r - 1.422156*g - 2.794411*b",
        "0.299401*r + 3.480539*g + 0.698603*b",
        "0.299401*r + 0.355539*g + 9.031936*b",
    ),
    "amber-bias": (
        "0.949576*r - 0.103215*g - 0.046321*b",
        "0.051607*r + 1.092566*g + 0.002517*b",
        "0.034741*r + 0.003776*g + 1.221207*b",
    ),
}


@dataclass(frozen=True)
class Transform:
    red: Callable[[float, float, float], float]
    green: Callable[[float, float, float], float]
    blue: Callable[[float, float, float], float]
    red_expr: str
    green_expr: str
    blue_expr: str
    name: str
    inverse_exprs: tuple[str, str, str] | None


class ExpressionError(ValueError):
    pass


def clamp(value: float, low: float = 0, high: float = 255) -> float:
    return min(high, max(low, value))


def clamp_channel(value: float) -> int:
    return int(round(clamp(value)))


def compile_channel_expr(expr: str) -> Callable[[float, float, float], float]:
    parsed = ast.parse(expr, mode="eval")
    allowed_names = {"r", "g", "b"}
    allowed_functions = {
        "abs": abs,
        "clamp": clamp,
        "max": max,
        "min": min,
        "round": round,
    }
    binary_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    unary_ops = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    def evaluate(node: ast.AST, variables: dict[str, float]) -> float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body, variables)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in allowed_names:
                raise ExpressionError(f"unknown variable in expression {expr!r}: {node.id}")
            return variables[node.id]
        if isinstance(node, ast.BinOp):
            operation = binary_ops.get(type(node.op))
            if operation is None:
                raise ExpressionError(f"unsupported operator in expression {expr!r}")
            return float(operation(evaluate(node.left, variables), evaluate(node.right, variables)))
        if isinstance(node, ast.UnaryOp):
            operation = unary_ops.get(type(node.op))
            if operation is None:
                raise ExpressionError(f"unsupported unary operator in expression {expr!r}")
            return float(operation(evaluate(node.operand, variables)))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed_functions:
                raise ExpressionError(f"unsupported function in expression {expr!r}")
            if node.keywords:
                raise ExpressionError(f"keyword arguments are not supported in expression {expr!r}")
            args = [evaluate(arg, variables) for arg in node.args]
            return float(allowed_functions[node.func.id](*args))
        raise ExpressionError(f"unsupported syntax in expression {expr!r}: {ast.dump(node, include_attributes=False)}")

    def channel(r: float, g: float, b: float) -> float:
        return evaluate(parsed, {"r": r, "g": g, "b": b})

    return channel


def build_transform(args: argparse.Namespace) -> Transform:
    transform_name = args.preset or "custom"
    inverse_exprs = INVERSE_PRESETS.get(args.preset)
    if args.preset:
        red_expr, green_expr, blue_expr, _description = PRESETS[args.preset]
    else:
        red_expr, green_expr, blue_expr = "r", "g", "b"

    if args.red is not None:
        red_expr = args.red
        inverse_exprs = None
    if args.green is not None:
        green_expr = args.green
        inverse_exprs = None
    if args.blue is not None:
        blue_expr = args.blue
        inverse_exprs = None

    if inverse_exprs is None and args.preset:
        transform_name = f"custom based on {args.preset}"

    return Transform(
        red=compile_channel_expr(red_expr),
        green=compile_channel_expr(green_expr),
        blue=compile_channel_expr(blue_expr),
        red_expr=red_expr,
        green_expr=green_expr,
        blue_expr=blue_expr,
        name=transform_name,
        inverse_exprs=inverse_exprs,
    )


def transform_hex_color(color: str, transform: Transform) -> str:
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    new_red = clamp_channel(transform.red(red, green, blue))
    new_green = clamp_channel(transform.green(red, green, blue))
    new_blue = clamp_channel(transform.blue(red, green, blue))
    return f"#{new_red:02x}{new_green:02x}{new_blue:02x}"


def parse_hex_color(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def transform_theme_text(text: str, transform: Transform) -> tuple[str, list[tuple[str, str, str]]]:
    changes: list[tuple[str, str, str]] = []
    output_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in line:
            output_lines.append(line)
            continue

        key, value = [part.strip() for part in line.split("=", 1)]
        key_label = key
        if key == "palette":
            palette_index, separator, _color = value.partition("=")
            if separator:
                key_label = f"palette {palette_index.strip()}"

        def replace(match: re.Match[str]) -> str:
            original = match.group(0)
            updated = transform_hex_color(original, transform)
            changes.append((key_label, original.lower(), updated))
            return updated

        output_lines.append(HEX_COLOR_RE.sub(replace, line))

    return "".join(output_lines), changes


def build_transform_header(transform: Transform) -> str:
    lines = [
        "# Transformed by transform-ghostty-theme.py",
        f"# Transform: {transform.name}",
        f"# Forward: R' = {transform.red_expr}",
        f"# Forward: G' = {transform.green_expr}",
        f"# Forward: B' = {transform.blue_expr}",
    ]
    if transform.inverse_exprs is not None:
        inverse_red, inverse_green, inverse_blue = transform.inverse_exprs
        lines.extend(
            [
                "# Reverse is approximate because transformed colors are rounded and clamped.",
                "# Reverse command:",
                f"#   transform-ghostty-theme.py THEME --in-place --red '{inverse_red}' --green '{inverse_green}' --blue '{inverse_blue}'",
            ]
        )
    else:
        lines.extend(
            [
                "# Reverse: no automatic inverse is known for this custom transform.",
                "# Reverse by restoring the .bak/original theme, or derive inverse formulas from the forward transform above.",
            ]
        )
    return "\n".join(lines) + "\n\n"


def add_transform_header(text: str, transform: Transform) -> str:
    return f"{build_transform_header(transform)}{text}"


def swatch(color: str) -> str:
    red, green, blue = parse_hex_color(color)
    return f"\033[48;2;{red};{green};{blue}m{' ' * SWATCH_COLUMN_WIDTH}\033[0m"


def print_preview(changes: list[tuple[str, str, str]], render_swatches: bool) -> None:
    if not changes:
        print("No Ghostty color values found.")
        return

    key_width = max(len(key) for key, _original, _updated in changes)
    if render_swatches:
        print(
            f"{'Key':<{key_width}}  "
            f"{'Original':<{HEX_COLUMN_WIDTH}}  "
            f"{'Before':<{SWATCH_COLUMN_WIDTH}}  "
            f"{'Updated':<{HEX_COLUMN_WIDTH}}  "
            f"{'After':<{SWATCH_COLUMN_WIDTH}}"
        )
        print(
            f"{'-' * key_width}  "
            f"{'-' * HEX_COLUMN_WIDTH}  "
            f"{'-' * SWATCH_COLUMN_WIDTH}  "
            f"{'-' * HEX_COLUMN_WIDTH}  "
            f"{'-' * SWATCH_COLUMN_WIDTH}"
        )
        for key, original, updated in changes:
            print(
                f"{key:<{key_width}}  "
                f"{original:<{HEX_COLUMN_WIDTH}}  "
                f"{swatch(original)}  "
                f"{updated:<{HEX_COLUMN_WIDTH}}  "
                f"{swatch(updated)}"
            )
    else:
        print(f"{'Key':<{key_width}}  {'Original':<{HEX_COLUMN_WIDTH}}  {'Updated':<{HEX_COLUMN_WIDTH}}")
        print(f"{'-' * key_width}  {'-' * HEX_COLUMN_WIDTH}  {'-' * HEX_COLUMN_WIDTH}")
        for key, original, updated in changes:
            print(f"{key:<{key_width}}  {original:<{HEX_COLUMN_WIDTH}}  {updated:<{HEX_COLUMN_WIDTH}}")


def write_output(path: Path, text: str, backup: bool) -> None:
    original_mode = path.stat().st_mode if path.exists() else None
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
        if original_mode is not None:
            os.chmod(temp_path, original_mode)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    preset_help = ", ".join(f"{name}: {values[3]}" for name, values in PRESETS.items())
    parser = argparse.ArgumentParser(
        description="Apply RGB channel transforms to #RRGGBB colors in a Ghostty theme.",
        epilog=(
            "Expression variables are r, g, and b in 0-255 channel space. "
            "Results are rounded and clamped to 0-255. "
            f"Presets: {preset_help}"
        ),
    )
    parser.add_argument("input", type=Path, help="Ghostty theme file to read.")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Theme file to write. Omit with --in-place, --preview, or to write to stdout.",
    )
    parser.add_argument("--in-place", action="store_true", help="Overwrite the input theme file.")
    parser.add_argument(
        "--backup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Create INPUT.bak when using --in-place. Enabled by default.",
    )
    parser.add_argument("--preview", action="store_true", help="Print a transformation table instead of theme text.")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="Use a built-in transform preset.")
    parser.add_argument("--red", metavar="EXPR", help="Expression for the output red channel.")
    parser.add_argument("--green", metavar="EXPR", help="Expression for the output green channel.")
    parser.add_argument("--blue", metavar="EXPR", help="Expression for the output blue channel.")
    parser.add_argument("--show-formula", action="store_true", help="Print the formulas being used before output.")
    parser.add_argument("--no-header", action="store_true", help="Do not prepend transform metadata comments to output.")
    parser.add_argument(
        "--no-render",
        action="store_true",
        help="Do not render ANSI color swatches in --preview output.",
    )
    args = parser.parse_args(argv)

    if args.in_place and args.output is not None:
        parser.error("use either --in-place or an output path, not both")
    if args.backup and not args.in_place:
        args.backup = False
    if not args.preset and args.red is None and args.green is None and args.blue is None:
        parser.error("choose --preset or provide at least one of --red, --green, or --blue")

    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    transform = build_transform(args)
    text = args.input.read_text(encoding="utf-8", errors="ignore")
    transformed_text, changes = transform_theme_text(text, transform)

    if args.show_formula:
        print("Transform:")
        print(f"  name: {transform.name}")
        print(f"  R' = {transform.red_expr}")
        print(f"  G' = {transform.green_expr}")
        print(f"  B' = {transform.blue_expr}")
        print()

    if args.preview:
        print_preview(changes, render_swatches=not args.no_render)
        return 0

    if not args.no_header:
        transformed_text = add_transform_header(transformed_text, transform)

    if args.in_place:
        write_output(args.input, transformed_text, args.backup)
        print(f"updated {args.input}")
        if args.backup:
            print(f"backup  {args.input.with_suffix(args.input.suffix + '.bak')}")
    elif args.output is not None:
        write_output(args.output, transformed_text, backup=False)
        print(f"wrote {args.output}")
    else:
        print(transformed_text, end="")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ExpressionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
