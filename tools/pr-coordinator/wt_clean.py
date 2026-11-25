"""Archive small evidence files and remove explicitly named finished worktrees."""

import argparse
import shutil
import subprocess
from pathlib import Path

from runtime import P, load


def git(worktree, *args):
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    ).stdout


def registered():
    return {
        Path(parts[0][len("worktree ") :]).resolve(): parts
        for record in git(P.checkout, "worktree", "list", "--porcelain", "-z").split(
            "\0\0"
        )
        if (parts := record.split("\0")) and parts[0].startswith("worktree ")
    }


def archive(worktree):
    output = worktree / P.get("cleanup", "output_directory", "out")
    destination = P.data / "archive" / worktree.name
    for file in output.rglob("*"):
        if (
            not file.is_symlink()
            and file.is_file()
            and file.suffix in {".log", ".md", ".json", ".txt", ".xml"}
            and file.stat().st_size < 1_000_000
        ):
            target = destination / file.relative_to(output)
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copy2(file, target)


def inbound_link(worktree, others):
    ignore = {".git", ".venv", "node_modules", ".pixi", "out", "target"}
    for other in others:
        for relative in P.get("cleanup", "link_scan_roots", ["."]):
            start = other / relative
            for entry in start.glob("*"):
                if entry.name in ignore:
                    continue
                candidates = [entry]
                if entry.is_dir() and not entry.is_symlink():
                    candidates += list(entry.glob("*")) + list(entry.glob("*/*"))
                for path in candidates:
                    if path.is_symlink() and path.resolve().is_relative_to(worktree):
                        return path
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--from", dest="file", type=Path, help="read one worktree path per line"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="remove eligible worktrees; default is a dry run",
    )
    opts = parser.parse_args()
    paths = opts.paths + (
        [Path(line) for line in opts.file.read_text().splitlines() if line.strip()]
        if opts.file
        else []
    )
    entries = registered()
    active = {
        Path(row["worktree"]).resolve()
        for row in load(P.data / "train.json", [])
        if row.get("stage") not in {"closed", "merged"}
    }
    for worktree in (path.expanduser().resolve() for path in paths):
        if (
            worktree == P.checkout
            or worktree not in entries
            or worktree in active
            or not worktree.is_relative_to(P.worktrees)
            or worktree == P.worktrees
        ):
            print(f"SKIP {worktree}: not a finished managed worktree")
            continue
        if git(worktree, "status", "--porcelain", "--untracked-files=normal").strip():
            print(f"SKIP {worktree}: dirty")
            continue
        head = git(worktree, "rev-parse", "HEAD").strip()
        remote_refs = git(
            P.checkout, "for-each-ref", "--contains", head, "refs/remotes"
        ).strip()
        if not remote_refs:
            print(
                f"SKIP {worktree}: local head not reachable from a remote-tracking ref"
            )
            continue
        if link := inbound_link(worktree, entries.keys() - {worktree}):
            print(
                f"SKIP {worktree}: {link} depends on it; relocate the shared input first"
            )
            continue
        if opts.apply:
            archive(worktree)
            git(P.checkout, "worktree", "remove", str(worktree))
            print(f"Removed {worktree}")
        else:
            print(f"Would archive and remove {worktree}")
