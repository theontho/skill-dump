"""Foreground supervisor and collection loops; one of each per project."""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from landing import (
    assert_owner,
    checks,
    eligible,
    enqueue,
    json_gh,
    labels,
    mergeable,
    paused,
    pull,
    queue_numbers,
    review,
    reviewed,
)
from runtime import P, atomic_text, cli, gh, load, locked


def record(*args):
    subprocess.run(cli(*args), check=True, timeout=90)


def headline():
    hours = P.get("dashboard", "headline_hours", 24)
    start = P.get("dashboard", "headline_since") or (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = json_gh(
        "pr",
        "list",
        "--state",
        "merged",
        "--search",
        f"merged:>={start}",
        "--limit",
        "500",
        "--json",
        "number",
    )
    queue = " ".join(f"#{n}" for n in queue_numbers()) or "empty"
    train = [
        row
        for row in load(P.data / "train.json", [])
        if row.get("stage") not in {"merged", "closed"}
    ]
    blocked = [f"#{row['pr']}" for row in train if row.get("stage") == "blocked"]
    runners = load(P.data / "state.json", {}).get("runners", {})
    record(
        "headline",
        f"{len(merged)} PRs merged since {start} · merge queue: {queue} · landing train: {len(train)} PRs"
        + (f" ({len(blocked)} blocked: {' '.join(blocked)})" if blocked else "")
        + f" · runners {sum(runners.get('running', {}).values())} running / {sum(runners.get('waiting', {}).values())} waiting",
    )


def queue_event(number):
    return next(
        iter(
            pull(
                number,
                "timelineItems(last:1,itemTypes:[ADDED_TO_MERGE_QUEUE_EVENT,"
                "REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{__typename "
                "... on RemovedFromMergeQueueEvent{reason createdAt}}}",
            )["timelineItems"]["nodes"]
        ),
        {},
    )


def priority_pr(n, current):
    from train import only_cancelled_run

    tags = labels(current)
    entry = current["mergeQueueEntry"]
    authorized = P.land_asap in tags or current["autoMergeRequest"] or entry
    if not authorized:
        record(
            "pr",
            n,
            "waiting",
            "--machine",
            "priority (author)",
            "--note",
            "Waiting for author to request merge",
        )
        return
    head = current["headRefOid"]
    if P.land_asap in tags and not entry:
        for run in json_gh(
            "run",
            "list",
            "--commit",
            head,
            "--limit",
            "10",
            "--json",
            "databaseId,conclusion,event",
        ):
            rid = run["databaseId"]
            marker = P.data / "locks" / f"rerun-{rid}"
            if (
                run["event"] == "pull_request"
                and run["conclusion"] == "cancelled"
                and not marker.exists()
                and only_cancelled_run(rid)
            ):
                outcome = gh(
                    "run",
                    "rerun",
                    str(rid),
                    "--failed",
                    capture_output=True,
                    timeout=90,
                )
                if outcome.returncode == 0:
                    marker.touch()
                    record("log", f"priority #{n}: reran cancelled jobs in {rid}")
    if not reviewed(current) or not mergeable(current):
        return
    if entry:
        marker = P.data / "locks" / f"jumped-{n}-{head[:9]}"
        if entry["position"] <= 1 or marker.exists() or not checks(n)[0]:
            return
        # Re-read before moving an entry; do not touch anyone's branch.
        now = review(n)
        if (
            not eligible(now, owned=False)
            or not labels(now) & {P.priority, P.land_asap}
            or now["headRefOid"] != head
            or not now["mergeQueueEntry"]
            or now["mergeQueueEntry"]["position"] <= 1
            or not reviewed(now)
            or not mergeable(now)
        ):
            return
        result = json_gh(
            "api",
            "graphql",
            "-f",
            "query=mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}",
            "-f",
            f"id={now['id']}",
        )
        if not result.get("data"):
            return
        # Use the same previously validated SHA; dequeue also disables auto-merge.
        result = json_gh(
            "api",
            "graphql",
            "-f",
            "query=mutation($id:ID!,$sha:GitObjectID!){enqueuePullRequest("
            "input:{pullRequestId:$id,jump:true,expectedHeadOid:$sha}){mergeQueueEntry{position}}}",
            "-f",
            f"id={now['id']}",
            "-f",
            f"sha={head}",
        )
        if (
            not result.get("data", {})
            .get("enqueuePullRequest", {})
            .get("mergeQueueEntry")
        ):
            raise RuntimeError(f"priority #{n} dequeued but could not be re-enqueued")
        marker.touch()
        record("log", f"priority #{n}: moved to front of merge queue")
    elif P.land_asap in tags and enqueue(n, head, priority=True):
        record("log", f"priority #{n}: enqueued at front")


def refresh_triage():
    subprocess.run(
        ["git", "fetch", "-q", P.remote, P.base],
        cwd=P.checkout,
        check=True,
        timeout=180,
    )
    triage = subprocess.run(
        cli("triage"), capture_output=True, text=True, timeout=300, check=True
    ).stdout
    atomic_text(P.data / "triage.jsonl", triage)
    record("sync")
    return triage


def supervise_cycle():
    from lane import acquire, touches

    triage = refresh_triage()
    record("reconcile")
    headline()
    train = {
        r["pr"]
        for r in load(P.data / "train.json", [])
        if r.get("stage") not in {"closed", "merged"}
    }
    for row in map(json.loads, triage.splitlines()):
        n = row["n"]
        try:
            current = review(n)
            if not eligible(current, owned=False):
                continue
            tags = labels(current)
            if current["mergeQueueEntry"]:
                record(
                    "pr",
                    n,
                    "queued",
                    "--note",
                    f"merge queue: position {current['mergeQueueEntry']['position']}",
                )
            if tags & {P.priority, P.land_asap}:
                priority_pr(n, current)
                continue
            if (
                n in train
                or not eligible(current)
                or current["mergeQueueEntry"]
                or current["autoMergeRequest"]
            ):
                continue
            last_event = queue_event(n)
            reason = last_event.get("reason")
            if reason == "failed_checks":
                record(
                    "pr",
                    n,
                    "fixing",
                    "--note",
                    "Merge group failed; needs investigation before re-enqueue",
                )
                continue
            if reason not in {None, "manual", "merged"}:
                record(
                    "pr",
                    n,
                    "dropped",
                    "--note",
                    f"Removed from the merge queue: {reason} at {last_event.get('createdAt', '')}",
                )
            if (
                paused()
                or not reviewed(current)
                or not mergeable(current)
                or not checks(n)[0]
            ):
                continue
            if touches(n) and not acquire(n):
                record(
                    "pr",
                    n,
                    "waiting",
                    "--note",
                    "Passable; waiting for generated-files lane",
                )
                continue
            with locked(f"land-pr-{n}.lock", wait=False):
                if any(
                    row["pr"] == n and row.get("stage") not in {"closed", "merged"}
                    for row in load(P.data / "train.json", [])
                ):
                    continue
                if enqueue(n, current["headRefOid"]):
                    record(
                        "log",
                        f"supervisor: enqueue #{n} at {current['headRefOid'][:9]}",
                    )
        except BlockingIOError:
            continue
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            print(f"supervise #{n}: {exc}", file=sys.stderr, flush=True)


def collect_cycle(tick, *, mutate):
    jobs = ["hosts"]
    if mutate:
        jobs += ["yield-runners", "cancel-stale"]
    if tick % 5 == 0:
        if not mutate:
            refresh_triage()
            headline()
        jobs += ["lifecycle", "graph"]
    for command in jobs:
        try:
            subprocess.run(cli(command), check=True, timeout=1200)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"collect {command}: {exc}", file=sys.stderr, flush=True)


def loop(kind):
    parser = argparse.ArgumentParser(
        description=f"Run the project {kind} loop in the foreground."
    )
    parser.add_argument("--once", action="store_true")
    if kind == "collect":
        parser.add_argument(
            "--mutate",
            action="store_true",
            help="Yield and cancel GitHub runs, not just collect state",
        )
    args = parser.parse_args()
    mutate = kind == "supervise" or args.mutate
    if mutate:
        assert_owner()
    with locked(f"{kind}.lock", wait=False):
        tick = 0
        while True:
            try:
                if kind == "supervise":
                    supervise_cycle()
                else:
                    collect_cycle(tick, mutate=mutate)
            except (
                OSError,
                RuntimeError,
                ValueError,
                KeyError,
                subprocess.SubprocessError,
            ) as exc:
                print(f"{kind}: {exc}", file=sys.stderr, flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            tick += 1
            time.sleep(
                P.get("loops", f"{kind}_seconds", 300 if kind == "supervise" else 60)
            )
