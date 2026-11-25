"""Refresh the coordinator's PR lifecycle snapshot (GitHub + local evidence)."""

import fcntl
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode

from lifecycle_model import build, stamp, summarize
from runtime import P, gh_argv

HERE = P.data
REPO = P.repo
DAY = 86400
KINDS = (
    "PULL_REQUEST_COMMIT HEAD_REF_FORCE_PUSHED_EVENT READY_FOR_REVIEW_EVENT CONVERT_TO_DRAFT_EVENT "
    "REVIEW_REQUESTED_EVENT PULL_REQUEST_REVIEW ADDED_TO_MERGE_QUEUE_EVENT REMOVED_FROM_MERGE_QUEUE_EVENT MERGED_EVENT"
)
FIELDS = """
createdAt mergedAt closedAt state isDraft headRefName headRefOid title author {login}
timelineItems(first:100 %s itemTypes:[%s]) {
  pageInfo {hasNextPage endCursor}
  nodes {
    __typename
    ... on PullRequestCommit {commit {oid committedDate pushedDate}}
    ... on HeadRefForcePushedEvent {createdAt beforeCommit {oid} afterCommit {oid}}
    ... on ReadyForReviewEvent {createdAt}
    ... on ConvertToDraftEvent {createdAt}
    ... on ReviewRequestedEvent {createdAt requestedReviewer {... on User {login} ... on Bot {login}}}
    ... on PullRequestReview {url createdAt submittedAt state author {login} commit {oid}}
    ... on AddedToMergeQueueEvent {createdAt}
    ... on RemovedFromMergeQueueEvent {createdAt reason}
    ... on MergedEvent {createdAt}
  }
}
"""


def read(name, default):
    try:
        return json.loads((HERE / name).read_text())
    except (OSError, ValueError):
        return default


def write(name, value):
    target = HERE / name
    temp = target.with_suffix(".new")
    temp.write_text(json.dumps(value, separators=(",", ":")) + "\n")
    temp.replace(target)


def gh(*args):
    return json.loads(
        subprocess.run(
            gh_argv(*args), timeout=90, capture_output=True, text=True, check=True
        ).stdout
    )


def rest(path, result_key=None, **query):
    suffix = urlencode({"per_page": 100, **query})
    pages = gh("api", "--paginate", "--slurp", f"repos/{REPO}/{path}?{suffix}")
    return [
        entry for page in pages for entry in (page[result_key] if result_key else page)
    ]


def graphql(cursors):
    fields = []
    for number, cursor in cursors.items():
        after = f"after:{json.dumps(cursor)}" if cursor else ""
        fields.append(
            f"p{number}:pullRequest(number:{number}){{{FIELDS % (after, ','.join(KINDS.split()))}}}"
        )
    query = '{repository(owner:"__OWNER__",name:"__REPO__"){' + " ".join(fields) + "}}"
    result = gh(
        "api",
        "graphql",
        "-f",
        "query="
        + query.replace("__OWNER__", P.repo_owner).replace("__REPO__", P.repo_name),
    )
    if result.get("errors"):
        raise ValueError(str(result["errors"]))
    return result["data"]["repository"]


class Cache:
    def __init__(self, now):
        self.now = now
        self.data = read("lifecycle-cache.json", {})
        self.errors = []
        self.used = set()

    def fresh(self, key, ttl):
        return key in self.data and self.data[key]["at"] + ttl > self.now

    def get(self, key, ttl, fetch, default=None, *, after=0):
        self.used.add(key)
        if not self.fresh(key, ttl) or self.data[key]["at"] < after:
            try:
                self.data[key] = {"at": self.now, "value": fetch()}
            except (subprocess.SubprocessError, ValueError, KeyError, OSError) as exc:
                text = getattr(exc, "stderr", None) or str(exc)
                self.errors.append(f"{key}: {text.strip()[:180]}")
        return self.data.get(key, {}).get("value", default)

    def timelines(self, numbers):
        def ttl(number):
            saved = self.data.get(f"pr:{number}", {})
            closed = stamp(saved.get("value", {}).get("closedAt"))
            if (
                closed
                and closed > self.now - 2 * DAY
                and any(
                    e["__typename"] == "PullRequestReview" and "state" not in e
                    for e in saved.get("value", {}).get("events", [])
                )
            ):
                return 0
            return 7 * DAY if closed and saved["at"] > closed + 600 else 240

        pending = [n for n in numbers if not self.fresh(f"pr:{n}", ttl(n))]
        first = {}
        for offset in range(0, len(pending), 6):
            batch = pending[offset : offset + 6]
            try:
                first.update(graphql(dict.fromkeys(batch)))
            except (subprocess.SubprocessError, ValueError, KeyError, OSError) as exc:
                self.errors.append(
                    f"PR timelines: {(getattr(exc, 'stderr', None) or str(exc)).strip()[:180]}"
                )

        def fetch(number):
            pr = first[f"p{number}"]
            page = pr.pop("timelineItems")
            pr["events"] = page["nodes"]
            while page["pageInfo"]["hasNextPage"]:
                page = graphql({number: page["pageInfo"]["endCursor"]})[f"p{number}"][
                    "timelineItems"
                ]
                pr["events"].extend(page["nodes"])
            return pr

        return {n: self.get(f"pr:{n}", ttl(n), lambda n=n: fetch(n)) for n in numbers}

    def save(self):
        write(
            "lifecycle-cache.json",
            {
                key: dict(record, seen=self.now) if key in self.used else record
                for key, record in self.data.items()
                if key in self.used
                or record.get("seen", record["at"]) > self.now - 7 * DAY
            },
        )


def local_history(state, now):
    saved = read("lifecycle-coord-history.json", {})
    log = {
        (date, message): None
        for date, message in saved.get("log", []) + state.get("log", [])
        if stamp(date) >= now - 14 * DAY
    }
    certs = {
        tuple(c.get(key) for key in ("pr", "target", "sha", "status", "at")): c
        for c in saved.get("certs", []) + state.get("certs", [])
        if (stamp(c.get("at")) or 0) >= now - 14 * DAY
    }
    history = {
        "log": sorted(log, key=lambda entry: entry[0]),
        "certs": list(certs.values()),
    }
    write("lifecycle-coord-history.json", history)
    return history


def project(items, keys):
    return [{key: item.get(key) for key in keys.split()} for item in items]


RUN = "id name head_branch head_sha created_at run_started_at updated_at status conclusion html_url run_attempt"
JOB = "id name created_at started_at completed_at labels conclusion html_url run_attempt status"
CHECK = "id name started_at completed_at details_url conclusion status"
STATUS = "context state created_at target_url description"


def collect():
    now = int(time.time())
    cache = Cache(now)
    state = read("state.json", {})
    train = {row["pr"]: row for row in read("train.json", [])}
    history = local_history(state, now)
    # A concurrent triage writer can expose a partial final line; use state.json as fallback.
    triage = {}
    triage_file = HERE / "triage.jsonl"
    for line in triage_file.read_text().splitlines() if triage_file.exists() else []:
        try:
            row = json.loads(line)
            triage[row["n"]] = row
        except (ValueError, KeyError):
            continue
    holders = {}
    lane_file = HERE / "locks/lane.holders"
    if lane_file.exists():
        for line in lane_file.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and all(p.isdecimal() for p in parts):
                holders[int(parts[0])] = int(parts[1])
    tracked = {pr["number"]: pr for pr in state.get("prs", [])}
    prs = cache.timelines(sorted(tracked.keys() | triage.keys()))
    prs = {
        n: pr
        for n, pr in prs.items()
        if pr
        and (
            pr["state"] == "OPEN"
            or (pr["mergedAt"] and stamp(pr["mergedAt"]) >= now - 2 * DAY)
        )
    }
    since = datetime.fromtimestamp(now - 3 * DAY, timezone.utc).isoformat()
    groups = cache.get(
        "merge_groups",
        240,
        lambda: project(
            rest(
                "actions/runs",
                "workflow_runs",
                event="merge_group",
                created=f">={since}",
            ),
            RUN,
        ),
        [],
    )

    def collect_pr(item):
        n, pr = item
        branch = pr["headRefName"]
        opened = pr["createdAt"]
        merged = stamp(pr["mergedAt"])
        settled = merged and now > merged + 600
        stable = 7 * DAY if settled else 240
        finalized_after = merged + 600 if settled else 0
        runs = cache.get(
            f"branch:{n}:{branch}",
            stable,
            lambda: project(
                rest(
                    "actions/runs",
                    "workflow_runs",
                    event="pull_request",
                    branch=branch,
                    created=f">={opened}",
                ),
                RUN,
            ),
            [],
            after=finalized_after,
        )
        groups_for_pr = [
            r for r in groups if re.search(rf"(?:^|/)pr-{n}-", r["head_branch"])
        ]
        recent = [
            r
            for r in runs
            if stamp(r["created_at"]) >= now - 2 * DAY
            and (not merged or stamp(r["created_at"]) <= merged)
        ]
        # Check/status lookup is per observed head, not per constituent commit.
        heads = {r["head_sha"] for r in recent} | {pr["headRefOid"]}
        heads.update(
            c["oid"]
            for e in pr["events"]
            if e.get("createdAt") and stamp(e["createdAt"]) >= now - 2 * DAY
            for c in [e.get("afterCommit"), e.get("beforeCommit")]
            if c
        )
        cert_shas = {
            c["sha"] for c in history["certs"] if c["pr"] == n and c.get("sha")
        }
        heads.update(
            c["oid"]
            for e in pr["events"]
            if (c := e.get("commit"))
            and any(c["oid"].startswith(short) for short in cert_shas)
        )
        checks = {}
        statuses = {}
        for sha in sorted(heads - {None}):
            ttl = stable if sha == pr["headRefOid"] or settled else 30 * 60
            checks[sha] = cache.get(
                f"checks:{sha}",
                ttl,
                lambda sha=sha: project(
                    rest(f"commits/{sha}/check-runs", "check_runs", filter="all"), CHECK
                ),
                [],
                after=finalized_after,
            )
            statuses[sha] = cache.get(
                f"statuses:{sha}",
                ttl,
                lambda sha=sha: project(
                    [
                        s
                        for s in rest(f"commits/{sha}/statuses")
                        if s["context"].startswith(P.cert_prefix)
                    ],
                    STATUS,
                ),
                [],
                after=finalized_after,
            )
        jobs = {}
        for r in recent + groups_for_pr:
            if r["name"].lower().endswith("/ draft"):
                continue
            rid = r["id"]
            ttl = 7 * DAY if r["status"] == "completed" else 240
            jobs[rid] = cache.get(
                f"jobs:{rid}:{r['run_attempt']}:{r['status']}",
                ttl,
                lambda rid=rid: project(
                    rest(f"actions/runs/{rid}/jobs", "jobs", filter="all"), JOB
                ),
                [],
            )
        return build(
            n,
            pr,
            runs,
            jobs,
            checks,
            statuses,
            groups_for_pr,
            history,
            train.get(n),
            holders.get(n),
            now,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        series = list(pool.map(collect_pr, prs.items()))
    series.sort(
        key=lambda p: (p["merged"] is not None, -(p["merged"] or 0), -p["number"])
    )
    cache.save()
    if not series and tracked and cache.errors:
        raise RuntimeError("; ".join(cache.errors[:5]))
    write(
        "lifecycle.json",
        {
            "generated": now,
            "prs": series,
            "summary": summarize(series),
            "warnings": sorted(set(cache.errors)),
            "scope_hours": 48,
        },
    )
    print(
        f"{len(series)} PRs, {sum(p['merged'] is not None for p in series)} merged, {len(cache.errors)} API warnings"
    )


def main():
    with (HERE / "locks/lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            collect()
