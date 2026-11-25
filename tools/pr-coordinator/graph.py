"""Compute the open-PR dependency graph for the dashboard (stored under state["graph"]).

Nodes: open tracked PRs (ours, bot, priority) with stage info.
Edges:
  conflict  — the two PR heads have a 3-way text conflict (git merge-tree), so they can't share the queue.
  waits     — directed: a PR that conflicts with a queued PR or a lane holder must wait for it.
"""

import fcntl
import json
import subprocess
import time
from itertools import combinations

from runtime import P, load, save

HERE = P.data
MAIN = P.checkout
G = [
    "git",
    "-C",
    str(MAIN),
    "-c",
    "merge.generated.driver=git merge-file --marker-size=%L %A %O %B",
]


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout


def main():
    triage = {}
    for line in (HERE / "triage.jsonl").read_text().splitlines():
        d = json.loads(line)
        triage[d["n"]] = d
    nums = sorted(triage)
    try:
        train = {
            e["pr"]: e["worktree"]
            for e in json.loads((HERE / "train.json").read_text())
        }
    except (OSError, ValueError):
        train = {}
    for n in nums:
        run([*G, "fetch", "-q", P.remote, f"pull/{n}/head:refs/coord/graph/{n}"])
        wt = train.get(n)
        if wt:  # prefer the worktree's unpushed resolution when it builds on the pushed head
            code, head = run(["git", "-C", wt, "rev-parse", "HEAD"])
            if (
                code == 0
                and run(
                    [
                        *G,
                        "merge-base",
                        "--is-ancestor",
                        f"refs/coord/graph/{n}",
                        head.strip(),
                    ]
                )[0]
                == 0
            ):
                run([*G, "update-ref", f"refs/coord/graph/{n}", head.strip()])
    files = {}
    for n in nums:
        _, base = run([*G, "merge-base", P.base_ref, f"refs/coord/graph/{n}"])
        _, out = run([*G, "diff", "--name-only", base.strip(), f"refs/coord/graph/{n}"])
        files[n] = set(out.split())
    conflicts = []
    for a, b in combinations(nums, 2):
        shared = files[a] & files[b]
        if not shared:
            continue
        code, out = run(
            [
                *G,
                "merge-tree",
                "--write-tree",
                "--name-only",
                f"refs/coord/graph/{a}",
                f"refs/coord/graph/{b}",
            ]
        )
        if code == 1:
            paths = [
                p
                for p in out.splitlines()[1:]
                if p and not p.startswith(("Auto-merging", "CONFLICT"))
            ]
            conflicts.append({"a": a, "b": b, "files": paths[:6]})
    holders = set()
    try:
        holders = {
            int(line.split()[0])
            for line in (HERE / "locks/lane.holders").read_text().splitlines()
            if line.strip()
        }
    except OSError:
        pass
    blockers = {n for n in nums if triage[n].get("queue")} | holders
    waits = []
    for c in conflicts:
        a, b = c["a"], c["b"]
        if a in blockers and b not in blockers:
            waits.append({"from": b, "to": a})
        elif b in blockers and a not in blockers:
            waits.append({"from": a, "to": b})
    nodes = [
        {
            "n": n,
            "title": triage[n]["title"],
            "draft": triage[n]["draft"],
            "queue": triage[n].get("queue"),
            "merge": triage[n]["merge"],
            "holder": n in holders,
            "labels": triage[n].get("labels", []),
        }
        for n in nums
    ]
    graph = {
        "nodes": nodes,
        "conflicts": conflicts,
        "waits": waits,
        "at": time.strftime("%Y-%m-%d %H:%M"),
    }
    with open(HERE / "state.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        s = load(HERE / "state.json", {"prs": [], "machines": [], "log": []})
        s["graph"] = graph
        save(HERE / "state.json", s)


if __name__ == "__main__":
    main()
