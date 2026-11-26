"""Verify and adopt an owner draft without bypassing the landing train."""

import argparse
import os
import re
import subprocess
import time
from urllib.parse import quote, urlencode

from landing import assert_owner, json_gh, labels, paused, review, reviewed
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
    owner_pr(pr, draft=False)
    wt, _ = handoff_worktree(pr, head)
    return submit(pr, wt)


def submit(pr, wt):
    return subprocess.run(
        cli("train", "add", pr, "--worktree", wt), check=True
    ).returncode


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


def evidence(pr, head):
    pattern = P.get(
        "drafts",
        "reviewer_pattern",
        r"(?i)^(?:app/)?copilot(?:-pull-request-reviewer)?(?:\[bot\])?$",
    )
    api = f"repos/{P.repo}"
    reviews = json_gh("api", f"{api}/pulls/{pr}/reviews", "--paginate", "--slurp")
    reviewed_head = any(
        row.get("commit_id") == head
        and row.get("state") in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}
        and (user := row.get("user"))
        and user.get("type") == "Bot"
        and re.fullmatch(pattern, user["login"])
        for page in reviews
        for row in page
    )
    pages = json_gh("api", f"{api}/commits/{head}/statuses", "--paginate", "--slurp")
    latest = {}
    for page in pages:
        for row in page:
            if row["context"].startswith(P.cert_prefix):
                latest.setdefault(row["context"], row["state"])
    return reviewed_head, latest, running_certs(pr, head)


def blocking(statuses):
    """Certifications that stop readiness: any test failure, or an error on a required one.

    An error on an optional platform (host offline, infrastructure) only means hosted CI covers it.
    """
    required = set(P.get("drafts", "required_statuses", []))
    return {
        context: state
        for context, state in statuses.items()
        if state == "failure" or (state == "error" and context in required)
    }


def complete(result):
    accepted, statuses, running = result
    required = set(P.get("drafts", "required_statuses", []))
    return (
        accepted
        and bool(statuses)
        and all(statuses.get(context) == "success" for context in required)
        and "pending" not in statuses.values()
        and not blocking(statuses)
        and not running
    )


def request_certification(pr):
    """Start cert-pr in the background; it certifies the PR's current head."""
    log = P.data / "logs" / f"cert-pr-{pr}-auto.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as out:
        subprocess.Popen(
            cli("cert-pr", pr),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def watch(pr, *, certify=False):
    interval = P.get("drafts", "poll_seconds", 120)
    grace = P.get("drafts", "idle_seconds", 480)
    idle_since = None
    previous_head = None
    requested = set()
    while True:
        head = owner_pr(pr, draft=True)["headRefOid"]
        result = evidence(pr, head)
        if previous_head != head or result[2]:
            idle_since = None
        previous_head = head
        if complete(result) and owner_pr(pr, draft=True)["headRefOid"] == head:
            print(
                f"PR {pr} head {head[:9]}: reviewed; certification passed", flush=True
            )
            return head
        accepted, statuses, running = result
        if not running:
            if blocking(statuses):
                raise ValueError(f"certification failed on {head[:9]}: {statuses}")
            if certify and not statuses and head not in requested:
                # The implementer handed over without queuing certification.
                print(f"PR {pr} head {head[:9]}: requesting certification", flush=True)
                request_certification(pr)
                requested.add(head)
                idle_since = None
                time.sleep(interval)
                continue
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
        head = watch(pr, certify=True)
        with locked("draft-admission.lock"):
            wait_registered()
            capacity = P.command("landing", "ci_capacity", pr=pr)
            if not capacity:
                raise ValueError("landing.ci_capacity must be configured")
            capacity += P.command("drafts", "capacity_args", pr=pr)
            while True:
                row = owner_pr(pr, draft=True)
                if row["headRefOid"] != head or not reviewed(row):
                    raise ValueError(
                        "draft head changed or review still needs attention"
                    )
                if paused():
                    time.sleep(P.get("drafts", "poll_seconds", 120))
                    continue
                code = subprocess.run(capacity, cwd=P.checkout, check=False).returncode
                if code == 3:
                    time.sleep(P.get("drafts", "capacity_retry_seconds", 300))
                    continue
                if code:
                    raise ValueError(f"capacity gate exited {code}")
                if not complete(evidence(pr, head)):
                    raise ValueError("exact-head review or certification changed")
                wt, branch = handoff_worktree(pr, head)
                row = owner_pr(pr, draft=True)
                if row["headRefOid"] != head or not reviewed(row):
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
                result = submit(pr, wt)
                wait_registered()
                return result


def main(command):
    parser = argparse.ArgumentParser()
    parser.add_argument("pr", type=int)
    args = parser.parse_args()
    if args.pr < 1:
        parser.error("provide a positive PR number")
    try:
        if command == "watch-draft":
            watch(args.pr)
            return 0
        assert_owner()
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
