"""Bounded, metadata-only GitHub collection and PR efficiency calculations."""

import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from urllib.parse import urlencode


class GitHub:
    def __init__(self, project):
        self.project = project
        self.calls = 0
        self.missing_attempts = 0

    def api(self, path, *args):
        self.calls += 1
        if self.calls > 1500:
            raise ValueError("efficiency report exceeded 1,500 GitHub calls")
        result = subprocess.run(
            ["gh", "api", path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode:
            if "/attempts/" in path and "HTTP 404" in result.stderr:
                self.missing_attempts += 1
                return {}
            raise ValueError(
                f"GitHub report request failed: {result.stderr.strip()[:300]}"
            )
        return json.loads(result.stdout)

    def search(self, qualifier, lo, hi):
        return self.window(
            "search/issues",
            "items",
            "q",
            lo,
            hi,
            f"repo:{self.project.repo} is:pr {qualifier}:",
        )

    def window(self, path, key, parameter, lo, hi, prefix=""):
        """Inclusive second-resolution API ranges, split before the 1,000-result cap."""
        query = {parameter: f"{prefix}{iso(lo)}..{iso(hi - 1)}", "per_page": 100}
        page = self.api(f"{path}?{urlencode(query)}")
        count = page["total_count"]
        if count >= 1000 or page.get("incomplete_results"):
            middle = (lo + hi) // 2
            if middle == lo:
                raise ValueError(f"incomplete GitHub results for {path} at {iso(lo)}")
            return self.window(path, key, parameter, lo, middle, prefix) + self.window(
                path, key, parameter, middle, hi, prefix
            )
        rows = page[key]
        for number in range(2, (count + 99) // 100 + 1):
            rows.extend(self.api(f"{path}?{urlencode(query | {'page': number})}")[key])
        id_key = "number" if key == "items" else "id"
        if len({row[id_key] for row in rows}) != count:
            raise ValueError(f"GitHub page counts changed for {path}; retry the report")
        return rows

    def timelines(self, numbers):
        numbers = sorted(numbers)
        output = {}
        for offset in range(0, len(numbers), 8):
            cursors = dict.fromkeys(numbers[offset : offset + 8])
            while cursors:
                fields = []
                for number, cursor in cursors.items():
                    after = f",after:{json.dumps(cursor)}" if cursor else ""
                    fields.append(
                        TIMELINE.replace("NUMBER", str(number)).replace("AFTER", after)
                    )
                result = self.api(
                    "graphql", "-f", "query=" + self.project.graphql(" ".join(fields))
                )
                if result.get("errors"):
                    raise ValueError(f"GitHub PR timeline failed: {result['errors']}")
                pending = {}
                for number, cursor in cursors.items():
                    pr = result["data"]["repository"][f"p{number}"]
                    page = pr.pop("timelineItems")
                    target = output.setdefault(number, pr | {"events": []})
                    target["events"].extend(node for node in page["nodes"] if node)
                    if page["pageInfo"]["hasNextPage"]:
                        next_cursor = page["pageInfo"]["endCursor"]
                        if not next_cursor or next_cursor == cursor:
                            raise ValueError(
                                f"PR {number} returned an invalid timeline cursor"
                            )
                        pending[number] = next_cursor
                cursors = pending
        return output

    def collect(self, lo, hi, cert_prs):
        path = f"repos/{self.project.repo}/actions/runs"
        runs = []
        numbers = set(cert_prs)
        for start in range(lo, hi, 86400):
            end = min(hi, start + 86400)
            runs.extend(self.window(path, "workflow_runs", "created", start, end))
        # Recently updated PRs cover active branches whose runs omit pull_requests.
        numbers.update(row["number"] for row in self.search("closed", lo, hi))
        numbers.update(row["number"] for row in self.search("updated", lo, hi))
        numbers.update(
            pr["number"]
            for run in runs
            for pr in run["pull_requests"]
            if ((pr.get("base") or {}).get("repo") or {}).get("id")
            == run["repository"]["id"]
        )
        prs = self.timelines(numbers)

        def attempts(run):
            for number in range(run["run_attempt"] - 1, 0, -1):
                yield self.api(f"{path}/{run['id']}/attempts/{number}")

        return evaluate(prs, runs, attempts, lo, hi, cert_prs)


TIMELINE = """
pNUMBER:pullRequest(number:NUMBER) {
  createdAt closedAt mergedAt isDraft headRefName headRefOid headRepository {databaseId}
  timelineItems(first:100 AFTER itemTypes:[
    READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, HEAD_REF_FORCE_PUSHED_EVENT,
    PULL_REQUEST_REVIEW
  ]) {
    pageInfo {hasNextPage endCursor}
    nodes {
      __typename
      ... on ReadyForReviewEvent {createdAt}
      ... on ConvertToDraftEvent {createdAt}
      ... on HeadRefForcePushedEvent {createdAt afterCommit {oid}}
      ... on PullRequestReview {submittedAt isCopilotCodeReview author {login}}
    }
  }
}
"""


def stamp(value):
    return datetime.fromisoformat(value).timestamp() if value else 0


def iso(value):
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluate(prs, runs, attempts, lo, hi, cert_prs):
    metrics = Counter(
        merged_prs=0,
        review_prs_at_least_4=0,
        pushes_after_ready=0,
        superseded_hosted_runs=0,
        superseded_certifications=0,
        rerun_only_passes=0,
    )
    branches = defaultdict(list)
    heads = defaultdict(list)
    pushes = defaultdict(list)
    readiness = {}
    reviews = []
    for n, pr in prs.items():
        branches[
            ((pr.get("headRepository") or {}).get("databaseId"), pr["headRefName"])
        ].append(n)
        heads[pr["headRefOid"]].append(n)
        events = pr["events"]
        ready, draft = [
            sorted(stamp(e["createdAt"]) for e in events if e["__typename"] == kind)
            for kind in ("ReadyForReviewEvent", "ConvertToDraftEvent")
        ]
        initially_draft = (
            ready[0] <= draft[0]
            if ready and draft
            else bool(ready or (pr["isDraft"] and not draft))
        )
        first_ready = (
            (ready[0] if ready else 0) if initially_draft else stamp(pr["createdAt"])
        )
        readiness[n] = (
            first_ready,
            sorted([(at, True) for at in ready] + [(at, False) for at in draft]),
        )
        for e in events:
            if e["__typename"] == "HeadRefForcePushedEvent" and e["afterCommit"]:
                pushes[n].append((0, stamp(e["createdAt"]), e["afterCommit"]["oid"]))
        merge = stamp(pr["mergedAt"])
        if lo <= merge < hi:
            count = sum(
                0 < stamp(e.get("submittedAt")) <= merge
                and (
                    e["isCopilotCodeReview"]
                    or "copilot" in ((e["author"] or {}).get("login") or "").casefold()
                )
                for e in events
                if e["__typename"] == "PullRequestReview"
            )
            reviews.append(count)
            metrics["merged_prs"] += 1
            metrics["review_prs_at_least_4"] += count >= 4

    matched = []
    for run in runs:
        event, sha = run["event"], run["head_sha"]
        if event not in {"pull_request", "pull_request_target", "dynamic", "push"}:
            continue
        at = stamp(run["created_at"])
        branch = branches[
            ((run.get("head_repository") or {}).get("id"), run["head_branch"])
        ]
        direct = [
            pr["number"]
            for pr in run["pull_requests"]
            if pr["number"] in prs
            and (
                ((pr.get("base") or {}).get("repo") or {}).get("id")
                in (None, (run.get("repository") or {}).get("id"))
            )
        ]
        groups = (branch,) if event == "push" else (direct, branch, heads[sha])
        n = None
        for group in groups:
            eligible = {
                n
                for n in group
                if stamp(prs[n]["createdAt"]) <= at
                and (not prs[n]["closedAt"] or at <= stamp(prs[n]["closedAt"]))
            }
            if len(eligible) == 1:
                n = eligible.pop()
                break
        if n is None:
            continue
        if (event == "push" and run["head_branch"] not in {"main", "master"}) or (
            event == "pull_request"
            and re.search(r"/\s*synchronize\s*/", run.get("name") or "")
        ):
            pushes[n].append((2 if event == "push" else 1, at, sha))
        matched.append((n, run))

    for n, rows in pushes.items():
        kept = []
        for _, at, sha in sorted(rows):
            if not any(
                old_sha == sha and abs(old_at - at) < 300 for old_at, old_sha in kept
            ):
                kept.append((at, sha))
                first_ready = readiness[n][0]
                metrics["pushes_after_ready"] += bool(
                    first_ready and first_ready < at and lo <= at < hi
                )
        pushes[n] = kept

    def superseded(n, head, start, end):
        def same(sha):
            return sha and sha.startswith(head)

        # Use an observed replacement before the work ended, or a known final
        # merged head. A current head on an open PR cannot date old work.
        return any(
            start < at <= end and not same(sha) for at, sha in pushes[n]
        ) or bool(prs[n]["mergedAt"] and not same(prs[n]["headRefOid"]))

    for n, run in matched:
        sha = run["head_sha"]
        started = stamp(run["created_at"])
        updated = stamp(run["updated_at"])
        first_ready, transitions = readiness[n]
        ready_at_start = next(
            (ready for at, ready in reversed(transitions) if at <= started),
            bool(first_ready and first_ready <= started),
        )
        suffix = (run.get("name") or "").lower()
        if (
            run["event"] == "pull_request"
            and first_ready
            and first_ready <= started
            and not suffix.endswith("/ draft")
            and (ready_at_start or suffix.endswith("/ ready"))
            and run["conclusion"] not in {None, "skipped", "neutral", "action_required"}
        ):
            metrics["superseded_hosted_runs"] += superseded(n, sha, started, updated)
        merge = stamp(prs[n]["mergedAt"])
        if (
            merge
            and lo <= started < hi
            and run["conclusion"] == "success"
            and run["run_attempt"] > 1
            and run["head_sha"] == prs[n]["headRefOid"]
            and updated <= merge
        ):
            metrics["rerun_only_passes"] += any(
                past.get("conclusion") in {"failure", "timed_out", "startup_failure"}
                and past["head_sha"] == sha
                and stamp(past["updated_at"]) <= merge
                for past in attempts(run)
            )
    for n, certs in cert_prs.items():
        if n in prs:
            metrics["superseded_certifications"] += sum(
                superseded(n, sha, start, end) for sha, start, end in certs
            )
    return dict(metrics) | {
        "review_rounds_p50": statistics.median(reviews) if reviews else None,
        "review_rounds_p90": (
            statistics.quantiles(reviews, n=10, method="inclusive")[-1]
            if len(reviews) > 1
            else next(iter(reviews), None)
        ),
    }
