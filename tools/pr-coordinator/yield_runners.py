"""Yield hosted runners to priority (human collaborator) PRs.

Each call: if a `priority` PR has a job waiting for a runner for more than GRACE minutes, write locks/yield
(agents and coordinator tools hold new CI while it exists) and cancel agent `pull_request` runs whose jobs
have not started yet, remembering them. When no priority job is waiting, remove locks/yield and rerun the
cancelled agent runs. Merge-group runs are never cancelled (that would drop PRs from the merge queue).
"""

import json
import subprocess
import time
from datetime import datetime, timezone

from landing import json_gh
from runtime import P, atomic_text, cli, gh_argv, load, save

HERE = P.data
YIELD = HERE / "locks" / "yield"
CANCELLED = HERE / "yield_cancelled.json"
REPO = P.repo
GRACE = P.get(
    "landing", "yield_grace_minutes", 3
)  # minutes a priority job may wait before agents yield


def api(path):
    return json_gh("api", path)


def log(msg):
    subprocess.run([*cli(), "log", msg])


def os_of(job):
    labels = " ".join(job.get("labels") or []).lower()
    for key, name in (
        ("macos", "macOS"),
        ("windows", "Windows"),
        ("arm", "Linux ARM"),
        ("ubuntu", "Linux"),
        ("linux", "Linux"),
    ):
        if key in labels:
            return name
    return "other"


def record_runner_usage(runs_jobs, priority_branches):
    """Store hosted runner usage (running/waiting jobs by OS and by kind) in state.json for the dashboard."""
    import fcntl

    usage = {"running": {}, "waiting": {}, "by_kind": {}, "oldest_wait_min": {}}
    now = datetime.now(timezone.utc)
    for run, jobs in runs_jobs:
        kind = (
            "priority PR"
            if run["head_branch"] in priority_branches
            else {
                "merge_group": "merge queue",
                "pull_request": "agent PR",
                "push": "main push",
            }.get(run["event"], run["event"])
        )
        for j in jobs:
            if j["status"] == "in_progress":
                o = os_of(j)
                usage["running"][o] = usage["running"].get(o, 0) + 1
                usage["by_kind"][kind] = usage["by_kind"].get(kind, 0) + 1
            elif j["status"] in ("queued", "waiting"):
                o = os_of(j)
                usage["waiting"][o] = usage["waiting"].get(o, 0) + 1
                age = (
                    now - datetime.fromisoformat(j["created_at"].replace("Z", "+00:00"))
                ).total_seconds() / 60
                usage["oldest_wait_min"][kind] = round(
                    max(age, usage["oldest_wait_min"].get(kind, 0))
                )
    usage["at"] = time.strftime("%H:%M:%S")
    with open(HERE / "state.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        s = load(HERE / "state.json", {"prs": [], "machines": [], "log": []})
        hist = s.get("runner_history", [])[-180:]
        hist.append(
            [
                usage["at"],
                sum(usage["running"].values()),
                sum(usage["waiting"].values()),
            ]
        )
        s["runners"], s["runner_history"] = usage, hist
        tmp = HERE / "state.tmp"
        tmp.write_text(json.dumps(s, indent=1))
        tmp.replace(HERE / "state.json")


def main():
    prs = json_gh(
        "pr",
        "list",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "headRefName,headRefOid,labels,author,isCrossRepository",
    )
    protected = {P.hands_off, P.priority, P.land_asap}
    priority_branches = {
        p["headRefName"]
        for p in prs
        if {l["name"] for l in p["labels"]} & {P.priority, P.land_asap}
        and not any(l["name"] == P.hands_off for l in p["labels"])
    }
    ours = {
        (p["headRefName"], p["headRefOid"])
        for p in prs
        if (p.get("author") or {}).get("login") == P.owner
        and not p.get("isCrossRepository")
        and not {l["name"] for l in p["labels"]} & protected
    }
    runs = []
    for status in ("queued", "in_progress"):
        runs += api(f"repos/{REPO}/actions/runs?status={status}&per_page=100").get(
            "workflow_runs", []
        )
    now = datetime.now(timezone.utc)
    starving, agent_unstarted = [], []
    runs_jobs = []
    for run in runs:
        jobs = api(f"repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=100").get(
            "jobs", []
        )
        runs_jobs.append((run, jobs))
        waiting = [j for j in jobs if j["status"] in ("queued", "waiting")]
        if run["head_branch"] in priority_branches:
            for j in waiting:
                age = (
                    now - datetime.fromisoformat(j["created_at"].replace("Z", "+00:00"))
                ).total_seconds() / 60
                if age >= GRACE:
                    starving.append(
                        f"{run['head_branch']}: {j['name']} waiting {age:.0f} min"
                    )
        elif (
            run["event"] == "pull_request"
            and (run.get("head_repository") or {}).get("full_name", "").casefold()
            == REPO.casefold()
            and (run["head_branch"], run["head_sha"]) in ours
            and jobs
            and all(j["status"] in ("queued", "waiting") for j in jobs)
        ):
            agent_unstarted.append(run)
    record_runner_usage(runs_jobs, priority_branches)
    cancelled = load(CANCELLED, [])
    if starving:
        if not YIELD.exists():
            log(f"YIELD to priority PRs: {starving[:3]}")
        atomic_text(YIELD, "\n".join(starving) + "\n")
        for run in agent_unstarted:
            if any(row["id"] == run["id"] for row in cancelled):
                continue
            r = subprocess.run(
                gh_argv("run", "cancel", str(run["id"])),
                capture_output=True,
                text=True,
                timeout=90,
            )
            if r.returncode == 0:
                cancelled.append(
                    {
                        "id": run["id"],
                        "branch": run["head_branch"],
                        "sha": run["head_sha"],
                        "at": time.strftime("%H:%M"),
                    }
                )
                save(CANCELLED, cancelled)
                log(
                    f"yield: cancelled unstarted agent run {run['id']} ({run['head_branch']})"
                )
        save(CANCELLED, cancelled)
    elif YIELD.exists() or cancelled:
        YIELD.unlink(missing_ok=True)
        remaining, retried = [], 0
        for c in cancelled:
            run = api(f"repos/{REPO}/actions/runs/{c['id']}")
            if (
                run["event"] != "pull_request"
                or (run["head_branch"], run["head_sha"]) not in ours
                or (run.get("head_repository") or {}).get("full_name", "").casefold()
                != REPO.casefold()
            ):
                continue
            if run["status"] != "completed":
                remaining.append(c)
                continue
            if run["conclusion"] == "cancelled":
                from train import only_cancelled_run

                if only_cancelled_run(c["id"]):
                    result = subprocess.run(
                        gh_argv("run", "rerun", str(c["id"]), "--failed"),
                        capture_output=True,
                        timeout=90,
                    )
                    if result.returncode:
                        remaining.append(c)
                    else:
                        retried += 1
        save(CANCELLED, remaining)
        if retried:
            log(f"yield over: reran {retried} agent runs")


if __name__ == "__main__":
    main()
