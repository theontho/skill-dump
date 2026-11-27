"""Build the offline /tests snapshot from Actions metadata and pytest reports."""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from runtime import P, atomic_text

HERE = P.data
ROOT = P.checkout
REPO = P.repo
CACHE = HERE / "tests_inventory.json"
RUN_CACHE = HERE / "tests_inventory_runs"
ACTIVITY = HERE / "tests_inventory_activity.json"
RUN_LIST = HERE / "tests_inventory_run_list.json"
BASE = f"repos/{REPO}"
DAYS = P.get("tests", "activity_days", 7)
UTC = timezone.utc


def nearest_main():
    parser = argparse.ArgumentParser(description="Print the project's test selection.")
    parser.add_argument("files", nargs="+", help="worktree-root-relative changed paths")
    args = parser.parse_args()
    argv = P.command("tests", "nearest_command")
    if not argv:
        parser.error(
            "no tests.nearest_command configured; select targeted tests manually"
        )
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.rstrip("\n")
    return subprocess.run(
        [*argv, *args.files], cwd=root, timeout=P.get("tests", "nearest_timeout", 120)
    ).returncode


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_text(path, json.dumps(data, separators=(",", ":")))


def api(path: str, key: str, **params: str | int) -> list[dict[str, Any]]:
    cmd = [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "-X",
        "GET",
        f"{BASE}/{path}",
        "-f",
        "per_page=100",
    ]
    for name, value in params.items():
        cmd.extend(("-f", f"{name}={value}"))
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=90, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    pages = json.loads(result.stdout)
    records = [
        item for page in pages for item in (page if key == "__array__" else page[key])
    ]
    if key != "__array__" and len(records) < pages[0].get("total_count", 0):
        raise RuntimeError(
            f"Incomplete result for {path}: {len(records)} of {pages[0]['total_count']}"
        )
    return records


def stamp(t: datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(t: str) -> datetime:
    return datetime.fromisoformat(
        t.removesuffix("Z") + "+00:00" if t.endswith("Z") else t
    )


def list_runs(
    start: datetime, end: datetime, workflow: str = "", **params: str
) -> list[dict[str, Any]]:
    path = f"actions/workflows/{workflow}/runs" if workflow else "actions/runs"
    ranges: list[tuple[datetime, datetime]] = []
    while start < end:
        until = min(start + timedelta(days=7), end)
        ranges.append((start, until))
        start = until

    def segment(window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
        left, right = window
        try:
            return api(
                path,
                "workflow_runs",
                created=f"{stamp(left)}..{stamp(right)}",
                **params,
            )
        except RuntimeError as exc:
            if not str(exc).startswith("Incomplete result") or right - left < timedelta(
                minutes=2
            ):
                raise
            midpoint = left + (right - left) / 2
            return segment((left, midpoint)) + segment((midpoint, right))

    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = pool.map(segment, ranges)
        unique = {r["id"]: r for batch in batches for r in batch}
    return list(unique.values())


def workflows_runs(
    workflows: list[str], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    def fetch(workflow: str) -> list[dict[str, Any]]:
        events = P.get("tests", "workflow_events", {}).get(workflow)
        if events:
            return [
                r
                for event in events
                for r in list_runs(start, end, workflow, event=event)
            ]
        return list_runs(start, end, workflow)

    with ThreadPoolExecutor(max_workers=4) as pool:
        return [run for group in pool.map(fetch, workflows) for run in group]


JOB_FIELDS = (
    "id",
    "name",
    "conclusion",
    "started_at",
    "completed_at",
    "runner_name",
    "runner_group_name",
    "check_run_url",
)


class JobsAPI:
    """Reuse HTTPS connections for thousands of immutable GETs; throttle the initial backfill."""

    def __init__(self) -> None:
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "pr-coordinator-tests-inventory",
            "Authorization": "Bearer "
            + subprocess.check_output(
                ["gh", "auth", "token", "--hostname", "github.com"], text=True, cwd=ROOT
            ).strip(),
        }
        self.worker = threading.local()
        self.lock = threading.Lock()
        self.next_request = 0.0

    def page(self, path: str) -> tuple[dict[str, Any], bool]:
        for attempt in range(3):
            with self.lock:
                now = time.monotonic()
                wait = max(0.0, self.next_request - now)
                self.next_request = now + wait + 0.105
            time.sleep(wait)
            if not hasattr(self.worker, "connection"):
                self.worker.connection = http.client.HTTPSConnection(
                    "api.github.com", timeout=20
                )
            connection: http.client.HTTPSConnection = self.worker.connection
            try:
                connection.request("GET", path, headers=self.headers)
                response = connection.getresponse()
                body = response.read()
            except (OSError, http.client.HTTPException):
                connection.close()
                if attempt == 2:
                    raise RuntimeError("Jobs API connection failed") from None
                continue
            if response.status == 200:
                decoded: dict[str, Any] = json.loads(body)
                return decoded, 'rel="next"' in (response.getheader("Link") or "")
            retry_after = response.getheader("Retry-After")
            if response.status in {429, 502, 503, 504} or retry_after:
                delay = (
                    min(int(retry_after), 90)
                    if retry_after and retry_after.isdigit()
                    else 3
                )
                with self.lock:
                    self.next_request = max(self.next_request, time.monotonic() + delay)
                if attempt < 2:
                    continue
            raise RuntimeError(f"Jobs API HTTP {response.status}")
        raise RuntimeError("Jobs API retry limit")

    def jobs(self, rid: int, attempt: int) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        more, page = True, 1
        while more:
            data, more = self.page(
                f"/{BASE}/actions/runs/{rid}/attempts/{attempt}/jobs?per_page=100&page={page}"
            )
            jobs.extend({k: j.get(k) for k in JOB_FIELDS} for j in data["jobs"])
            page += 1
        return jobs


def get_run(run: dict[str, Any], source: JobsAPI) -> tuple[dict[str, Any], list[str]]:
    rid = run["id"]
    failures = []
    attempts = {}
    for attempt in range(1, (run.get("run_attempt") or 1) + 1):
        if attempt == run.get("run_attempt") and run["status"] != "completed":
            continue
        if attempt == run.get("run_attempt") and run["conclusion"] == "skipped":
            continue
        path = RUN_CACHE / f"{rid}-{attempt}.json"
        jobs = load(path, None)
        if jobs is None:
            try:
                jobs = source.jobs(rid, attempt)
                save(path, jobs)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{rid}/{attempt}: {exc}")
                continue
        attempts[str(attempt)] = jobs
    return attempts, failures


def fetch_activity(now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    RUN_CACHE.mkdir(exist_ok=True)
    workflows = [
        file.name
        for file in ROOT.glob(
            P.get("tests", "workflow_glob", ".github/workflows/*.y*ml")
        )
    ]
    runs = workflows_runs(workflows, now - timedelta(days=DAYS), now)
    fields = (
        "id",
        "name",
        "path",
        "event",
        "status",
        "conclusion",
        "run_attempt",
        "head_branch",
        "head_sha",
        "created_at",
        "pull_requests",
    )
    runs = [{field: run.get(field) for field in fields} for run in runs]
    save(RUN_LIST, {"collected_at": stamp(now), "runs": runs})
    return collect_attempts(runs)


def collect_attempts(
    runs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    RUN_CACHE.mkdir(exist_ok=True)
    skipped = sum(run["conclusion"] == "skipped" for run in runs)
    print(
        f"Listed {len(runs)} workflow records ({skipped} entirely skipped)", flush=True
    )
    failures = []
    source = JobsAPI()
    with ThreadPoolExecutor(max_workers=14) as pool:
        ordered = sorted(
            runs,
            key=lambda r: (
                Path(r["path"].split("@", 1)[0]).name
                not in P.get("tests", "priority_workflows", []),
                r["created_at"],
            ),
            reverse=False,
        )
        pending = {pool.submit(get_run, r, source): r for r in ordered}
        for i, future in enumerate(as_completed(pending), 1):
            attempts, errors = future.result()
            pending[future]["attempts"] = attempts
            failures.extend(errors)
            if i % 250 == 0:
                print(
                    f"Jobs checked for {i}/{len(runs)} workflows ({len(failures)} errors)",
                    flush=True,
                )
    return runs, failures


def read_activity() -> dict[str, Any]:
    complete: dict[str, Any] = load(ACTIVITY, {})
    if complete:
        return complete
    live = load(RUN_LIST, {})
    if not live:
        return {}
    for run in live["runs"]:
        attempts = {}
        for attempt in range(1, (run.get("run_attempt") or 1) + 1):
            if attempt == run.get("run_attempt") and run["conclusion"] == "skipped":
                continue
            jobs = load(RUN_CACHE / f"{run['id']}-{attempt}.json", None)
            if jobs is not None:
                attempts[str(attempt)] = jobs
        run["attempts"] = attempts
    return {**live, "partial": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--jobs-only",
        action="store_true",
        help="Collect GitHub job attempts without building the dashboard snapshot.",
    )
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="Contact GitHub and SSH certification hosts to refresh all sources.",
    )
    mode.add_argument(
        "--annotations",
        action="store_true",
        help="Build from cached inputs and fetch missing failure annotations from GitHub.",
    )
    mode.add_argument(
        "--resume-jobs",
        action="store_true",
        help="Finish job collection using the existing run list.",
    )
    args = parser.parse_args()
    if args.jobs_only or args.refresh or args.resume_jobs:
        if args.resume_jobs:
            saved = load(RUN_LIST, {})
            if not saved:
                parser.error("No saved run list. Use --jobs-only first.")
            collected_at = saved["collected_at"]
            runs, errors = collect_attempts(saved["runs"])
        else:
            now = datetime.now(UTC)
            collected_at = stamp(now)
            runs, errors = fetch_activity(now)
        save(ACTIVITY, {"collected_at": collected_at, "runs": runs, "errors": errors})
        print(
            f"Collected {len(runs)} runs; {len(errors)} unavailable job attempts.",
            flush=True,
        )
    if args.refresh:
        from tests_reports import refresh

        refresh()
    if not args.jobs_only and not args.resume_jobs:
        from tests_metrics import build

        snapshot = build(fetch_annotations=args.annotations or args.refresh)
        print(
            f"Wrote {CACHE.name}: {len(snapshot['jobs'])} job families, {len(snapshot['evidence']['tests'])} test IDs"
        )


if __name__ == "__main__":
    main()
