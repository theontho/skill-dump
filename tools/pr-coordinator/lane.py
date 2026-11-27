"""Conflict-aware reservations for PRs that touch generated files."""

import argparse
import subprocess
import time

from landing import json_gh, paused, queue_numbers
from runtime import P, atomic_text, gh, load, locked

HOLDERS = P.data / "locks/lane.holders"
DRIVER = "merge.generated.driver=git merge-file --marker-size=%L %A %O %B"


def git(*args, cwd=None, check=True):
    return subprocess.run(
        ["git", "-c", DRIVER, *args],
        cwd=cwd or P.checkout,
        capture_output=True,
        text=True,
        timeout=180,
        check=check,
    )


def touches(number):
    result = gh(
        "pr",
        "diff",
        str(number),
        "--name-only",
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    return any(P.generated.fullmatch(path) for path in result.stdout.splitlines())


def ref(number, worktrees):
    remote = f"refs/coord/lane/{number}"
    git("fetch", "-q", P.remote, f"+pull/{number}/head:{remote}")
    if number in worktrees:
        head = git("rev-parse", "HEAD", cwd=worktrees[number]).stdout.strip()
        if (
            git("merge-base", "--is-ancestor", remote, head, check=False).returncode
            == 0
        ):
            return head
    return remote


def open_pr(number):
    return json_gh("pr", "view", str(number), "--json", "state")["state"] == "OPEN"


def priority(name):
    values = P.get("lane", name, [])
    path = P.data / "locks" / f"lane.{name}"
    return list(
        dict.fromkeys(
            [
                *map(int, values),
                *(map(int, path.read_text().split()) if path.exists() else []),
            ]
        )
    )


def acquire(number):
    with locked("lane.lock"):
        if paused():
            return False
        now = int(time.time())
        queued = set(queue_numbers())
        rows = HOLDERS.read_text().splitlines() if HOLDERS.exists() else []
        holders = {
            int(n): int(since)
            for n, since in (row.split() for row in rows if row.strip())
            if (
                int(n) in queued
                or now - int(since) <= P.get("lane", "hold_seconds", 7200)
            )
            and open_pr(n)
        }
        if number in holders:
            return True
        all_train = {
            row["pr"]: row["worktree"] for row in load(P.data / "train.json", [])
        }
        mine = ref(number, all_train)
        top, favored = priority("top"), priority("priority")
        wait_for = []
        if number not in top:
            wait_for = top + (favored if number not in favored else [])
        others = [
            n for n in sorted(queued | holders.keys()) if n != number and touches(n)
        ]
        others += [
            n for n in wait_for if n != number and n not in others and open_pr(n)
        ]
        for n in others:
            merge = git(
                "merge-tree",
                "--write-tree",
                "--name-only",
                ref(n, all_train),
                mine,
                check=False,
            )
            if merge.returncode:
                atomic_text(
                    P.data / "locks" / f"lane-sim-{number}.txt",
                    f"Blocked by #{n}\n{merge.stdout}{merge.stderr}",
                )
                return False
        holders[number] = now
        atomic_text(HOLDERS, "".join(f"{n} {since}\n" for n, since in holders.items()))
        print(f"PR #{number}: reserved a generated-files lane slot", flush=True)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--try", dest="once", action="store_true")
    mode.add_argument("--touches", action="store_true")
    parser.add_argument("pr", type=int)
    args = parser.parse_args()
    if not touches(args.pr):
        return 1 if args.touches else 0
    if args.touches:
        return 0
    while not acquire(args.pr):
        if args.once:
            return 3
        time.sleep(120)
    print(f"PR #{args.pr} has a conflict-free lane slot.")
    return 0
