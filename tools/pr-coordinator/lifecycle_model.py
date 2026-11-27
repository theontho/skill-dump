"""Turn timing evidence into visible intervals and non-overlapping PR-hours."""

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from typing import Any

from runtime import P

WEB = P.web
DAY = 86400
REVIEW_ESCALATION = 3
ORDER = ("dev", "review", "cert", "lane", "refresh", "checks", "hosted", "queue")
PRIORITY = ("queue", "hosted", "refresh", "cert", "review", "lane", "checks", "dev")


def stamp(value):
    return (
        int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        if value
        else None
    )


def review_rounds(events, reviewer_pattern):
    """Count distinct commits with a submitted Copilot review."""
    return len(
        {
            head
            for row in events
            if row["__typename"] == "PullRequestReview"
            and row.get("submittedAt")
            and row.get("state")
            in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED"}
            and reviewer_pattern.fullmatch((row.get("author") or {}).get("login") or "")
            and (head := (row.get("commit") or {}).get("oid"))
        }
    )


def weekly_reviews(rows, now):
    results = sorted(
        row["rounds"]
        for row in rows
        if row["merged"] is not None and now - 7 * DAY <= row["merged"] <= now
    )
    n = len(results)
    return {
        "prs": n,
        "p90": results[math.ceil(n * 0.9) - 1] if n else None,
        "four_or_more": sum(count >= 4 for count in results),
    }


def union(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    output: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if b < a:
            continue
        if output and a <= output[-1][1]:
            output[-1] = (output[-1][0], max(b, output[-1][1]))
        else:
            output.append((a, b))
    return output


def minus(
    ranges: Iterable[tuple[int, int]], exclusions: Sequence[tuple[int, int]]
) -> Iterator[tuple[int, int]]:
    for a, b in ranges:
        for x, y in exclusions:
            if y <= a:
                continue
            if x >= b:
                break
            if x > a:
                yield a, min(x, b)
            a = max(a, y)
        if b > a:
            yield a, b


def lane(job):
    for source in (job["name"].lower(), " ".join(job.get("labels") or []).lower()):
        for pattern, name in (
            ("windows", "Windows"),
            ("macos|darwin", "macOS"),
            ("arm64|aarch64|ubuntu-.*-arm", "Linux ARM64"),
            ("ubuntu|linux", "Linux x64"),
        ):
            if re.search(pattern, source):
                return name
    return "Other"


def outcome(conclusion):
    if conclusion in {"cancelled", "stale", "aborted", "superseded"}:
        return "cancelled"
    if conclusion in {
        "failure",
        "error",
        "fail",
        "timed_out",
        "startup_failure",
        "action_required",
    }:
        return "failed"
    return "running"


def coordinator(number, history, train, holder, merged, finish, add):
    transitions = []
    for date, msg in history["log"]:
        if match := re.match(rf"train #{number}: (\w+): (.*)", msg):
            transitions.append((stamp(date), *match.groups()))
        elif msg.startswith(f"lane: #{number} waiting"):
            transitions.append(
                (stamp(date), "waiting", "Waiting for a conflict-free lane slot")
            )
        elif msg.startswith(f"lane: #{number} merges cleanly"):
            transitions.append((stamp(date), "granted", "Lane slot granted"))
    if train and train.get("at"):
        transitions.append((stamp(train["at"]), train["stage"], train.get("note", "")))
    if holder and not merged:
        transitions.append((holder, "granted", "Lane slot held"))
    transitions.sort(key=lambda t: t[0])
    current = None
    for at, stage, note in [*transitions, (finish, "terminal", "")]:
        if at > finish:
            continue
        if stage == "granted" and (not current or current[0] != "lane"):
            continue
        text = note.lower()
        phase = None
        if stage == "waiting":
            phase = (
                "lane"
                if "slot" in text or "lane" in text
                else "checks"
                if "pr checks" in text
                else None
            )
        elif stage == "fixing":
            phase = "refresh"
        if current and current[0] == phase:
            continue
        if current:
            previous, start, label = current
            activity = "running" if previous == "refresh" else "waiting"
            end = None if stage == "terminal" and not merged else at
            add(
                previous,
                start,
                end,
                label,
                activity,
                outcome("fail") if stage == "blocked" else activity,
                note=f"Coordinator log · {note}"
                if stage == "blocked"
                else "Coordinator log · minute precision",
            )
            if stage == "blocked":
                add(previous, at, at, note, status="failed")
        current = (phase, at, note) if phase else None


def certifications(number, history, statuses, head, merged, latest, add, waste):
    groups = defaultdict(list)
    for sha, entries in statuses.items():
        for entry in entries:
            groups[sha, entry["context"].removeprefix(P.cert_prefix)].append(
                (
                    stamp(entry["created_at"]),
                    entry["state"],
                    entry["target_url"],
                    "GitHub commit status",
                )
            )
    names = {
        "macos": "macos-arm64",
        "windows": "windows-x64",
        "linux": "linux-x64",
        "local-linux": "linux-x64",
    }
    for cert in history["certs"]:
        if cert["pr"] != number or not cert.get("sha"):
            continue
        short, target = cert["sha"], cert["target"]
        sha = next((s for s in statuses if s.startswith(short)), short)
        layer = P.get("certification", "layers", {}).get(
            target, names.get(target, target)
        )
        if layer == "platform":
            layer = P.hosts.get(cert.get("host"), {}).get(
                "platform", names.get(target, target)
            )
        groups[sha, layer].append(
            (
                stamp(cert["at"]),
                cert["status"],
                None,
                "Coordinator certification record",
            )
        )
    for date, message in history["log"]:
        match = re.match(
            rf"PR #{number}: ([\w-]+) cert exit (\d+) on ([a-f0-9]+)", message
        )
        if match:
            target, code, short = match.groups()
            platform = names.get(target, target)
            sha = next((s for s in statuses if s.startswith(short)), short)
            at = stamp(date)
            if not any(
                abs(at - old) < 120 and state not in {"running", "pending", "queued"}
                for old, state, _, _ in groups[sha, platform]
            ):
                groups[sha, platform].append(
                    (
                        at,
                        "success" if code == "0" else "failure",
                        None,
                        "Coordinator certification exit",
                    )
                )
    for (sha, platform), milestones in groups.items():
        valid = head.startswith(sha)
        pending = running = last_terminal = None
        name = f"Certification · {platform}"
        commit = f"{WEB}/commit/{sha}"
        for at, state, url, source in sorted(milestones, key=lambda m: m[0]):
            detail = f"{source} · {sha[:9]}" + (
                " · superseded head" if not valid else ""
            )
            url = url or commit
            if state == "queued":
                pending = pending if pending is not None else at
            elif state in {"running", "pending"}:
                if pending is not None and pending <= at:
                    add(
                        "cert",
                        pending,
                        at,
                        name,
                        "waiting",
                        url=url,
                        note=detail,
                        lane=platform,
                    )
                    pending = None
                running = running if running is not None else at
            else:
                # GitHub and the coordinator may both record the same finish.
                if (
                    last_terminal is not None
                    and at - last_terminal < 120
                    and running is None
                    and pending is None
                ):
                    continue
                result = outcome(state)
                if running is not None and at >= running:
                    add(
                        "cert",
                        running,
                        at,
                        name,
                        status=result,
                        url=url,
                        note=detail,
                        lane=platform,
                    )
                elif pending is not None:
                    add(
                        "cert",
                        pending,
                        at,
                        name,
                        "waiting",
                        result,
                        url,
                        note=detail,
                        lane=platform,
                    )
                else:
                    add(
                        "cert",
                        at,
                        at,
                        f"{name}: {state}; no start observed",
                        status=result,
                        url=url,
                        note=detail,
                        lane=platform,
                    )
                if (
                    not valid
                    and state not in {"skipped", "superseded"}
                    and (
                        running is not None
                        or state
                        in {
                            "pass",
                            "success",
                            "failure",
                            "fail",
                            "error",
                        }
                    )
                ):
                    waste("superseded_certs", name, at, url, note=detail)
                pending = running = None
                last_terminal = at
        detail = f"{sha[:9]}" + (" · superseded head" if not valid else "")
        if pending is not None or running is not None:
            start = running if running is not None else pending
            activity = "running" if running is not None else "waiting"
            ended = merged if valid else latest(sha, start) or merged
            result = "cancelled" if not valid else activity
            if ended or valid:
                add(
                    "cert",
                    start,
                    ended,
                    name,
                    activity,
                    result,
                    commit,
                    note=detail,
                    lane=platform,
                )
            else:
                add(
                    "cert",
                    start,
                    start,
                    f"{name}: final time unknown",
                    status=result,
                    url=commit,
                    note=detail,
                    lane=platform,
                )


def build(
    number, pr, runs, job_lists, checks, statuses, groups, history, train, holder, now
):
    link = f"{WEB}/pull/{number}"
    created = stamp(pr["createdAt"])
    events = pr["events"]
    by_time = sorted(
        (stamp(e["createdAt"]), i, e)
        for i, e in enumerate(events)
        if e.get("createdAt")
    )
    merged = stamp(pr["mergedAt"]) or next(
        (t for t, _, e in by_time if e["__typename"] == "MergedEvent"), None
    )
    finish = merged or now
    pushes = [
        (stamp(commit["pushedDate"]), "GitHub push timestamp")
        for e in events
        if (commit := e.get("commit"))
        and commit.get("pushedDate")
        and created - 7 * DAY <= stamp(commit["pushedDate"]) <= created
    ]
    first, source = min(
        pushes, default=(created, "PR opened (first push not exposed by GitHub)")
    )
    segments: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    wasted: list[dict[str, Any]] = []

    def add(
        phase,
        start,
        end,
        label,
        activity="running",
        status=None,
        url=link,
        detail=False,
        **extra,
    ):
        if (
            start is None
            or start > finish
            or (end is not None and (end < start or end < first))
        ):
            return
        (jobs if detail else segments).append(
            {
                "phase": phase,
                "start": max(first, start),
                "end": min(end, finish) if end is not None else (merged or None),
                "label": label,
                "activity": activity,
                "status": status or activity,
                "url": url or link,
                **extra,
            }
        )

    def waste(kind, label, at, url, count=1, **extra):
        if first <= at <= finish:
            wasted.append(
                {
                    "kind": kind,
                    "label": label,
                    "at": at,
                    "url": url,
                    "count": count,
                    **extra,
                }
            )

    transitions = [
        e["__typename"]
        for _, _, e in by_time
        if e["__typename"]
        in {
            "ReadyForReviewEvent",
            "ConvertToDraftEvent",
        }
    ]
    initially_draft = (
        transitions[0] == "ReadyForReviewEvent" if transitions else pr["isDraft"]
    )
    ready_start = None if initially_draft else created
    ready, queue = [], []
    requested = entered = None
    movements = [(stamp(r["created_at"]), r["head_sha"]) for r in runs]
    for at, _, e in by_time:
        kind = e["__typename"]
        if kind == "ReadyForReviewEvent" and ready_start is None:
            ready_start = at
        elif kind == "ConvertToDraftEvent" and ready_start is not None:
            ready.append((ready_start, at))
            ready_start = None
        elif (
            kind == "ReviewRequestedEvent"
            and "copilot"
            in ((e.get("requestedReviewer") or {}).get("login") or "").lower()
        ):
            if requested is not None:
                add(
                    "review",
                    requested,
                    at,
                    "Copilot request replaced",
                    "waiting",
                    "cancelled",
                )
            requested = at
        elif (
            kind == "PullRequestReview"
            and "copilot" in ((e.get("author") or {}).get("login") or "").lower()
        ):
            if (submitted := stamp(e.get("submittedAt"))) is not None:
                add(
                    "review",
                    min(requested or at, submitted),
                    submitted,
                    "Copilot review turnaround",
                    "waiting",
                    url=e["url"],
                    note="Request to submission, or review creation to submission if no request was visible.",
                )
                requested = None
        elif kind == "AddedToMergeQueueEvent":
            if entered is not None:
                queue.append((entered, at, "re-enqueued"))
            entered = at
        elif kind == "RemovedFromMergeQueueEvent":
            reason = e["reason"] or "unknown reason"
            if entered is not None:
                queue.append((entered, at, reason))
                entered = None
            if reason != "merged":
                label = f"Removed from queue: {reason}"
                waste("queue_removals", label, at, link)
                add("queue", at, at, label, status="failed")
        elif kind == "HeadRefForcePushedEvent" and e.get("afterCommit"):
            movements.append((at, e["afterCommit"]["oid"]))
    if ready_start is not None:
        ready.append((ready_start, finish + 1))
    if requested is not None:
        add("review", requested, merged, "Waiting for Copilot", "waiting")
    if entered is not None:
        queue.append((entered, merged, "merged" if merged else None))

    def latest(sha, start):
        return min(
            (at for at, other in movements if at > start and not other.startswith(sha)),
            default=None,
        )

    ready_by_sha = defaultdict(list)
    for run in runs:
        created_run = stamp(run["created_at"])
        suffix = run["name"].lower()
        if created_run < first or created_run > finish or suffix.endswith("/ draft"):
            continue
        if suffix.endswith("/ ready") or any(a <= created_run < b for a, b in ready):
            ready_by_sha[run["head_sha"]].append(run)
    queue_ids = set()
    for begin, end, reason in queue:
        stop = end or finish
        occupied = []
        for run in groups:
            at = stamp(run["created_at"])
            if not begin <= at <= stop or run["id"] in queue_ids:
                continue
            queue_ids.add(run["id"])
            url = run["html_url"]
            result = outcome(run["conclusion"])
            started = stamp(run["run_started_at"])
            done = stamp(run["updated_at"]) if run["status"] == "completed" else None
            if (
                started
                and started <= stop
                and run["status"] not in {"queued", "waiting", "requested", "pending"}
            ):
                occupied.append((started, min(done or stop, stop)))
                add(
                    "queue",
                    started,
                    min(done or stop, stop) if done or end else None,
                    "Merge-group checks",
                    status=result,
                    url=url,
                    run=run["id"],
                )
            if done and result != "running" and done <= stop:
                add(
                    "queue",
                    done,
                    done,
                    "Merge-group checks",
                    status=result,
                    url=url,
                    run=run["id"],
                )
            if result == "cancelled":
                waste("cancelled_runs", "Cancelled merge-group run", at, url)
            if run["run_attempt"] > 1:
                waste("reruns", "Merge-group checks", at, url, run["run_attempt"] - 1)
            for job in job_lists.get(run["id"], []):
                job_parts("queue", job, run, lane(job), False, finish, add)
        for start, finish_wait in minus([(begin, stop)], union(occupied)):
            add(
                "queue",
                start,
                None if end is None and finish_wait == now else finish_wait,
                "In merge queue",
                "waiting",
                note=f"Queue exit: {reason}"
                if reason
                else "Awaiting merge-group checks.",
            )

    for sha, ready_runs in ready_by_sha.items():
        superseded = sha != pr["headRefOid"]
        for run in ready_runs:
            at, rid, url = stamp(run["created_at"]), run["id"], run["html_url"]
            if rid not in job_lists:
                continue
            note = f"Head {sha[:9]}" + (" · superseded" if superseded else "")
            selected = job_lists[rid]
            if not selected:
                selected = [
                    dict(check, html_url=check["details_url"])
                    for check in checks.get(sha, [])
                    if f"/actions/runs/{rid}/" in (check.get("details_url") or "")
                ]
            if run["conclusion"] == "cancelled" or any(
                j["conclusion"] == "cancelled"
                and (j.get("run_attempt") or 1) < run["run_attempt"]
                for j in selected
            ):
                waste("cancelled_runs", run["name"], at, url, note=note)
            if superseded and any(
                j["conclusion"] not in {"skipped", "neutral"} for j in selected
            ):
                waste("superseded_runs", run["name"], at, url, note=note)
            if run["run_attempt"] > 1:
                waste("reruns", run["name"], at, url, run["run_attempt"] - 1, note=note)
            lanes = defaultdict(list)
            for j in selected:
                os_lane = lane(j)
                lanes[os_lane].extend(
                    job_parts("hosted", j, run, os_lane, superseded, finish, add)
                )
            for os_lane, pieces in lanes.items():
                active = union((a, b) for a, b, mode in pieces if mode == "running")
                waiting = union((a, b) for a, b, mode in pieces if mode == "waiting")
                for mode, ranges in [
                    ("running", active),
                    ("waiting", minus(waiting, active)),
                ]:
                    for a, b in ranges:
                        end = None if b == now and run["status"] != "completed" else b
                        label = f"{os_lane} · " + (
                            "hosted jobs"
                            if mode == "running"
                            else "waiting for a runner"
                        )
                        add(
                            "hosted",
                            a,
                            end,
                            label,
                            mode,
                            url=url,
                            lane=os_lane,
                            note=note,
                            run=rid,
                        )
            if not any(lanes.values()) and run["status"] in {
                "queued",
                "requested",
                "pending",
                "waiting",
            }:
                add(
                    "hosted",
                    at,
                    None,
                    "Workflow queued; jobs not yet visible",
                    "waiting",
                    url=url,
                    note=note,
                )
            elif (
                not any(lanes.values())
                and not selected
                and run["conclusion"] not in {"skipped", "neutral"}
            ):
                started = stamp(run["run_started_at"])
                done = (
                    stamp(run["updated_at"]) if run["status"] == "completed" else None
                )
                add(
                    "hosted",
                    started,
                    done,
                    "Workflow activity; job timings unavailable",
                    status=outcome(run["conclusion"]),
                    url=url,
                    note=note,
                    run=rid,
                )
            if outcome(run["conclusion"]) != "running":
                done = stamp(run["updated_at"])
                add(
                    "hosted",
                    done,
                    done,
                    run["name"],
                    status=outcome(run["conclusion"]),
                    url=url,
                    note=note,
                )

    coordinator(number, history, train, holder, merged, finish, add)
    certifications(
        number, history, statuses, pr["headRefOid"], merged, latest, add, waste
    )
    for a, b in minus(
        [(first, finish)], union((s["start"], s["end"] or finish) for s in segments)
    ):
        add(
            "dev",
            a,
            None if b == now and not merged else b,
            "Draft / development and unrecorded time",
            "unknown",
            "waiting",
            note=source,
        )
    if merged:
        add("merged", merged, merged, "Merged")
    return {
        "number": number,
        "title": pr["title"],
        "url": link,
        "state": "merged" if merged else "draft" if pr["isDraft"] else "open",
        "first": first,
        "first_source": source,
        "merged": merged,
        "segments": sorted(segments, key=lambda s: s["start"]),
        "jobs": jobs,
        "waste": wasted,
    }


def job_parts(phase, job, run, os_lane, superseded, finish, add):
    result = outcome(job["conclusion"])
    if job["conclusion"] in {"skipped", "neutral"}:
        return []
    queued, started, done = [
        stamp(job.get(key)) for key in ("created_at", "started_at", "completed_at")
    ]
    if started and done and started > done:
        started = None
    if job.get("status") in {"queued", "waiting", "requested", "pending"}:
        started = None
    url = job.get("html_url") or run["html_url"]
    attempt = job.get("run_attempt") or run["run_attempt"]
    info = {
        "lane": os_lane,
        "job": str(job["id"]),
        "run": run["id"],
        "note": f"{run['head_sha'][:9]} · attempt {attempt}"
        + (" · superseded head" if superseded else ""),
    }
    pieces = []
    if queued and (started is None or started > queued):
        add(
            phase,
            queued,
            started or done,
            job["name"],
            "waiting",
            result if done and not started else "waiting",
            url,
            detail=True,
            **info,
        )
        pieces.append((queued, started or done or finish, "waiting"))
    if started:
        add(
            phase,
            started,
            done,
            job["name"],
            status=result,
            url=url,
            detail=True,
            **info,
        )
        pieces.append((started, done or finish, "running"))
    return pieces


def summarize(prs):
    merged = [pr for pr in prs if pr["merged"]]
    aggregate: dict[tuple[str, str], int] = defaultdict(int)
    samples: dict[str, list[int]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    for pr in merged:
        edges = defaultdict(list)
        for index, segment in enumerate(pr["segments"]):
            a, b = segment["start"], segment["end"] or pr["merged"]
            if a < b:
                edges[a].append((index, True))
                edges[b].append((index, False))
        totals: dict[str, int] = defaultdict(int)
        active: set[int] = set()
        previous = pr["first"]
        for at, changes in sorted(edges.items()):
            if active and at > previous:
                selected = min(
                    (pr["segments"][i] for i in active),
                    key=lambda s: (
                        PRIORITY.index(s["phase"]),
                        s["activity"] != "running",
                    ),
                )
                key = selected["phase"], selected["activity"]
                aggregate[key] += at - previous
                totals[key[0]] += at - previous
            previous = at
            for index, start in changes:
                (active.add if start else active.discard)(index)
        for phase, seconds in totals.items():
            samples[phase].append(seconds)
        for event in pr["waste"]:
            counts[event["kind"]] += event["count"]
    rows = []
    for phase in ORDER:
        observed = sorted(samples[phase])
        n = len(observed)
        if n:
            rows.append(
                {
                    "phase": phase,
                    "prs": n,
                    "median": (observed[(n - 1) // 2] + observed[n // 2]) // 2,
                    "p90": observed[math.ceil(n * 0.9) - 1],
                    **{
                        mode: aggregate[phase, mode]
                        for mode in ("waiting", "running", "unknown")
                    },
                }
            )
    return {"merged": len(merged), "phases": rows, "waste": dict(counts)}
