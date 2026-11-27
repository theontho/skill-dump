"""Refresh, validate and land ready PRs handed off by implementers."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path

from landing import (
    EnqueueResult,
    assert_owner,
    checks,
    eligible,
    enqueue_result,
    paused,
    pull,
    queued,
    review,
)
from lane import acquire, touches
from remote import RT_NOT_ADMITTED
from retries import ACTION_ERRORS, Retry, RetryLimit, error_class
from runtime import P, cli, gh_argv, load, locked, questions, save, uuid4

HERE = P.data
MAIN = P.checkout
TRAIN = HERE / "train.json"
GEN = P.generated
QUICK_TESTS = P.get("landing", "quick_tests", [])
LOCK = threading.Lock()


def sh(cmd, cwd=None, timeout=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout if r.returncode == 0 else r.stdout + r.stderr)


def locked_json(path, update=None, default=None):
    with LOCK, locked("train.lock"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = default
        if update is not None:
            data = update(data)
            save(path, data)
        return data


def status(n, stage, note):
    sh([*cli(), "pr", str(n), stage, "--machine", "landing train", "--note", note])
    sh([*cli(), "log", f"train #{n}: {stage}: {note}"])

    def upd(t):
        for e in t:
            if e["pr"] == n:
                e.update(stage=stage, note=note, at=time.strftime("%Y-%m-%d %H:%M"))
        return t

    locked_json(TRAIN, upd, [])


def need_agent(n, why):
    def upd(qs):
        qs = qs or []
        qs.append(
            {
                "id": f"train-{n}-{int(time.time())}",
                "agent": "landing-train",
                "pr": f"#{n}",
                "question": why,
            }
        )
        return qs

    questions(upd, agent=True)
    status(n, "blocked", why[:200])


def registered(pr, head):
    return any(
        row["pr"] == pr
        and row.get("head", head) == head
        and row.get("stage") not in {"closed", "merged"}
        for row in load(TRAIN, [])
    )


def register(pr, wt, head, *, readd=False):
    wt = str(Path(wt).resolve())
    added = False

    def update(rows):
        nonlocal added
        for row in rows:
            if row["pr"] == pr and row.get("head", head) == head:
                stage = row.get("stage")
                if stage not in {"closed", "merged"} and not (
                    readd and (stage == "blocked" or row["worktree"] != wt)
                ):
                    row.setdefault("head", head)
                    return rows
        added = True
        return [row for row in rows if row["pr"] != pr] + [
            {
                "pr": pr,
                "head": head,
                "worktree": wt,
                "stage": "new",
                "added": time.strftime("%Y-%m-%d %H:%M"),
                "token": uuid4().hex,
            }
        ]

    locked_json(TRAIN, update, [])
    if added:
        sh(
            [
                *cli(),
                "log",
                f"#{pr}: registered {head[:9]} with the landing train ({wt})",
            ]
        )
        print(f"#{pr} handed to the landing train ({wt}); you can stop now.")
    else:
        print(f"#{pr} already registered with the landing train at {head[:9]}.")


def train_retry(n, head, step, **inputs):
    return Retry(
        P.data,
        f"train-{step}",
        pr=n,
        head=head,
        settings=P.settings.get("landing"),
        **inputs,
    )


def enqueue_checked(n, head, active=lambda: True):
    action = train_retry(n, head, "enqueue")
    dropped = train_retry(n, head, "merge-group").previous("queue-failed_checks")
    if dropped:
        raise RetryLimit(
            f"Merge group failed at {head[:9]}; diagnose before re-enqueueing. Log: {dropped[-1]}"
        )
    for attempt in range(2):
        if not active() or paused():
            return False
        action.check()
        try:
            outcome, kind, detail = enqueue_result(n, head)
        except ACTION_ERRORS as exc:
            action.failed(error_class(exc), exc)
            raise
        if outcome is EnqueueResult.QUEUED:
            return True
        if outcome is EnqueueResult.STOPPED:
            return False
        log = action.failed(kind, detail)
        if attempt == 0:
            action.check()
            if not active() or paused():
                return False
            current = review(n)
            if not eligible(current) or current["headRefOid"] != head:
                return False
            sh([*cli(), "log", f"train #{n}: re-checking enqueue gate once; log {log}"])
            time.sleep(P.get("landing", "enqueue_retry_seconds", 15))
    action.check()
    return False


def gh_json(args):
    code, out = sh(gh_argv(*args))
    return json.loads(out) if code == 0 and out.strip().startswith(("{", "[")) else None


def pr_info(n):
    return gh_json(
        [
            "pr",
            "view",
            str(n),
            "--json",
            "state,isDraft,headRefName,headRefOid,baseRefOid,mergeStateStatus",
        ]
    )


def in_queue(n):
    return queued(n)


def last_removal(n):
    nodes = pull(
        n,
        "timelineItems(last:1,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT])"
        "{nodes{... on RemovedFromMergeQueueEvent{reason createdAt}}}",
    )["timelineItems"]["nodes"]
    return (nodes[0]["reason"], nodes[0]["createdAt"]) if nodes else (None, None)


def checks_ready(n):
    return checks(n)


def only_cancelled_run(run_id):
    """True if a workflow run's only non-success jobs are cancelled ones (plus aggregate gates)."""
    code, out = sh(gh_argv("run", "view", str(run_id), "--json", "jobs"))
    jobs = json.loads(out)["jobs"] if code == 0 else []
    aggregates = set(P.get("landing", "aggregate_checks", ["required"]))
    return only_cancelled_jobs(jobs, aggregates)


def only_cancelled_jobs(jobs, aggregates):
    return any(job.get("conclusion") == "cancelled" for job in jobs) and all(
        job.get("conclusion") in {"success", "skipped", "neutral", "cancelled"}
        or (job.get("conclusion") == "failure" and job.get("name") in aggregates)
        for job in jobs
    )


def only_cancelled(n):
    runs = (
        gh_json(
            [
                "run",
                "list",
                "--event",
                "merge_group",
                "--limit",
                "40",
                "--json",
                "databaseId,headBranch",
            ]
        )
        or []
    )
    return next(
        (
            only_cancelled_run(run["databaseId"])
            for run in runs
            if f"pr-{n}-" in run["headBranch"]
        ),
        False,
    )


def wait_merged_or_removed(n, head, since, active=lambda: True):
    action = train_retry(n, head, "merge-group")
    while active():
        info = pr_info(n)
        if info and info["state"] == "MERGED":
            status(n, "merged", "landed by the train")
            return True
        if not in_queue(n):
            reason, at = last_removal(n)
            if at and at >= since:
                if paused():
                    time.sleep(120)
                    continue
                cancelled = reason == "failed_checks" and only_cancelled(n)
                if cancelled and (not info or info["headRefOid"] != head):
                    need_agent(n, f"#{n}: cancellation-only retry could not safely enqueue; head changed.")
                    return False
                if cancelled and not action.previous("queue-cancelled"):
                    # The merge-group run was cancelled (queue rebuilt), not failed: re-enqueue once.
                    if not active():
                        return False
                    action.failed(
                        "queue-cancelled", f"Merge-group cancellation at {at}."
                    )
                    if not enqueue_checked(n, head, active):
                        need_agent(
                            n,
                            f"#{n}: cancellation-only retry could not safely enqueue.",
                        )
                        return False
                    status(
                        n,
                        "queued",
                        "merge-group run was cancelled (queue rebuilt); re-enqueued",
                    )
                    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    time.sleep(120)
                    continue
                why = f"#{n} was removed from the merge queue ({reason} at {at}); look at the merge-group run."
                log = action.failed(f"queue-{reason}", why)
                need_agent(n, f"{why} Log: {log}")
                return False
        time.sleep(180)


def settled_head(n, expected, attempts=5, pause=30):
    """The PR head, giving GitHub time to report our own just-pushed head before calling it foreign."""
    head = pr_info(n)["headRefOid"]
    for _ in range(attempts):
        if head == expected:
            break
        time.sleep(pause)
        head = pr_info(n)["headRefOid"]
    return head


def refresh(n, wt, branch, *, active=lambda: True):
    """Merge origin/main; regenerate; push. Returns True if HEAD changed and was pushed."""
    git_step(wt, "fetch", "-q", P.remote, P.base, branch, timeout=180)
    code, out = sh(["git", "status", "--porcelain", "--untracked-files=no"], cwd=wt)
    dirty = [line[3:] for line in out.splitlines() if line.strip()]
    if dirty and all(GEN.fullmatch(p) for p in dirty):
        # Leftover regeneration from an interrupted refresh: it is regenerated below anyway.
        git_step(wt, "checkout", "--", *dirty)
        out = ""
    if out.strip():
        need_agent(
            n, f"#{n}: worktree {wt} has uncommitted changes; train won't touch it."
        )
        return None
    # Keep unpushed local commits (e.g. an agent's conflict resolution) when they build on the remote branch.
    code, cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    ahead = (
        cur.strip() == branch
        and sh(
            ["git", "merge-base", "--is-ancestor", f"{P.remote}/{branch}", "HEAD"],
            cwd=wt,
        )[0]
        == 0
    )
    if not ahead:
        # Never destroy divergent commits (or switch an unrelated branch in a supplied worktree).
        if (
            cur.strip() != branch
            or sh(
                ["git", "merge-base", "--is-ancestor", "HEAD", f"{P.remote}/{branch}"],
                cwd=wt,
            )[0]
        ):
            need_agent(
                n,
                f"#{n}: worktree branch is wrong or diverged; local commits left intact.",
            )
            return None
        git_step(wt, "merge", "--ff-only", f"{P.remote}/{branch}")
    _, local_only = sh(
        ["git", "rev-list", "--count", f"{P.remote}/{branch}..HEAD"], cwd=wt
    )
    code, _ = sh(["git", "merge-base", "--is-ancestor", P.base_ref, "HEAD"], cwd=wt)
    if code == 0 and local_only.strip() not in ("", "0"):
        code = 1  # main already merged locally but unpushed: still run regen/gate/push below
    if code == 0:
        return False
    code, out = sh(
        ["git", "-c", "merge.generated.driver=true", "merge", "--no-edit", P.base_ref],
        cwd=wt,
    )
    if code != 0:
        _, conflicted = sh(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt)
        files = conflicted.splitlines()
        if not files:
            raise RuntimeError(f"git merge failed: {out[-500:]}")
        if any(not GEN.fullmatch(f) for f in files):
            sh(["git", "merge", "--abort"], cwd=wt)
            need_agent(
                n,
                f"#{n}: merging main conflicts in source files {files}; needs an agent to resolve.",
            )
            return None
        for f in files:
            # Take main's side. If main deleted the file (modify/delete), delete it too; never resurrect it.
            on_main = sh(["git", "cat-file", "-e", f"{P.base_ref}:{f}"], cwd=wt)[0] == 0
            if on_main:
                git_step(wt, "checkout", "--theirs", "--", f)
                git_step(wt, "add", "--", f)
            else:
                git_step(wt, "rm", "-q", "--", f)
        git_step(wt, "-c", "core.editor=true", "commit", "--no-edit", "-q")
    _, gone = sh(
        ["git", "diff", "--name-only", "--diff-filter=A", P.base_ref, "HEAD"], cwd=wt
    )
    stale = [
        f
        for f in gone.splitlines()
        if GEN.fullmatch(f)
        and sh(["git", "log", "-1", "--format=%h", P.base_ref, "--", f], cwd=wt)[
            1
        ].strip()
    ]
    if stale:  # generated file main deleted but this PR still carries: remove it
        git_step(wt, "rm", "-q", "--", *stale)
        git_step(
            wt, "commit", "-q", "-m", "Drop generated files that main no longer tracks"
        )
    for step in ("provision", "regenerate"):
        command = P.command("landing", step, worktree=wt, pr=n)
        if command:
            code, out, log = run_step(n, wt, step, command, timeout=1800)
            if code:
                need_agent(
                    n,
                    f"#{n}: {step} failed after merging the base; log {log}\n{out[-1500:]}",
                )
                return None
    _, changed = sh(["git", "status", "--porcelain"], cwd=wt)
    if changed.strip():
        paths = [line[3:] for line in changed.splitlines()]
        if not all(GEN.fullmatch(path) for path in paths):
            need_agent(
                n, f"#{n}: regeneration modified non-generated files; review {wt}."
            )
            return None
        git_step(wt, "add", "-A", "--", *paths)
        git_step(
            wt, "commit", "-q", "-m", "Regenerate references after merging the base"
        )
    gate = P.command("landing", "quick_gate", worktree=wt, pr=n)
    quick = existing_tests(wt)
    code, out = 0, ""
    if gate and (quick or not QUICK_TESTS):
        host = P.get("landing", "quick_host")
        if host:
            code, out, log = run_step(
                n,
                wt,
                "quick-gate",
                cli("rt", host, wt, "--admit-now", "--", *gate, *quick),
                timeout=5400,
                declined=RT_NOT_ADMITTED,
            )
        if not host or code == RT_NOT_ADMITTED:
            command = P.command("landing", "quick_local_prefix", pr=n)
            code, out, log = run_step(
                n, wt, "quick-gate", [*command, *gate, *quick], timeout=5400
            )
    if code != 0:
        tail = "\n".join(l for l in out.splitlines() if "agent-coord:" not in l)[-1500:]
        need_agent(
            n, f"#{n}: quick gate failed after merging main + regen; log {log}\n{tail}"
        )
        return None
    if not active():
        return None
    current = review(n)
    if current and current["mergeQueueEntry"]:
        return "queued"  # preserve any unpushed local resolutions
    remote_head = git_step(wt, "rev-parse", f"{P.remote}/{branch}").strip()
    if not eligible(current) or current["headRefOid"] != remote_head:
        need_agent(
            n, f"#{n}: head or eligibility changed during refresh; no push made."
        )
        return None
    if not active():
        return None
    code, out, log = run_step(
        n,
        wt,
        "push",
        ["git", "push", "-q", P.remote, f"HEAD:refs/heads/{branch}"],
        timeout=180,
    )
    if code != 0:
        need_agent(n, f"#{n}: push failed; log {log}: {out[-500:]}")
        return None
    return True


def git_step(wt, *args, timeout=180):
    code, out = sh(["git", *args], cwd=wt, timeout=timeout)
    if code:
        raise RuntimeError(f"git {args[0]} failed: {out[-500:]}")
    return out


def existing_tests(wt):
    return [test for test in QUICK_TESTS if (Path(wt) / test).is_file()]


def run_step(n, wt, step, command, *, timeout, declined=None):
    head = git_step(wt, "rev-parse", "HEAD").strip()
    action = train_retry(n, head, step, argv=command)
    with action.observe():
        code, out = sh(command, cwd=wt, timeout=timeout)
    log = (
        action.failed(f"exit-{code}", out[-16000:]) if code and code != declined else ""
    )
    return code, out, log


def assigned(entry):
    token = entry.get("token", entry.get("added"))
    return any(
        row["pr"] == entry["pr"] and row.get("token", row.get("added")) == token
        for row in load(TRAIN, [])
    )


def land_safely(entry, attempts=3, pause=120):
    """Land a PR; retry transient failures (GitHub API, network) before asking for help.

    land() resumes from the PR's current state, so a retry repeats no completed step.
    """
    n = entry["pr"]
    entry = entry.copy()
    try:
        with locked(f"land-pr-{n}.lock", wait=False):
            for attempt in range(1, attempts + 1):
                if entry.get("added") and not assigned(entry):
                    return
                try:
                    info = pr_info(n)
                    if not info:
                        raise RuntimeError("could not read PR state")
                    entry["head"] = info["headRefOid"]
                    entry["base"] = info.get("baseRefOid")
                    train_retry(n, entry["head"], "land", base=entry["base"]).check()
                    land(entry)
                    return
                except RetryLimit as exc:
                    if not entry.get("added") or assigned(entry):
                        need_agent(n, str(exc))
                    return
                except ACTION_ERRORS as exc:
                    if isinstance(exc, BlockingIOError) or (
                        entry.get("added") and not assigned(entry)
                    ):
                        return
                    action = train_retry(
                        n, entry.get("head"), "land", base=entry.get("base")
                    )
                    log = action.failed(error_class(exc), exc)
                    try:
                        action.check()
                    except RetryLimit as limit:
                        need_agent(n, str(limit))
                        return
                    if attempt == attempts:
                        need_agent(n, f"Train stopped safely: {exc}; log {log}")
                        return
                    sh([*cli(), "log", f"train #{n}: retrying after: {exc}; log {log}"])
                    time.sleep(pause * attempt)
    except BlockingIOError:
        return


def land(entry):
    n, wt = entry["pr"], entry["worktree"]

    def active():
        return assigned(entry)

    if not active():
        return
    if not Path(wt).exists() and not Path(wt).is_symlink():
        from drafts import handoff_worktree

        wt, _ = handoff_worktree(n, entry.get("head"))
        wt = str(wt)
        locked_json(
            TRAIN,
            lambda rows: [
                dict(row, worktree=wt)
                if row.get("token", row.get("added"))
                == entry.get("token", entry.get("added"))
                and row["pr"] == n
                else row
                for row in rows
            ],
            [],
        )
    if git_step(
        wt, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ) != git_step(MAIN, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        need_agent(
            n, f"#{n}: assigned worktree does not belong to the configured checkout."
        )
        return
    while active():
        info = pr_info(n)
        if not info:
            raise RuntimeError("could not read PR state")
        if info["state"] != "OPEN":
            status(n, info["state"].lower(), "no longer open")
            return
        entry["head"] = info["headRefOid"]
        entry["base"] = info.get("baseRefOid")
        train_retry(n, entry["head"], "land", base=entry["base"]).check()
        if not eligible(review(n)):
            need_agent(
                n,
                f"#{n} is not an eligible ready owner PR targeting the configured base.",
            )
            return
        if in_queue(n):
            status(n, "queued", "already in the merge queue; watching")
            wait_merged_or_removed(
                n,
                info["headRefOid"],
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                active,
            )
            return
        # Source conflicts with main need an agent; flag them now instead of after the lane wait.
        git_step(
            MAIN,
            "fetch",
            "-q",
            P.remote,
            P.base,
            f"+pull/{n}/head:refs/coord/train/{n}",
        )
        # Prefer the worktree's unpushed commits (an agent's local conflict resolution) over the remote head.
        _, local_head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
        ahead = (
            sh(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    f"refs/coord/train/{n}",
                    local_head.strip(),
                ],
                cwd=MAIN,
            )[0]
            == 0
        )
        target = local_head.strip() if ahead else f"refs/coord/train/{n}"
        code, out = sh(
            [
                "git",
                "-C",
                str(MAIN),
                "-c",
                "merge.generated.driver=git merge-file --marker-size=%L %A %O %B",
                "merge-tree",
                "--write-tree",
                "--name-only",
                P.base_ref,
                target,
            ]
        )
        if code == 1:
            paths = [p for p in out.split("\n\n")[0].splitlines()[1:] if p]
            source = [p for p in paths if not GEN.fullmatch(p)]
            if source:
                need_agent(
                    n,
                    f"#{n}: conflicts with main in source files {source}; needs an agent to resolve.",
                )
                return
        needs_lane = touches(n)
        status(n, "waiting", "waiting for a conflict-free lane slot")
        while paused() or (needs_lane and not acquire(n)):
            if not active():
                return
            time.sleep(120)
        if not active():
            return  # removed from the train (an agent took it back)
        status(n, "fixing", "merging main + regenerating")
        pushed = refresh(n, wt, info["headRefName"], active=active)
        if pushed is None:
            return
        if pushed == "queued":
            status(
                n,
                "queued",
                "enqueued elsewhere during refresh; local commits preserved, watching",
            )
            wait_merged_or_removed(
                n,
                pr_info(n)["headRefOid"],
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                active,
            )
            return
        status(n, "waiting", "waiting for PR checks")
        # After our own push GitHub can still report the previous head for a while; use what we pushed.
        checked_head = (
            sh(["git", "rev-parse", "HEAD"], cwd=wt)[1].strip()
            if pushed is True
            else pr_info(n)["headRefOid"]
        )
        entry["head"] = checked_head
        checks_action = train_retry(n, checked_head, "checks")
        checks_action.check()
        rerun_action = train_retry(n, checked_head, "cancelled-checks")
        deadline = time.time() + 3 * 3600
        time.sleep(120 if pushed else 0)
        while active():
            ok, pending, rows = checks_ready(n)
            if ok:
                break
            if not pending:
                # Lost runners/cancellations: rerun the run's failed+cancelled jobs once before asking for help.
                failed_runs = {
                    c["link"].split("/runs/")[1].split("/")[0]
                    for c in rows
                    if c.get("bucket") in ("fail", "cancel")
                    and "/runs/" in c.get("link", "")
                }
                if (
                    failed_runs
                    and not rerun_action.previous("cancelled")
                    and all(only_cancelled_run(r) for r in failed_runs)
                ):
                    rerun_action.failed(
                        "cancelled", f"Cancelled workflows: {sorted(failed_runs)}"
                    )
                    for r in failed_runs:
                        sh(gh_argv("run", "rerun", r, "--failed"))
                    status(n, "waiting", "reran cancelled jobs; waiting for PR checks")
                    time.sleep(180)
                    continue
                why = f"#{n}: PR checks failed on the refreshed head; see `gh pr checks {n}`."
                log = checks_action.failed("failed-checks", why)
                need_agent(n, f"{why} Log: {log}")
                return
            if time.time() > deadline:
                why = f"#{n}: PR checks still pending after 3 h."
                log = checks_action.failed("checks-timeout", why)
                need_agent(n, f"{why} Log: {log}")
                return
            time.sleep(180)
        if not active():
            return
        head = settled_head(n, checked_head)
        if head != checked_head:
            need_agent(n, f"#{n}: head changed outside the train; no enqueue made.")
            return
        # main may have moved while checks ran; if it did and we touch generated files, refresh again
        code, behind = sh(
            gh_argv(
                "api", f"repos/{P.repo}/compare/{P.base}...{head}", "--jq", ".behind_by"
            )
        )
        gen = touches(n)
        if gen and behind.strip() not in ("0", ""):
            # Only refresh again if it no longer merges cleanly with main.
            git_step(
                MAIN,
                "fetch",
                "-q",
                P.remote,
                P.base,
                f"+pull/{n}/head:refs/coord/train/{n}",
            )
            if (
                sh(
                    [
                        "git",
                        "-C",
                        str(MAIN),
                        "-c",
                        "merge.generated.driver=git merge-file --marker-size=%L %A %O %B",
                        "merge-tree",
                        "--write-tree",
                        P.base_ref,
                        f"refs/coord/train/{n}",
                    ]
                )[0]
                != 0
            ):
                continue
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        while paused():
            if not active():
                return
            time.sleep(120)
        if not active():
            return
        if not enqueue_checked(n, head, active):
            if paused():
                continue
            if active():
                need_agent(n, f"#{n}: enqueue gate rejected the current head; parked.")
            return
        status(n, "queued", f"enqueued at {head[:9]}")
        wait_merged_or_removed(n, head, since, active)
        return


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["add", "remove", "list", "run"])
    p.add_argument("pr", nargs="?", type=int)
    p.add_argument("--worktree")
    a = p.parse_args()
    if a.cmd in {"add", "remove"} and (a.pr is None or a.pr < 1):
        p.error("add/remove requires a positive PR number")
    if a.cmd == "add":
        from drafts import handoff_worktree, owner_pr

        assert_owner()
        head = owner_pr(a.pr, draft=False)["headRefOid"]
        wt = Path(a.worktree).expanduser() if a.worktree else P.worktree(a.pr)
        if not wt.exists() and not wt.is_symlink():
            wt, _ = handoff_worktree(a.pr, head)
        register(a.pr, wt, head, readd=True)
    elif a.cmd == "remove":
        locked_json(TRAIN, lambda t: [e for e in (t or []) if e["pr"] != a.pr], [])
        print(f"#{a.pr} removed from the landing train")
    elif a.cmd == "list":
        for e in locked_json(TRAIN, None, []) or []:
            print(e)
    else:
        assert_owner()
        with locked("train-daemon.lock", wait=False):
            run()


def run():
    running = {}
    while True:
        for e in locked_json(TRAIN, None, []) or []:
            if e["pr"] not in running and e.get("stage") not in (
                "merged",
                "closed",
                "blocked",
            ):
                t = threading.Thread(target=land_safely, args=(e,), daemon=True)
                t.start()
                running[e["pr"]] = t
        for n in [n for n, t in running.items() if not t.is_alive()]:
            del running[n]
        time.sleep(60)


if __name__ == "__main__":
    main()
