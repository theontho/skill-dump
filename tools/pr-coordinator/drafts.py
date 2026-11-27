"""Verify and adopt an owner draft without bypassing the landing train."""

import argparse
import os
import subprocess
import time
from urllib.parse import quote, urlencode

from cert_policy import plan_outcome, review_state, routed, statuses
from landing import assert_owner, json_gh, labels, paused, review
from reconcile import cert_pid
from runtime import P, cli, gh, load, locked, save
from worktrees import prepare


def owner_pr(pr, *, draft):
    row = review(pr)
    if (
        row["state"] != "OPEN"
        or row["isDraft"] != draft
        or (row["author"] or {}).get("login") != P.owner
        or row["isCrossRepository"]
        or row["baseRefName"] != P.base
        or row["labels"]["pageInfo"]["hasNextPage"]
        or P.hands_off in labels(row)
    ):
        raise ValueError("not an eligible owner PR in the expected draft state")
    return row


def handoff(pr, head=None):
    row = owner_pr(pr, draft=False)
    head = head or row["headRefOid"]
    if row["headRefOid"] != head:
        raise ValueError("head changed before handoff")
    from train import registered

    if registered(pr, head):
        print(f"#{pr} already registered with the landing train at {head[:9]}.")
        return 0
    wt, _ = handoff_worktree(pr, head)
    return submit(pr, wt, head)


def submit(pr, wt, head, *, readd=False):
    from train import register

    register(pr, wt, head, readd=readd)
    return 0


def handoff_worktree(pr, head):
    with locked(f"worktree-pr-{pr}.lock"):
        row = json_gh("pr", "view", str(pr), "--json", "headRefName,headRefOid")
        if head and row["headRefOid"] != head:
            raise ValueError("head changed before handoff")
        return prepare(pr, row, "train"), row["headRefName"]


def running_certs(pr, head):
    try:
        with locked(f"cert-pr-{pr}-{head}.lock", wait=False):
            pass
    except BlockingIOError:
        return True
    for row in load(P.data / "state.json", {}).get("certs", []):
        if (
            row.get("pr") == pr
            and row.get("sha") in (head, head[:9])
            and row.get("status") in {"queued", "running"}
        ):
            try:
                pid = int(row["pid"])
                if pid > 0:
                    os.kill(pid, 0)
                    return True
            except PermissionError:
                return True
            except (ProcessLookupError, KeyError, TypeError, ValueError):
                pass
    return False


def evidence(pr, head, *, row=None):
    settled, _ = review_state(pr, head, row if row is not None else review(pr))
    return settled, statuses(head), running_certs(pr, head)


def base_head(pr, head):
    row = json_gh("pr", "view", str(pr), "--json", "baseRefOid,headRefOid")
    if row["headRefOid"] != head:
        raise ValueError("head changed while checking the certification plan")
    return row["baseRefOid"]


def blocking(statuses):
    """Certifications that stop readiness: any test failure, or an error on a required one.

    An error on an optional platform (host offline, infrastructure) only means hosted CI covers it.
    """
    required = set(P.get("drafts", "required_statuses", [])) if not routed() else set()
    return {
        context: state
        for context, state in statuses.items()
        if state == "failure" or (state == "error" and context in required)
    }


def complete(result, *, planned=None):
    accepted, statuses, running = result
    required = set(P.get("drafts", "required_statuses", []))
    return (
        accepted
        and (
            planned
            if planned is not None
            else bool(statuses)
            and all(statuses.get(context) == "success" for context in required)
        )
        and "pending" not in statuses.values()
        and not blocking(statuses)
        and not running
    )


def request_certification(pr, head, targets):
    log = P.data / "logs" / f"cert-pr-{pr}-{head[:9]}-auto.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as out:
        return subprocess.Popen(
            cli("cert-pr", pr, *targets, "--head", head),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def request_missing(pr, head, result, *, base=None):
    accepted, statuses, running = result
    mapping = P.get("drafts", "status_targets", {})
    missing = set(P.get("drafts", "required_statuses", [])) - statuses.keys()
    slots = P.get("certification", "slots", {})
    layers = P.get("certification", "layers", {})
    missing_targets = set()
    for target, hosts in slots.items():
        layer = layers.get(target, target)
        contexts = (
            {P.cert_prefix + P.hosts[host].get("platform", "") for host in hosts}
            if layer == "platform"
            else {P.cert_prefix + layer}
        )
        if any(
            mapping.get(key) == target or key not in mapping and key in contexts
            for key in missing
        ):
            missing_targets.add(target)
    if accepted and (running or (base is None and statuses and not missing_targets)):
        return False
    with locked(f"draft-prerequisites-{pr}.lock"):
        path = P.data / f"draft-prerequisites-{pr}.json"
        marker = load(path, {})
        if marker.get("head") != head:
            marker = {"head": head}
        row = owner_pr(pr, draft=True)
        if row["headRefOid"] != head:
            return False
        settled, may_request_review = review_state(pr, head, row)
        request_review = may_request_review and not marker.get("review")
        requested = marker.get("cert", [])
        targets = sorted(missing_targets - set(requested))
        request_default = not statuses and not requested
        route_attempt = marker.get("route_attempt", {})
        if route_attempt.get("base") != base:
            route_attempt = {}
        request_cert = (
            accepted
            and settled
            and not running
            and (
                not plan_outcome(pr, head, base)
                and route_attempt.get("requests", 0) < 2
                and not cert_pid(route_attempt)
                if base is not None
                else request_default or targets and "*" not in requested
            )
        )
        if not (request_review or request_cert):
            return False
        changed = False
        if request_review:
            reviewer = P.get("drafts", "reviewer", "@copilot")
            gh(
                "pr",
                "edit",
                str(pr),
                "--add-reviewer",
                reviewer,
                check=True,
                timeout=90,
            )
            print(f"PR #{pr} head {head[:9]}: requested {reviewer} review", flush=True)
            marker["review"] = True
            save(path, marker)
            changed = True

        if request_cert and not running_certs(pr, head):
            targets = [] if base is not None or request_default else targets
            print(
                f"PR #{pr} head {head[:9]}: requesting certification"
                + (f" ({', '.join(targets)})" if targets else ""),
                flush=True,
            )
            pid = request_certification(pr, head, targets)
            if base is None:
                marker["cert"] = requested + (targets or ["*"])
            else:
                marker["route_attempt"] = {
                    "base": base,
                    "pid": pid,
                    "requests": route_attempt.get("requests", 0) + 1,
                }
            save(path, marker)
            changed = True
        return changed


def watch(pr):
    interval = P.get("drafts", "poll_seconds", 120)
    grace = P.get("drafts", "idle_seconds", 480)
    idle_since = None
    previous_head = None
    deadline = time.monotonic() + P.get("drafts", "watch_timeout", 21600)
    while True:
        if time.monotonic() >= deadline:
            raise ValueError("timed out waiting for settled review and certification")
        row = owner_pr(pr, draft=True)
        head = row["headRefOid"]
        base = base_head(pr, head) if routed() else None
        result = evidence(pr, head, row=row)
        outcome = plan_outcome(pr, head, base) if base is not None else None
        if outcome == "failure":
            raise ValueError(f"local tests failed on {head[:9]}")
        planned = outcome == "complete" if base is not None else None
        if previous_head != head or result[2]:
            idle_since = None
        previous_head = head
        if (
            complete(result, planned=planned)
            and owner_pr(pr, draft=True)["headRefOid"] == head
        ):
            print(
                f"PR {pr} head {head[:9]}: review and certification plan settled",
                flush=True,
            )
            return head
        accepted, statuses, running = result
        if not running and blocking(statuses):
            raise ValueError(f"certification failed on {head[:9]}: {statuses}")
        if request_missing(pr, head, result, base=base):
            idle_since = None
            time.sleep(interval)
            continue
        if not running:
            if idle_since is None:
                idle_since = time.monotonic()
            if time.monotonic() - idle_since >= grace:
                raise ValueError(
                    f"no running certification on {head[:9]}; "
                    f"exact-head review={accepted}, statuses={statuses}"
                )
        time.sleep(interval)


def wait_registered():
    marker = P.data / "ready-admission.json"
    previous = load(marker, None)
    if not previous:
        return
    deadline = time.monotonic() + P.get("drafts", "ready_run_timeout", 300)
    query = urlencode(
        {
            "head_sha": previous["head"],
            "event": "pull_request",
            "created": ">=" + previous["since"],
        }
    )
    workflow = quote(previous["workflow"], safe="")
    while True:
        runs = json_gh(
            "api", f"repos/{P.repo}/actions/workflows/{workflow}/runs?{query}"
        )

        def matches(row):
            pulls = row.get("pull_requests") or []
            repo = (row.get("head_repository") or {}).get("full_name", "")
            return (
                row.get("head_sha") == previous["head"]
                and row.get("display_title", "").endswith(previous["suffix"])
                and (
                    any(p["number"] == previous["pr"] for p in pulls)
                    or (
                        not pulls
                        and row.get("head_branch") == previous["branch"]
                        and repo.casefold() == P.repo.casefold()
                    )
                )
            )

        if any(matches(row) for row in runs["workflow_runs"]):
            marker.unlink()
            return
        if time.monotonic() >= deadline:
            raise ValueError(
                f"ready workflow for #{previous['pr']} has not appeared; admission held"
            )
        time.sleep(10)


def finish(pr):
    with locked(f"finish-draft-{pr}.lock", wait=False):
        if not review(pr)["isDraft"]:
            with locked("draft-admission.lock"):
                marker = load(P.data / "ready-admission.json", {})
                same_pr = marker.get("pr") == pr
                result = handoff(pr, marker["head"] if same_pr else None)
                if same_pr:
                    wait_registered()
            return result
        head = watch(pr)
        with locked("draft-admission.lock"):
            wait_registered()
            capacity = P.command("landing", "ci_capacity", pr=pr)
            if not capacity:
                raise ValueError("landing.ci_capacity must be configured")
            capacity += P.command("drafts", "capacity_args", pr=pr)
            deadline = time.monotonic() + P.get("drafts", "admission_timeout", 3600)
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError("timed out waiting for spare readiness capacity")
                row = owner_pr(pr, draft=True)
                if not review_state(pr, head, row)[0]:
                    raise ValueError(
                        "draft head changed or review still needs attention"
                    )
                if paused():
                    time.sleep(P.get("drafts", "poll_seconds", 120))
                    continue
                code = subprocess.run(
                    capacity, cwd=P.checkout, check=False, timeout=180
                ).returncode
                if code == 3:
                    time.sleep(P.get("drafts", "capacity_retry_seconds", 300))
                    continue
                if code:
                    raise ValueError(f"capacity gate exited {code}")
                base = base_head(pr, head) if routed() else None
                planned = (
                    plan_outcome(pr, head, base) == "complete"
                    if base is not None
                    else None
                )
                if not complete(evidence(pr, head), planned=planned):
                    raise ValueError("exact-head review or certification changed")
                wt, branch = handoff_worktree(pr, head)
                row = owner_pr(pr, draft=True)
                if not review_state(pr, head, row)[0] or (
                    base is not None and base_head(pr, head) != base
                ):
                    raise ValueError(
                        "draft head changed or review still needs attention"
                    )
                if paused():
                    continue
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                gh("pr", "ready", str(pr), check=True, timeout=90)
                if workflow := P.get("drafts", "ready_workflow"):
                    save(
                        P.data / "ready-admission.json",
                        {
                            "pr": pr,
                            "head": head,
                            "branch": branch,
                            "workflow": workflow,
                            "since": since,
                            "suffix": P.get("drafts", "ready_run_suffix", "/ ready"),
                        },
                    )
                if owner_pr(pr, draft=False)["headRefOid"] != head:
                    raise ValueError(
                        "head changed at readiness; not submitted to train"
                    )
                result = submit(pr, wt, head, readd=True)
                wait_registered()
                return result


def main(command):
    parser = argparse.ArgumentParser()
    parser.add_argument("pr", type=int)
    args = parser.parse_args()
    if args.pr < 1:
        parser.error("provide a positive PR number")
    try:
        assert_owner()
        if command == "watch-draft":
            watch(args.pr)
            return 0
        return (finish if command == "finish-draft" else handoff)(args.pr)
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"ATTENTION #{args.pr}: {exc}", flush=True)
        return 1
