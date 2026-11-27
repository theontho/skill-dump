"""Cancel stale work when a PR's head moves: hosted pull_request runs and local certification jobs.

Runs every minute from collect_loop.sh. Skips `priority` (human collaborator) PRs entirely — we never
cancel a collaborator's runs. Merge-group runs are never touched.
"""

import json
import os
import signal
import subprocess

from reconcile import cert_pid
from runtime import P, cli, gh_argv, load

HERE = P.data
REPO = P.repo


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def log(msg):
    subprocess.run([*cli(), "log", msg])


def main():
    prs = json.loads(
        run(
            gh_argv(
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,headRefName,headRefOid,labels,author,isCrossRepository",
            )
        )
        or "[]"
    )
    heads = {
        p["headRefName"]: p
        for p in prs
        if (p.get("author") or {}).get("login") == P.owner
        and not p.get("isCrossRepository")
        and not any(
            l["name"] in (P.priority, P.land_asap, P.hands_off) for l in p["labels"]
        )
    }
    # 1. hosted pull_request runs on an old head of the same branch
    for status in ("queued", "in_progress"):
        runs = json.loads(
            run(
                gh_argv(
                    "api",
                    f"repos/{REPO}/actions/runs?event=pull_request&status={status}&per_page=100",
                )
            )
            or "{}"
        ).get("workflow_runs", [])
        for r in runs:
            p = heads.get(r["head_branch"])
            if (
                p
                and (r.get("head_repository") or {}).get("full_name", "").casefold()
                == REPO.casefold()
                and r["head_sha"] != p["headRefOid"]
            ):
                if (
                    subprocess.run(
                        gh_argv("run", "cancel", str(r["id"])), capture_output=True
                    ).returncode
                    == 0
                ):
                    log(
                        f"stale: cancelled run {r['id']} ({r['name']}) for #{p['number']} old head {r['head_sha'][:9]}"
                    )
    # 2. local certification jobs (cert.sh) still running for a superseded head
    by_number = {p["number"]: p for p in heads.values()}
    certs = load(HERE / "state.json", {}).get("certs", [])
    procs = run(["ps", "-eo", "pid=,ppid=,args="]).splitlines()
    table = [
        (int(x.split(None, 2)[0]), int(x.split(None, 2)[1]), x.split(None, 2)[2])
        for x in procs
        if len(x.split(None, 2)) == 3
    ]
    stopped = set()
    for c in certs:
        p = by_number.get(c["pr"])
        if (
            c.get("status") != "running"
            or not p
            or p["headRefOid"].startswith(c.get("sha") or "~")
        ):
            continue
        # only cancel when the certified commit is an ancestor of the new head (the head moved on)
        main = str(P.checkout)
        subprocess.run(
            ["git", "-C", main, "fetch", "-q", P.remote, f"pull/{c['pr']}/head"],
            capture_output=True,
        )
        if (
            subprocess.run(
                [
                    "git",
                    "-C",
                    main,
                    "merge-base",
                    "--is-ancestor",
                    c["sha"],
                    p["headRefOid"],
                ],
                capture_output=True,
            ).returncode
            != 0
        ):
            continue
        root = cert_pid(c)
        if not root:
            continue
        family, frontier = set(), {root}
        while frontier:  # all descendants of that cert.sh
            nxt = {
                pid
                for pid, parent, _ in table
                if parent in frontier and pid not in family
            }
            family |= nxt
            frontier = nxt
        for pid, _, args in table:
            if pid not in stopped and pid in family and any(
                token in args
                for token in P.get("certification", "cancel_process_matches", [])
            ):
                try:
                    os.kill(
                        pid, signal.SIGTERM
                    )  # the controller marks the status `error` and cleans up the worker
                    stopped.add(pid)
                    log(
                        f"stale: stopped {c['target']} certification of #{c['pr']} at {c['sha']} "
                        f"(head is now {p['headRefOid'][:9]})"
                    )
                except OSError:
                    pass


if __name__ == "__main__":
    main()
