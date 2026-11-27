"""Refresh, validate and land ready PRs handed off by implementers."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from pathlib import Path

import stack_landing as stacks
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
from runtime import P, cli, gh, gh_argv, load, locked, questions, save, uuid4

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
        and row.get("stage") not in {"blocked", "closed", "merged"}
        for row in load(TRAIN, [])
    )


def register(pr, wt, head, *, readd=False, blocked_only=False):
    wt = str(Path(wt).resolve())
    added = False

    def update(rows):
        nonlocal added
        if blocked_only and not any(
            row["pr"] == pr and row.get("stage") == "blocked" for row in rows
        ):
            raise ValueError(f"#{pr} has no blocked landing train entry")
        for row in rows:
            if row["pr"] == pr and row.get("head", head) == head:
                stage = row.get("stage")
                if stage not in {"blocked", "closed", "merged"} and not (
                    readd and row["worktree"] != wt
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
            outcome, kind, detail = enqueue_result(n, head, active=active)
        except stacks.StackError as exc:
            log = action.failed("native-stack", exc)
            raise RetryLimit(f"Native stack landing stopped: {exc}; log {log}") from exc
        except ACTION_ERRORS as exc:
            action.failed(error_class(exc), exc)
            raise
        if outcome is EnqueueResult.QUEUED:
            if detail:
                sh([*cli(), "log", f"train #{n}: {detail}"])
            return True
        if outcome is EnqueueResult.STOPPED:
            if kind.startswith("stack-"):
                raise RetryLimit(detail)
            return False
        log = action.failed(kind, detail)
        if attempt == 0:
            action.check()
            if not active() or paused():
                return False
            current = review(n)
            if not current or current["headRefOid"] != head:
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


def refresh(n, wt, branch, *, active=lambda: True, draft_head=None):
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
    allowed = (
        eligible(current)
        if draft_head is None
        else current
        and current["isDraft"]
        and remote_head == draft_head
        and eligible({**current, "isDraft": False})
    )
    if not allowed or current["headRefOid"] != remote_head:
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


def refresh_requirements(wt, base, head):
    """Return conflicts and shared generated paths, not mere distance from base."""
    ancestor = git_step(wt, "merge-base", base, head).strip()
    if ancestor == base:
        return [], set()

    def changes(tip):
        return set(
            git_step(
                wt, "diff", "--name-only", "--no-renames", "-z", ancestor, tip, "--"
            )
            .rstrip("\0")
            .split("\0")
        )

    overlap = {path for path in changes(base) & changes(head) if GEN.fullmatch(path)}
    code, out = sh(
        [
            "git",
            "-c",
            "merge.generated.driver=git merge-file --marker-size=%L %A %O %B",
            "merge-tree",
            "--write-tree",
            "--name-only",
            base,
            head,
        ],
        cwd=wt,
        timeout=90,
    )
    if code not in (0, 1):
        raise RuntimeError(f"could not compute merge requirements: {out[-500:]}")
    conflicts = [p for p in out.split("\n\n")[0].splitlines()[1:] if p] if code else []
    if code and not conflicts:
        raise RuntimeError("could not identify files requiring conflict resolution")
    return conflicts, overlap


def latest_requirements(n, head):
    ref = f"refs/coord/train/{n}"
    git_step(MAIN, "fetch", "-q", P.remote, P.base, f"+pull/{n}/head:{ref}")
    if git_step(MAIN, "rev-parse", ref).strip() != head:
        raise ValueError(f"#{n}: head changed outside the train")
    base = git_step(MAIN, "rev-parse", P.base_ref).strip()
    return base, refresh_requirements(MAIN, base, head)


def unresolved_source_conflicts(n, wt, head, base, conflicts):
    source = [path for path in conflicts if not GEN.fullmatch(path)]
    if not source:
        return False
    local = git_step(wt, "rev-parse", "HEAD").strip()
    if (
        local != head
        and sh(["git", "merge-base", "--is-ancestor", head, local], cwd=wt, timeout=30)[
            0
        ]
        == 0
    ):
        local_conflicts, _ = refresh_requirements(wt, base, local)
        if all(GEN.fullmatch(path) for path in local_conflicts):
            return False
    need_agent(
        n,
        f"#{n}: conflicts with main in source files {source}; needs an agent to resolve.",
    )
    return True


def refresh_to_draft(n, wt, branch, head, *, active):
    current = review(n)
    if not active() or not eligible(current) or current["headRefOid"] != head:
        return
    if current["mergeQueueEntry"] or current["autoMergeRequest"]:
        need_agent(n, f"#{n}: dequeue or disable auto-merge before refreshing this PR.")
        return
    gh("pr", "ready", str(n), "--undo", check=True, timeout=90)
    pushed = refresh(n, wt, branch, active=active, draft_head=head)
    if pushed:
        need_agent(
            n,
            f"#{n}: refresh changed the head; review the new draft, then use finish-draft.",
        )
    elif pushed is False:
        need_agent(
            n,
            f"#{n}: refresh is no longer needed; re-admit the draft with finish-draft.",
        )


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
        try:
            stack = stacks.lookup(n)
        except stacks.StackError as exc:
            need_agent(n, f"Could not determine stack membership: {exc}")
            return
        if not eligible(review(n), stack=stack):
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
        if stack and (problem := stacks.prefix_problem(stack)):
            need_agent(n, problem)
            return
        head = info["headRefOid"]
        base, (conflicts, _) = latest_requirements(n, head)
        if unresolved_source_conflicts(n, wt, head, base, conflicts):
            return
        needs_lane = touches(n)
        status(n, "waiting", "waiting for a conflict-free lane slot")
        while paused() or (needs_lane and not acquire(n)):
            if not active():
                return
            time.sleep(120)
        if not active():
            return  # removed from the train (an agent took it back)
        base, (conflicts, overlap) = latest_requirements(n, head)
        if conflicts or overlap:
            if not unresolved_source_conflicts(n, wt, head, base, conflicts):
                if stack is not None or stacks.membership(n) is not None:
                    need_agent(
                        n,
                        f"#{n}: the train cannot resolve generated overlap or conflicts in a stack.",
                    )
                    return
                status(n, "fixing", "refreshing required generated overlap or conflict")
                refresh_to_draft(n, wt, info["headRefName"], head, active=active)
            return
        status(n, "waiting", "waiting for PR checks")
        checked_head = head
        entry["head"] = checked_head
        checks_action = train_retry(n, checked_head, "checks")
        checks_action.check()
        rerun_action = train_retry(n, checked_head, "cancelled-checks")
        deadline = time.time() + 3 * 3600
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
                why = f"#{n}: PR checks failed on the current head; see `gh pr checks {n}`."
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
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        while paused():
            if not active():
                return
            _, (conflicts, overlap) = latest_requirements(n, head)
            if conflicts or overlap:
                continue
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
    p.add_argument("cmd", choices=["add", "retry", "remove", "list", "run"])
    p.add_argument("pr", nargs="?", type=int)
    p.add_argument("--worktree")
    a = p.parse_args()
    if a.cmd in {"add", "retry", "remove"} and (a.pr is None or a.pr < 1):
        p.error(f"{a.cmd} requires a positive PR number")
    if a.cmd in {"add", "retry"}:
        from drafts import handoff_worktree, owner_pr

        assert_owner()
        retry = a.cmd == "retry"
        path = a.worktree
        if retry:
            parked = next(
                (
                    row
                    for row in load(TRAIN, [])
                    if row["pr"] == a.pr and row.get("stage") == "blocked"
                ),
                None,
            )
            if parked is None:
                raise ValueError(f"#{a.pr} has no blocked landing train entry")
            path = path or parked["worktree"]
        head = owner_pr(a.pr, draft=False)["headRefOid"]
        wt = Path(path).expanduser() if path else P.worktree(a.pr)
        if not wt.exists() and not wt.is_symlink():
            wt, _ = handoff_worktree(a.pr, head)
        register(a.pr, wt, head, readd=True, blocked_only=retry)
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
