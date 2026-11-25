"""Project overlapping evidence onto one ordered clock per pull request."""

import math
import re
from collections import Counter, defaultdict
from typing import Any

from lifecycle_model import PRIORITY, stamp, union

STAGES = (
    "dev",
    "review",
    "cert",
    "ready",
    "hosted",
    "lane",
    "refresh",
    "checks",
    "queue",
    "merged",
)
ROW_ORDER = (*STAGES[:-1], "unrecorded", "merged")


def times(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "p90": None}
    values = sorted(values)
    n = len(values)
    return {
        "n": n,
        "median": (values[(n - 1) // 2] + values[n // 2]) // 2,
        "p90": values[math.ceil(n * 0.9) - 1],
    }


def ready_signals(
    pr: dict[str, Any], raw: dict[str, Any], end: int
) -> tuple[list[int], int | None]:
    opened = stamp(raw.get("createdAt")) or pr["first"]
    ordered = sorted(
        (at, i, event)
        for i, event in enumerate(raw.get("events", []))
        if (at := stamp(event.get("createdAt"))) is not None and opened <= at <= end
    )
    transitions = [
        (at, event["__typename"])
        for at, _, event in ordered
        if event["__typename"] in {"ReadyForReviewEvent", "ConvertToDraftEvent"}
    ]
    ready = [at for at, kind in transitions if kind == "ReadyForReviewEvent"]
    if (transitions and transitions[0][1] == "ConvertToDraftEvent") or (
        not transitions and raw.get("isDraft") is False
    ):
        ready.insert(0, opened)
    approved = [
        at
        for _, _, event in ordered
        if event["__typename"] == "PullRequestReview"
        and event.get("state") == "APPROVED"
        and (at := stamp(event.get("submittedAt"))) is not None
        and opened <= at <= end
    ]
    return sorted(set(ready)), min(ready + approved, default=None)


def strip(pr: dict[str, Any], end: int, ready: list[int]) -> list[dict[str, Any]]:
    """Partition [start, end], counting overlapping work once and retaining revisits."""
    start = pr["first"]
    changes: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    for index, seg in enumerate(pr["segments"]):
        a = max(start, seg["start"])
        b = min(end, seg["end"] if seg["end"] is not None else end)
        if a < b and seg["phase"] in PRIORITY and seg["phase"] != "dev":
            changes[a].append((index, True))
            changes[b].append((index, False))
    first_ready = ready[0] if ready else None
    intervals: list[dict[str, Any]] = []
    if first_ready is not None:
        changes[first_ready]
    changes[end]
    active: set[int] = set()
    previous = start
    for at, edges in sorted(changes.items()):
        if previous < at:
            phase = min(
                (pr["segments"][i]["phase"] for i in active),
                key=PRIORITY.index,
                default=None,
            )
            if not phase:
                # After readiness, silence is not evidence that development resumed.
                phase = (
                    "dev"
                    if first_ready is None or previous < first_ready
                    else "unrecorded"
                )
            if intervals and intervals[-1]["stage"] == phase and previous not in ready:
                intervals[-1]["end"] = at
            else:
                intervals.append({"stage": phase, "start": previous, "end": at})
        for index, begins in edges:
            (active.add if begins else active.discard)(index)
        previous = at
    return intervals


def rework(
    pr: dict[str, Any], raw: dict[str, Any], runs: list[dict[str, Any]], end: int
) -> Counter[str]:
    counts: Counter[str] = Counter()
    reviews: list[tuple[int, str]] = []
    revisions: list[tuple[int, str]] = []
    for event in raw.get("events", []):
        kind = event["__typename"]
        if (
            kind == "PullRequestReview"
            and "copilot" in ((event.get("author") or {}).get("login") or "").lower()
        ):
            commit = (event.get("commit") or {}).get("oid")
            if commit and (submitted := stamp(event.get("submittedAt"))) is not None:
                reviews.append((submitted, commit))
        if kind == "HeadRefForcePushedEvent":
            sha = (event.get("afterCommit") or {}).get("oid")
            at = stamp(event.get("createdAt"))
        else:
            commit_data = event.get("commit") or {}
            sha = commit_data.get("oid")
            at = stamp(commit_data.get("pushedDate"))
        if sha and at is not None:
            revisions.append((at, sha))
    run_shas: dict[int, str] = {}
    for run in runs:
        at = stamp(run["created_at"])
        if at is not None:
            revisions.append((at, run["head_sha"]))
        run_shas[run["id"]] = run["head_sha"]
    first_for_sha: dict[str, int] = {}
    for at, sha in sorted(revisions):
        if pr["first"] <= at <= end:
            first_for_sha.setdefault(sha, at)
    revisions = sorted((at, sha) for sha, at in first_for_sha.items())
    ready_running: dict[int, list[tuple[int, int]]] = defaultdict(list)
    recert_pairs: set[tuple[str, str]] = set()
    for seg in pr["segments"]:
        if seg["activity"] != "running":
            continue
        if (
            seg["phase"] == "hosted"
            and seg.get("run") in run_shas
            and seg["start"] < (seg["end"] or end)
        ):
            ready_running[seg["run"]].append((seg["start"], seg["end"] or end))
        if seg["phase"] == "cert" and seg["start"] < (seg["end"] or end):
            match = re.search(r"\b[a-f0-9]{7,40}\b", seg.get("note", ""))
            review = max(
                ((at, sha) for at, sha in reviews if at < seg["start"]), default=None
            )
            if (
                match
                and review
                and not review[1].startswith(match[0])
                and any(
                    sha.startswith(match[0]) and review[0] < at <= seg["start"]
                    for at, sha in revisions
                )
            ):
                recert_pairs.add((review[1], match[0]))
    counts["recert"] = len(recert_pairs)
    counts["superseded"] = sum(
        1
        for rid, spans in ready_running.items()
        if any(
            sha != run_shas[rid] and any(a <= at < b for a, b in spans)
            for at, sha in revisions
        )
    )
    counts["removed"] = sum(
        e["count"] for e in pr["waste"] if e["kind"] == "queue_removals"
    )
    return counts


def overview(
    snapshot: dict[str, Any], cache: dict[str, Any], history: dict[str, Any]
) -> dict[str, Any]:
    rows = []
    samples: dict[str, list[int]] = defaultdict(list)
    loops: Counter[str] = Counter()
    full: list[int] = []
    ready_to_merge: list[int] = []
    actual_pushes: list[int] = []
    for pr in snapshot["prs"]:
        n = pr["number"]
        end = pr["merged"] or snapshot["generated"]
        raw = cache.get(f"pr:{n}", {}).get("value", {})
        runs = cache.get(f"branch:{n}:{raw.get('headRefName')}", {}).get("value", [])
        markers, ready = ready_signals(pr, raw, end)
        intervals = strip(pr, end, markers)
        current = (
            "merged" if pr["merged"] else intervals[-1]["stage"] if intervals else "dev"
        )
        active = [
            s["phase"]
            for s in pr["segments"]
            if s["end"] is None and s["phase"] in PRIORITY and s["phase"] != "dev"
        ]
        if active and not pr["merged"]:
            current = min(active, key=PRIORITY.index)
        elif raw.get("isDraft") and not pr["merged"]:
            current = "dev"
        elif not pr["merged"] and markers and markers[-1] == end:
            current = "ready"
        rows.append(
            {
                "number": n,
                "title": pr["title"],
                "url": pr["url"],
                "current": current,
                "merged": pr["merged"],
                "first": pr["first"],
                "first_source": pr["first_source"],
                "end": end,
                "strip": intervals,
                "ready": markers,
            }
        )
        if not pr["merged"]:
            continue
        elapsed = end - pr["first"]
        full.append(elapsed)
        if pr["first_source"] == "GitHub push timestamp":
            actual_pushes.append(elapsed)
        if ready is not None:
            ready_to_merge.append(end - ready)
        durations: dict[str, list[tuple[int, int]]] = defaultdict(list)
        durations["dev"].extend(
            (s["start"], s["end"]) for s in intervals if s["stage"] == "dev"
        )
        for seg in pr["segments"]:
            if seg["phase"] not in {"dev", "merged"}:
                durations[seg["phase"]].append(
                    (
                        max(seg["start"], pr["first"]),
                        min(seg["end"] if seg["end"] is not None else end, end),
                    )
                )
        for milestone in ["dev", "merged"] + (["ready"] if markers else []):
            durations.setdefault(milestone, [])
        for phase, ranges in durations.items():
            seconds = sum(finish - start for start, finish in union(ranges))
            samples[phase].append(seconds)
        loops.update(rework(pr, raw, runs, end))
        conflict = False
        seen: set[tuple[str, str]] = set()
        for date, note in history.get("log", []):
            at = stamp(date)
            if (date, note) in seen or at is None or not pr["first"] <= at <= end:
                continue
            seen.add((date, note))
            if note.startswith(f"train #{n}:"):
                if "needs an agent to resolve" in note and "conflict" in note:
                    if not conflict:
                        loops["conflicts"] += 1
                    conflict = True
                else:
                    conflict = False
    rows.sort(
        key=lambda row: (
            row["merged"] is not None,
            -(row["merged"] or 0) if row["merged"] else ROW_ORDER.index(row["current"]),
            -row["number"],
        )
    )
    return {
        "stages": {stage: times(samples[stage]) for stage in STAGES},
        "rows": rows,
        "loops": dict(loops),
        "full": times(full),
        "push_only": times(actual_pushes),
        "ready": times(ready_to_merge),
    }
