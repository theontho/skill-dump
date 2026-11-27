"""Fail-closed GitHub checks shared by landing and refresh operations."""

import json
import subprocess
from enum import Enum, auto

from runtime import P, gh


def json_gh(*args):
    result = gh(*args, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh {args[0]} failed")
    data = json.loads(result.stdout)
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data


def query(selection, *args):
    return json_gh("api", "graphql", "-f", f"query={P.graphql(selection)}", *args)[
        "data"
    ]["repository"]


def pull(number, selection):
    return query(f"pullRequest(number:{int(number)}){{{selection}}}")["pullRequest"]


def queued(number):
    return bool(pull(number, "mergeQueueEntry{position}")["mergeQueueEntry"])


def queue_numbers():
    branch = json.dumps(P.base)
    cursor = None
    while True:
        after = ",after:" + json.dumps(cursor) if cursor else ""
        queue = query(
            f"mergeQueue(branch:{branch}){{entries(first:100{after})"
            "{nodes{pullRequest{number}} pageInfo{hasNextPage endCursor}}}"
        )["mergeQueue"]
        if queue is None:
            return
        entries = queue["entries"]
        yield from (row["pullRequest"]["number"] for row in entries["nodes"])
        if not entries["pageInfo"]["hasNextPage"]:
            return
        cursor = entries["pageInfo"]["endCursor"]


def checks(number):
    result = gh(
        "pr",
        "checks",
        str(number),
        "--json",
        "bucket,name,workflow,link",
        capture_output=True,
        text=True,
        timeout=90,
    )
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return False, False, []
    except ValueError:
        return False, False, []
    pending = any(row.get("bucket") == "pending" for row in rows)
    command = P.command("landing", "pr_checks_ready", pr=number)
    if command:
        gate = subprocess.run(
            command,
            cwd=P.checkout,
            input=result.stdout,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return gate.returncode == 0, pending, rows
    required = set(P.get("landing", "required_checks", ["required"]))
    passed = {row.get("name") for row in rows if row.get("bucket") == "pass"}
    return (
        (
            result.returncode == 0
            and bool(required)
            and required <= passed
            and all(row.get("bucket") in {"pass", "skipping"} for row in rows)
        ),
        pending,
        rows,
    )


def review(number):
    return pull(
        number,
        "id state isDraft author{login} isCrossRepository baseRefName headRefOid "
        "mergeable mergeStateStatus reviewDecision mergeQueueEntry{position} autoMergeRequest{enabledAt} "
        "labels(first:100){nodes{name} pageInfo{hasNextPage}} "
        "reviewRequests(first:100){nodes{requestedReviewer{... on Bot{login} ... on User{login}}} "
        "pageInfo{hasNextPage}} "
        "reviewThreads(first:100){nodes{isResolved} pageInfo{hasNextPage}}",
    )


def labels(info):
    return {entry["name"] for entry in info["labels"]["nodes"]}


def eligible(info, *, owned=True):
    if (
        not info
        or info["state"] != "OPEN"
        or info["isDraft"]
        or info["baseRefName"] != P.base
    ):
        return False
    if info["labels"]["pageInfo"]["hasNextPage"] or P.hands_off in labels(info):
        return False
    if owned and (
        (info.get("author") or {}).get("login") != P.owner or info["isCrossRepository"]
    ):
        return False
    return True


def reviewed(info):
    threads = info["reviewThreads"]
    decision = info["reviewDecision"]
    return (
        not threads["pageInfo"]["hasNextPage"]
        and all(t["isResolved"] for t in threads["nodes"])
        and decision not in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}
        and (not P.get("landing", "require_approval", False) or decision == "APPROVED")
    )


def mergeable(info):
    return info["mergeable"] == "MERGEABLE" and info["mergeStateStatus"] in {
        "CLEAN",
        "BEHIND",
        "BLOCKED",
    }


def paused():
    return any((P.data / "locks" / name).exists() for name in ("yield", "hold"))


class EnqueueResult(Enum):
    QUEUED = auto()
    RECHECK = auto()
    STOPPED = auto()


def enqueue(number, head, *, priority=False):
    outcome, _, _ = enqueue_result(number, head, priority=priority)
    return outcome is EnqueueResult.QUEUED


def enqueue_result(number, head, *, priority=False):
    # Check before and after the CI gate: never apply an old verdict to a new head.
    before = review(number)
    if not eligible(before, owned=not priority) or before["headRefOid"] != head:
        return EnqueueResult.STOPPED, "", "PR head or eligibility changed"
    if not priority and paused():
        return EnqueueResult.STOPPED, "", "landing paused"
    ready, _, rows = checks(number)
    if not ready:
        summary = ", ".join(f"{row.get('name')}: {row.get('bucket')}" for row in rows)
        return (
            EnqueueResult.RECHECK,
            "readiness-gate",
            f"PR readiness gate rejected {head}: {summary or 'no checks'}",
        )
    info = review(number)
    if (
        not eligible(info, owned=not priority)
        or not reviewed(info)
        or info["headRefOid"] != head
    ):
        return EnqueueResult.STOPPED, "", "PR head, eligibility or review changed"
    if info["mergeQueueEntry"]:
        return EnqueueResult.QUEUED, "", ""
    if not mergeable(info):
        # GitHub can still be computing mergeability just after a head update.
        outcome = (
            EnqueueResult.RECHECK
            if info["mergeable"] == "UNKNOWN"
            else EnqueueResult.STOPPED
        )
        return (
            outcome,
            "mergeability",
            f"PR mergeability: {info['mergeable']} ({info['mergeStateStatus']})",
        )
    if priority:
        if P.land_asap not in labels(info):
            return EnqueueResult.STOPPED, "", "priority label removed"
        result = gh(
            "api",
            "graphql",
            "-f",
            "query=mutation($id:ID!,$sha:GitObjectID!){enqueuePullRequest("
            "input:{pullRequestId:$id,jump:true,expectedHeadOid:$sha}){mergeQueueEntry{position}}}",
            "-f",
            f"id={info['id']}",
            "-f",
            f"sha={head}",
            capture_output=True,
            text=True,
            timeout=90,
        )
        if not result.returncode and (
            json.loads(result.stdout)
            .get("data", {})
            .get("enqueuePullRequest", {})
            .get("mergeQueueEntry")
        ):
            return EnqueueResult.QUEUED, "", ""
        return (
            EnqueueResult.RECHECK,
            "priority-enqueue",
            result.stderr.strip() or "priority enqueue rejected",
        )
    if paused():
        return EnqueueResult.STOPPED, "", "landing paused"
    flags = P.command("landing", "merge_flags", default=["--squash", "--auto"])
    if "--auto" not in flags or "--admin" in flags or "--disable-auto" in flags:
        raise ValueError(
            "landing.merge_flags must enable auto-merge without bypassing protections"
        )
    result = gh(
        "pr",
        "merge",
        str(number),
        *flags,
        "--match-head-commit",
        head,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode:
        return (
            EnqueueResult.RECHECK,
            f"merge-exit-{result.returncode}",
            result.stderr.strip() or f"gh merge exited {result.returncode}",
        )
    return EnqueueResult.QUEUED, "", ""


def assert_owner():
    login = json_gh("api", "user")["login"]
    if login.casefold() != P.owner.casefold():
        raise RuntimeError(
            f"authenticated GitHub login {login!r} is not configured owner {P.owner!r}"
        )
