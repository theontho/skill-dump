"""Summarize immutable run attempts and JUnit observations without inventing outcomes."""

from __future__ import annotations

import ast
import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from runtime import P
from tests_inventory import (
    CACHE,
    DAYS,
    HERE,
    ROOT,
    UTC,
    api,
    load,
    parse,
    read_activity,
    save,
)
from tests_policy import NIGHTLY, RELEASE, family, job_purpose, stage, workflow_map
from tests_reports import ENV, REPORTS, pr_number

ANNOTATIONS = HERE / "tests_inventory_annotations"
INFRA = re.compile(
    r"lost (?:communication|connection)|runner (?:has |is )?(?:lost|disconnected|unavailable)|"
    r"failed to acquire runner|failed to start the runner|job was abandoned|"
    r"job .* exceeded (?:the )?maximum execution time|the operation was canceled due to a timeout|"
    r"upstream package mirror unreachable|fetching artifact metadata failed|"
    r"github actions service (?:is |was )?(?:unavailable|degraded)",
    re.I,
)
GATES = set(P.get("landing", "aggregate_checks", ["required"]))
SUITE_FILES = set(P.get("tests", "suite_workflows", []))
BUCKETS = P.get("tests", "areas", {})


def repo_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, text=True, capture_output=True
    ).stdout


def area(file: str) -> str:
    name = Path(file).name
    return next(
        (key for key, pattern in BUCKETS.items() if re.search(pattern, name)),
        "Other functionality",
    )


def source_change(files: set[str]) -> dict[str, str]:
    """Last changes on HEAD to directly imported product modules; no guess for external tools."""
    modules: dict[str, str] = {}
    import_roots = [
        ROOT / root for root in P.get("tests", "python_import_roots", ["src", "."])
    ]
    source_roots = P.get("tests", "source_roots", ["src", "scripts"])
    for root in source_roots:
        for source in (ROOT / root).rglob("*.py"):
            for parent in import_roots:
                if source.is_relative_to(parent):
                    name = ".".join(
                        source.relative_to(parent).with_suffix("").parts
                    ).removesuffix(".__init__")
                    modules[name] = source.relative_to(ROOT).as_posix()
                    break
    dependencies: dict[str, set[str]] = {}
    for file in files:
        current = ROOT / file
        try:
            tree = ast.parse(current.read_bytes())
        except (OSError, SyntaxError):
            continue
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{a.name}" for a in node.names)
        dependencies[file] = {modules[k] for k in imported if k in modules}
    wanted = set().union(*dependencies.values())
    recent = {}
    if wanted:
        sha = repo_git("rev-parse", "HEAD").strip()
        path = HERE / "tests_inventory_git.json"
        saved = load(path, {})
        if saved.get("head") == sha and wanted <= set(saved.get("changes", {})):
            recent = saved["changes"]
        else:
            date = ""
            for line in repo_git(
                "log",
                "--format=@%as",
                "--name-only",
                "HEAD",
                "--",
                *source_roots,
            ).splitlines():
                if line.startswith("@"):
                    date = line[1:]
                elif line in wanted and line not in recent:
                    recent[line] = date
            save(
                path,
                {
                    "head": sha,
                    "changes": {item: recent.get(item, "") for item in wanted},
                },
            )
    return {
        file: max((recent.get(dep, "") for dep in deps), default="")
        for file, deps in dependencies.items()
    }


def dt_fields(job: dict[str, Any]) -> tuple[float, float] | None:
    if not job["runner_name"] or not job["started_at"] or not job["completed_at"]:
        return None
    start, end = (
        parse(job["started_at"]).timestamp(),
        parse(job["completed_at"]).timestamp(),
    )
    return (start, end) if end >= start else None


def annotation(job: dict[str, Any], refresh: bool) -> list[str]:
    ident = (job.get("check_run_url") or "").rsplit("/", 1)[-1]
    if not ident.isdigit():
        return []
    path = ANNOTATIONS / f"{ident}.json"
    items = load(path, None)
    if items is None and refresh:
        try:
            items = [
                a.get("message") or ""
                for a in api(f"check-runs/{ident}/annotations", "__array__")
                if a.get("annotation_level") == "failure"
            ]
            save(path, items)
        except (RuntimeError, subprocess.SubprocessError):
            return []
    return items or []


def bucket(outcome: str | None) -> str:
    if outcome in {"success", "cancelled", "skipped"}:
        return outcome
    return (
        "failure"
        if outcome
        in {"failure", "timed_out", "startup_failure", "action_required", "stale"}
        else "other"
    )


def jobs_summary(
    activity: dict[str, Any], *, fetch_annotations: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    categorized: dict[
        tuple[str, str, str], list[tuple[dict[str, Any], list[dict[str, Any]]]]
    ] = defaultdict(list)
    statuses: Counter[str] = Counter()
    for run in activity.get("runs", []):
        workflow = Path(run["path"].split("@", 1)[0]).name
        key = stage(workflow, run["event"], run.get("head_branch") or "")
        statuses["records"] += 1
        if run["status"] != "completed":
            statuses["pending"] += 1
        if run["conclusion"] == "skipped":
            categorized[key, workflow, "Whole workflow skipped"].append((run, []))
            statuses["workflow_skips"] += 1
        elif run["status"] == "completed" and not run.get("attempts"):
            statuses["missing"] += 1
        for attempt, batch in run.get("attempts", {}).items():
            grouped = defaultdict(list)
            for job in batch:
                row = {
                    **job,
                    "run": run["id"],
                    "at": run["created_at"],
                    "attempt": int(attempt),
                    "sha": run["head_sha"],
                    "pr": pr_number(run),
                    "stage": key,
                    "workflow": workflow,
                }
                observations.append(row)
                grouped[family(job["name"])].append(row)
            for name, jobs in grouped.items():
                categorized[key, workflow, name].append((run, jobs))

    failed_jobs = [j for j in observations if bucket(j["conclusion"]) == "failure"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        annotations = dict(
            zip(
                (j["id"] for j in failed_jobs),
                pool.map(lambda j: annotation(j, fetch_annotations), failed_jobs),
                strict=True,
            )
        )

    by_name = defaultdict(list)
    by_pr = defaultdict(list)
    for job in observations:
        match = job["workflow"], job["stage"], job["name"]
        by_name[match + (job["sha"],)].append(job)
        if job["pr"] is not None and job["stage"] == "ready":
            by_pr[match + (job["pr"],)].append(job)

    def order(job: dict[str, Any]) -> tuple[str, int, int]:
        return job["started_at"] or job["at"], job["attempt"], job["id"]

    def classify(job: dict[str, Any]) -> str:
        notes = annotations[job["id"]]
        if job["conclusion"] in {"timed_out", "startup_failure", "stale"} or any(
            INFRA.search(n) or ENV.search(n) for n in notes
        ):
            return "infra"
        if job["name"] in GATES:
            return "summary"
        match = job["workflow"], job["stage"], job["name"]
        later = [j for j in by_name[match + (job["sha"],)] if order(j) > order(job)]
        if any(j["conclusion"] == "success" for j in later):
            return "flaky"
        signature = {
            note
            for note in notes
            if not re.fullmatch(r"Process completed with exit code \d+\.", note)
        }
        if signature and any(
            signature.intersection(annotations.get(j["id"], [])) for j in later
        ):
            return "real"
        if (
            job["stage"] == "ready"
            and job["pr"] is not None
            and any(
                order(j) > order(job)
                and j["conclusion"] == "success"
                and j["sha"] != job["sha"]
                for j in by_pr[match + (job["pr"],)]
            )
        ):
            return "real"
        return "unknown"

    classifications = {j["id"]: classify(j) for j in failed_jobs}
    rows = []
    for (key, workflow, name), runs in sorted(categorized.items()):
        outcomes: Counter[str] = Counter()
        signals: Counter[str] = Counter()
        wall, runner = [], []
        for _, jobs in runs:
            jobs_states = {bucket(j["conclusion"]) for j in jobs} or {"skipped"}
            outcomes[
                next(
                    state
                    for state in ("failure", "cancelled", "success", "other", "skipped")
                    if state in jobs_states
                )
            ] += 1
            job_spans = [span for j in jobs if (span := dt_fields(j))]
            if job_spans:
                wall.append(
                    (max(b for _, b in job_spans) - min(a for a, _ in job_spans)) / 60
                )
                runner.append(sum(b - a for a, b in job_spans) / 60)
            signals.update(
                classifications[j["id"]] for j in jobs if j["id"] in classifications
            )
        rows.append(
            {
                "stage": key,
                "workflow": workflow,
                "name": name,
                "why": (
                    "The run was skipped without allocating a runner; individual job outcomes were not requested."
                    if name == "Whole workflow skipped"
                    else job_purpose(workflow, name)
                ),
                "outcomes": dict(outcomes),
                "signals": dict(signals),
                "attempts": len(runs),
                "allocated": len(runner),
                "wall": round(statistics.median(wall), 2) if wall else None,
                "minutes": round(statistics.median(runner), 2) if runner else None,
                "hours": round(sum(runner) / 60, 2),
                "per_day": round(len(runner) / DAYS, 2),
            }
        )
    return rows, dict(statuses)


def test_tiers(files):
    command = P.command("tests", "tiers_command")
    if not command:
        return {}
    result = subprocess.run(
        command,
        input=json.dumps(sorted(files)),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return json.loads(result.stdout)


def evidence_summary(
    report_data: dict[str, Any], runs: list[dict[str, Any]]
) -> dict[str, Any]:
    latest_windows = defaultdict(set)
    for run in runs:
        if run["event"] == "pull_request" and (pr := pr_number(run)) is not None:
            latest_windows[run["head_sha"]].add(pr)

    indexed: dict[tuple[str, str], list[tuple[dict[str, Any], list[Any]]]] = (
        defaultdict(list)
    )
    reports = report_data.get("reports", [])
    for report in reports:
        if report["stage"] == "ready" and report["pr"] is not None:
            latest_windows[report["sha"]].add(report["pr"])
    for report in reports:
        candidates = latest_windows[report["sha"]]
        if report["stage"] == "local" and len(candidates) == 1:
            report["pr"] = next(iter(candidates))
        for case in report["tests"]:
            indexed[case[0], case[1]].append((report, case))

    tracked = {
        file
        for file in repo_git(
            "ls-files", *P.get("tests", "test_roots", ["tests"])
        ).splitlines()
        if re.fullmatch(
            P.get("tests", "test_file_pattern", r"tests/(?:[^/]+/)*test_[^/]+\.py"),
            file,
        )
    }
    tiers = test_tiers(tracked | {file for file, _ in indexed})

    def test_tier(file):
        return tiers.get(file, {}).get("tier", "unclassified")

    changes = source_change(tracked)
    file_entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    area_entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tier_entries: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def rollup(items: list[dict[str, Any]]) -> dict[str, Any]:
        outcomes: Counter[str] = Counter()
        signals: Counter[str] = Counter()
        lanes: Counter[str] = Counter()
        durations: dict[str, float] = defaultdict(float)
        for item in items:
            outcomes.update(item["outcomes"])
            signals.update(item["signals"])
            lanes.update(item["lanes"])
            for report_id, seconds in zip(
                item["_report_ids"], item["_times"], strict=True
            ):
                durations[report_id] += seconds
        return {
            "tests": len(items),
            "runs": outcomes.total(),
            "reports": len(durations),
            "outcomes": dict(outcomes),
            "signals": dict(signals),
            "prs": len({pr for item in items for pr in item["_prs"]}),
            "lanes": dict(lanes),
            "seconds": round(sum(durations.values()), 2),
            "median": round(statistics.median(durations.values()), 3)
            if durations
            else 0,
            "premerge": round(sum(item["premerge"] for item in items), 2),
        }

    for (file, name), observations in indexed.items():
        observations.sort(key=lambda x: (x[0]["at"], x[0]["id"]))
        signals: Counter[str] = Counter()
        outcomes: Counter[str] = Counter()
        lanes: Counter[str] = Counter()
        prs = set()
        nightly_lanes = set()
        premerge_lanes: dict[str, float] = defaultdict(float)
        times = []
        for i, (report, case) in enumerate(observations):
            result, elapsed, infra, fingerprint = case[2:]
            outcomes[result] += 1
            times.append(elapsed)
            lane_key = (
                report["lane"]
                if report["stage"] != "local"
                else f"{report['source']} · {report['lane']}"
            )
            if result != "skip":
                lanes[lane_key] += 1
            if result == "pass" and report["stage"] == "nightly":
                nightly_lanes.add(lane_key)
            if result == "pass" and report["stage"] in {"ready", "queue"}:
                premerge_lanes[lane_key] += elapsed
            if result != "fail":
                continue
            if infra:
                signals["infra"] += 1
                continue
            match = report["source"], report["lane"], report["stage"]
            later = [
                (r, c)
                for r, c in observations[i + 1 :]
                if (r["source"], r["lane"], r["stage"]) == match
                and r["id"] != report["id"]
            ]
            same_sha = [
                (r, c) for r, c in later if report["sha"] and r["sha"] == report["sha"]
            ]
            if any(c[2] == "pass" for _, c in same_sha):
                signals["flaky"] += 1
            elif (
                report["pr"] is not None
                and report["stage"] in {"ready", "local"}
                and report["sha"]
                and any(
                    r["pr"] == report["pr"]
                    and r["sha"]
                    and r["sha"] != report["sha"]
                    and c[2] == "pass"
                    for r, c in later
                )
            ):
                prs.add(report["pr"])
                signals["real"] += 1
            elif fingerprint and any(
                c[2] == "fail" and c[5] == fingerprint and not c[4] for _, c in same_sha
            ):
                signals["persistent"] += 1
            else:
                signals["unknown"] += 1
        row = {
            "file": file,
            "name": name,
            "area": area(file),
            "changed": changes.get(file, ""),
            "outcomes": dict(outcomes),
            "signals": dict(signals),
            "runs": len(observations),
            "seconds": round(sum(times), 2),
            "median": round(statistics.median(times), 3),
            "prs": len(prs),
            "pr_ids": sorted(prs),
            "_prs": prs,
            "lanes": dict(lanes),
            "_times": times,
            "_report_ids": [report["id"] for report, _ in observations],
            "premerge": round(sum(premerge_lanes[k] for k in nightly_lanes), 2),
            "overlap": sorted(nightly_lanes.intersection(premerge_lanes)),
        }
        file_entries[file].append(row)
        area_entries[area(file)].append(row)
        tier_entries[test_tier(file)].append(row)

    def source_files(kind: str, name: str) -> int:
        label = area if kind == "area" else test_tier
        return sum(label(file) == name for file in tracked)

    files = [
        {
            "name": file,
            "area": area(file),
            "tier": test_tier(file),
            "changed": changes.get(file, ""),
            "portable": tiers.get(file, {}).get("portable", False),
            **rollup(items),
        }
        for file, items in file_entries.items()
    ]
    all_items = [item for items in file_entries.values() for item in items]
    tests = [
        {k: v for k, v in item.items() if not k.startswith("_")} for item in all_items
    ]
    files.sort(key=lambda item: -item["seconds"])
    tests.sort(key=lambda item: (-item["seconds"], item["file"], item["name"]))
    return {
        "sources": report_data.get("sources", []),
        "from": report_data.get("from"),
        "to": report_data.get("to"),
        "tests": tests,
        "files": files,
        "tracked_files": len(tracked),
        "totals": rollup(all_items),
        "areas": [
            {"name": name, "files": source_files("area", name), **rollup(items)}
            for name, items in sorted(area_entries.items())
        ],
        "tiers": [
            {"name": name, "files": source_files("tier", name), **rollup(items)}
            for name, items in sorted(tier_entries.items())
        ],
    }


def recommendations(
    rows: list[dict[str, Any]], evidence: dict[str, Any]
) -> dict[str, Any]:
    expensive = sorted(
        (
            row
            for row in rows
            if row["hours"] >= 2
            and row["allocated"] >= 5
            and not row["outcomes"].get("failure")
            and row["stage"] not in {"release", "nightly"}
            and row["workflow"] not in RELEASE
            and not (row["workflow"] in NIGHTLY and row["stage"] == "main")
            and not row["name"].startswith(("install /", "install-lab /"))
        ),
        key=lambda row: -row["hours"],
    )
    flaky = sorted(
        (
            row
            for row in rows
            if row["signals"].get("flaky", 0) >= 2
            and row["signals"]["flaky"] > sum(row["signals"].values()) / 2
        ),
        key=lambda row: -row["signals"]["flaky"],
    )
    move = sorted(
        (
            test
            for test in evidence["tests"]
            if test["premerge"]
            and test["outcomes"].get("pass", 0) >= 15
            and not test["outcomes"].get("fail")
        ),
        key=lambda test: -test["premerge"],
    )[:20]
    return {
        "expensive": expensive[:5],
        "flaky": flaky[:5],
        "move": move,
        "move_hours": round(sum(t["premerge"] for t in move) / 3600, 3),
    }


def local_jobs(report_data: dict[str, Any]) -> list[dict[str, Any]]:
    start = (datetime.now(UTC) - timedelta(days=DAYS)).timestamp()
    durations = defaultdict(list)
    outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    for source in report_data.get("sources", []):
        for at, layer, seconds, *details in source.get("layers", []):
            if at >= start:
                outcomes[layer][details[0] if details else "success"] += 1
                if seconds is not None:
                    durations[layer].append(seconds / 60)
    return [
        {
            "stage": "local",
            "workflow": "Certification",
            "name": layer,
            "why": f"Checks the matching PR head on an owned machine. Duration known for {len(times)} of {counts.total()} retained outcomes.",
            "outcomes": dict(counts),
            "signals": {"unknown": counts["failure"]} if counts["failure"] else {},
            "allocated": counts.total(),
            "wall": round(statistics.median(times), 2) if times else None,
            "minutes": round(statistics.median(times), 2) if times else None,
            "hours": round(sum(times) / 60, 2) if times else 0,
            "per_day": round(counts.total() / DAYS, 2),
        }
        for layer, counts in sorted(outcomes.items())
        for times in [durations[layer]]
    ]


def build(*, fetch_annotations: bool = False) -> dict[str, Any]:
    activity = read_activity()
    reports = load(REPORTS, {})
    rows, stats = jobs_summary(activity, fetch_annotations=fetch_annotations)
    evidence = evidence_summary(reports, activity.get("runs", []))
    snapshot = {
        "policy": workflow_map(),
        "head": repo_git("rev-parse", "--short", "HEAD").strip(),
        "activity_at": activity.get("collected_at"),
        "partial": activity.get("partial", False) or bool(activity.get("errors")),
        "stats": stats,
        "jobs": [*rows, *local_jobs(reports)],
        "job_errors": len(activity.get("errors", [])),
        "historical_main_os_hours": round(
            sum(
                row["hours"]
                for row in rows
                if row["stage"] == "main" and row["workflow"] in NIGHTLY
            ),
            2,
        ),
        "evidence": evidence,
        "suggestions": recommendations(rows, evidence),
    }
    save(CACHE, snapshot)
    return snapshot
