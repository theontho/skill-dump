"""Fail-closed GitHub checks shared by landing and refresh operations."""

import json
import subprocess
from enum import Enum, auto

import stack_landing as stacks
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
        "id state isDraft author{login} isCrossRepository baseRefName headRefName headRefOid "
        "mergeable mergeStateStatus reviewDecision mergeQueueEntry{position} autoMergeRequest{enabledAt} "
        "labels(first:100){nodes{name} pageInfo{hasNextPage}} "
        "reviewRequests(first:100){nodes{requestedReviewer{... on Bot{login} ... on User{login}}} "
        "pageInfo{hasNextPage}} "
        "reviewThreads(first:100){nodes{isResolved} pageInfo{hasNextPage}}",
    )


def labels(info):
    return {entry["name"] for entry in info["labels"]["nodes"]}


def eligible(info, *, owned=True, stack=None):
    if (
        not info
        or info["state"] != "OPEN"
        or info["isDraft"]
        or (stack.base if stack else info["baseRefName"]) != P.base
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


def stack_action(stack):
    action = P.get("landing", "stack_merge_action", "merge_queue")
    if action not in ("merge_queue", "direct_merge"):
        raise stacks.StackError(
            "landing.stack_merge_action must be merge_queue or direct_merge"
        )
    if stack.base != P.base:
        raise stacks.StackError(
            f"stack #{stack.number} targets unexpected base {stack.base!r}"
        )
    try:
        queue = query(f"mergeQueue(branch:{json.dumps(stack.base)}){{id}}")[
            "mergeQueue"
        ]
    except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise stacks.StackError(
            f"cannot determine merge queue for {stack.base}: {exc}"
        ) from exc
    if (queue is not None) != (action == "merge_queue"):
        raise stacks.StackError(
            f"landing.stack_merge_action does not match the queue on {stack.base}"
        )
    if action == "merge_queue":
        return action, None
    allowed = stacks.read("")
    method = P.get("landing", "stack_merge_method")
    if method is None:
        try:
            preference = query("viewerDefaultMergeMethod")[
                "viewerDefaultMergeMethod"
            ].lower()
        except (
            RuntimeError,
            ValueError,
            KeyError,
            AttributeError,
            subprocess.SubprocessError,
        ):
            preference = None
        methods = (preference, "merge", "squash", "rebase")
    else:
        methods = (method,)
    for candidate in methods:
        if (
            candidate in ("merge", "squash", "rebase")
            and allowed.get(f"allow_{candidate}_merge") is True
        ):
            return action, candidate
    raise stacks.StackError("no allowed direct stack merge method")


def stack_enqueue(pr, head, stack, info, active):
    action, method = stack_action(stack)

    def preflight():
        fresh = stacks.lookup(pr)
        if fresh is None or (fresh.number, fresh.base, fresh.prefix) != (
            stack.number,
            stack.base,
            stack.prefix,
        ):
            raise stacks.StackError("stack prefix changed before submission")
        for lower in fresh.prefix[:-1]:
            if review(lower["number"])["state"] != "MERGED":
                raise stacks.StackError(
                    f"lower stack PR #{lower['number']} is not merged"
                )
        boundary = fresh.prefix[-1]
        boundary_head = boundary.get("head") or {}
        current = review(pr)
        if (
            not eligible(current, stack=fresh)
            or not reviewed(current)
            or not mergeable(current)
            or current["mergeQueueEntry"]
            or current["autoMergeRequest"]
            or current["headRefOid"] != head
            or current["headRefName"] != info["headRefName"]
            or current["baseRefName"] != info["baseRefName"]
            or boundary.get("state") != "open"
            or boundary.get("draft") is not False
            or boundary_head.get("ref") != current["headRefName"]
            or boundary_head.get("sha") != head
            or paused()
            or not active()
        ):
            raise stacks.StackError(
                "stack boundary changed or is no longer safe to submit"
            )

    state, record = stacks.merge(pr, head, stack, action, method, preflight=preflight)
    if state == "enqueued" and action != "merge_queue":
        raise stacks.StackError(
            f"unexpected queue result for direct stack merge; record: {record}"
        )
    fresh = stacks.detail(stack.number, pr)
    if [row["number"] for row in fresh.prefix] != [
        row["number"] for row in stack.prefix
    ]:
        raise stacks.StackError(
            f"stack prefix changed after merge-async; record: {record}"
        )
    for member in fresh.prefix:
        current = review(member["number"])
        if current["state"] != "MERGED" and not (
            state == "enqueued"
            and member["number"] == pr
            and current["state"] == "OPEN"
            and current["headRefOid"] == head
            and current["mergeQueueEntry"]
        ):
            raise stacks.StackError(
                f"could not verify #{member['number']} {state}; record: {record}"
            )
    remaining = ", ".join(f"#{n}" for n in fresh.upper)
    return (
        EnqueueResult.QUEUED,
        "",
        f"stack #{stack.number} {state}; upper PRs not submitted: {remaining or 'none'}; record: {record}",
    )


def enqueue_result(number, head, *, priority=False, active=lambda: True):
    stack_number = stacks.membership(number)
    if stack_number is not None and priority:
        return EnqueueResult.STOPPED, "", "use the landing train for native stacks"
    stack = stacks.detail(stack_number, number) if stack_number is not None else None
    if stack and (problem := stacks.prefix_problem(stack)):
        return EnqueueResult.STOPPED, "stack-prefix", problem
    # Check before and after the CI gate: never apply an old verdict to a new head.
    before = review(number)
    if (
        not eligible(before, owned=not priority, stack=stack)
        or before["headRefOid"] != head
    ):
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
        not eligible(info, owned=not priority, stack=stack)
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
    if stack:
        if info["autoMergeRequest"]:
            return (
                EnqueueResult.STOPPED,
                "stack-auto-merge",
                "disable auto-merge before landing a stack",
            )
        return stack_enqueue(number, head, stack, info, active)
    if stacks.membership(number) is not None or not active():
        return EnqueueResult.STOPPED, "", "PR membership or train assignment changed"
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
