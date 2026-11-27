"""Wait for a batch of main failures, queue drops, questions, or idle hosts."""

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone

from host_activity import busy_hosts, probe_window
from runtime import P, SLUG, gh_argv, load, locked, save

HERE = P.data
REPO = P.repo
IGNORED_CHECKS = tuple(P.get("events", "ignored_check_prefixes", [P.cert_prefix]))
KNOWN_FILE = HERE / "events_known.json"
JOURNAL = "events_journal.json"
DEFAULT_CONSUMER = "supervisor"
PRODUCER_LOCK = "event-collect.lock"
PARKED_LABELS = {"check-later", "research-done", "hands-off"}


def empty_journal():
    return {"events": [], "consumers": {}, "legacy_seen": []}


def read_journal():
    try:
        return json.loads((HERE / JOURNAL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_journal()


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gh(*args):
    r = subprocess.run(
        gh_argv(*args), capture_output=True, text=True, timeout=90, check=False
    )
    if r.returncode:
        print(f"events: {r.stderr.strip()}", file=sys.stderr, flush=True)
    return r.stdout if r.returncode == 0 else ""


def main_failures():
    sha = gh("api", f"repos/{REPO}/commits/{P.base}", "--jq", ".sha").strip()
    if not sha:
        return []
    runs = gh(
        "api",
        f"repos/{REPO}/commits/{sha}/check-runs?per_page=100",
        "--paginate",
        "--jq",
        '.check_runs[] | select(.status=="completed" and (.conclusion=="failure" or .conclusion=="timed_out"))'
        ' | "\\(.name)\\t\\(.html_url)"',
    )
    events = []
    known = set(P.get("events", "known_checks", [])) | set(load(KNOWN_FILE, []))
    for line in runs.splitlines():
        name, _, url = line.partition("\t")
        if not name.startswith(IGNORED_CHECKS) and name not in known:
            events.append(
                (
                    f"main:{sha}:{name}",
                    f"MAIN RED: '{name}' failed on main {sha[:9]} — {url}",
                )
            )
    return events


def queue_removals():
    q = (
        '{repository(owner:"__OWNER__",name:"__REPO__"){pullRequests(states:OPEN,first:60,'
        "orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number title timelineItems(last:1,"
        "itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{... on RemovedFromMergeQueueEvent{reason createdAt}}}}}}}"
    )
    out = gh(
        "api",
        "graphql",
        "-f",
        "query="
        + q.replace("__OWNER__", P.repo_owner)
        .replace("__REPO__", P.repo_name)
        .replace('branch:"main"', f'branch:"{P.base}"'),
    )
    events = []
    if not out:
        return events
    for pr in json.loads(out)["data"]["repository"]["pullRequests"]["nodes"]:
        for ev in pr["timelineItems"]["nodes"]:
            if ev and ev.get("reason") not in (None, "manual", "merged"):
                events.append(
                    (
                        f"dropped:{pr['number']}:{ev['createdAt']}",
                        f"DROPPED: #{pr['number']} ({pr['title'][:60]}) removed from merge queue: "
                        f"{ev['reason']} at {ev['createdAt']}",
                    )
                )
    return events


def answers():
    try:
        qs = json.loads((HERE / "questions.json").read_text())
    except (OSError, ValueError):
        return []
    return [
        (
            f"answer:{q['id']}:{q.get('answered_at')}",
            f"ANSWER Q{q['id']}: {q['answer']}  (question: {q['question'][:160]})",
        )
        for q in qs
        if q.get("answer") and not str(q["answer"]).startswith("(coordinator)")
    ]


def agent_questions():
    try:
        qs = json.loads((HERE / "agent_questions.json").read_text())
    except (OSError, ValueError):
        return []
    return [
        (
            f"agentq:{q['id']}",
            f"AGENT QUESTION from {q['agent']} about {q.get('pr')}: {q['question']}",
        )
        for q in qs
        if not q.get("answer")
    ]


def reachable_idle_hosts(state, project, *, now):
    observed = state.get("hosts_observed_at")
    if observed is None or not 0 <= now - observed <= probe_window(project):
        return []
    busy = busy_hosts(state, project.hosts)
    return [
        key
        for key, spec in project.hosts.items()
        if spec.get("idle_alert", True)
        and key not in busy
        and state.get("hosts", {}).get(key, {}).get("reachable")
    ]


def idle_hosts(state, project, *, now):
    threshold = project.get("events", "idle_minutes", 45)
    events = []
    for key in reachable_idle_hosts(state, project, now=now):
        observed_at = state["hosts_observed_at"]
        episode = state.get("host_idle", {}).get(key, {})
        since = episode.get("since")
        if since is not None and observed_at - since >= threshold * 60:
            minutes = int((observed_at - since) // 60)
            host = project.hosts[key].get("display", key)
            events.append(
                (
                    f"idle:{key}:{episode['id']}",
                    f"IDLE: {host} has had no agent, lease or long run for {minutes} min",
                )
            )
    return events


def eligible_issue():
    args = [
        "issue",
        "list",
        "--repo",
        REPO,
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        "number,state,labels",
    ]
    if search := P.get("events", "queue_search", ""):
        args += ["--search", search]
    out = gh(*args)
    if not out:
        return None
    machine_prefix = P.get("labels", "machine_prefix", "machine-").casefold()
    for issue in json.loads(out):
        if issue["state"] != "OPEN":
            continue
        labels = {label["name"].casefold() for label in issue["labels"]}
        if not labels & PARKED_LABELS and not any(
            label.startswith(machine_prefix) for label in labels
        ):
            return issue["number"]
    return None


def inactive_minutes(since, at):
    return int(
        (datetime.fromisoformat(at) - datetime.fromisoformat(since)).total_seconds()
        // 60
    )


def activity_since(supervisor):
    return supervisor.get("last_heartbeat") or supervisor.get("monitoring_since")


def heartbeat(*, owner_resume=False):
    with locked("events-journal.lock"):
        journal = read_journal()
        if not (HERE / JOURNAL).exists():
            # Let the first collector still import legacy seen IDs.
            journal["collection_pending"] = True
        supervisor = journal.setdefault("supervisor", {})
        at = timestamp()
        since = activity_since(supervisor)
        if owner_resume and since:
            minutes = inactive_minutes(since, at)
            if minutes >= P.get("events", "supervisor_idle_minutes", 30):
                journal["events"].append(
                    {
                        "id": f"owner-resume:{since}",
                        "at": at,
                        "message": f"OWNER-RESUME: owner prompted the supervisor after {minutes} min without a heartbeat",
                    }
                )
        supervisor["last_heartbeat"] = at
        save(HERE / JOURNAL, journal)


def collect(*, legacy_baseline=False):
    events = answers() + agent_questions()
    for source in (main_failures, queue_removals):
        try:
            events += source()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as exc:
            print(f"events: {exc}", file=sys.stderr, flush=True)
    events += idle_hosts(load(HERE / "state.json", {}), P, now=time.time())
    with locked("events-journal.lock"):
        path = HERE / JOURNAL
        journal = read_journal()
        first = not path.exists() or journal.pop("collection_pending", False)
        if first:
            seen_path = HERE / "events_seen.json"
            try:
                journal["legacy_seen"] = json.loads(seen_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                journal["legacy_seen"] = (
                    sorted({key for key, _ in events if not key.startswith("idle:")})
                    if legacy_baseline
                    else []
                )
        seen = set(journal["legacy_seen"]) | {row["id"] for row in journal["events"]}
        at = timestamp()
        changed = first
        supervisor = journal.setdefault("supervisor", {})
        since = activity_since(supervisor)
        if since is None:
            supervisor["monitoring_since"] = since = at
            changed = True
        for key, message in events:
            if key not in seen:
                journal["events"].append({"id": key, "at": at, "message": message})
                seen.add(key)
                changed = True
        if changed:
            save(path, journal)

    key = f"supervisor-idle:{since}"
    if key not in seen and inactive_minutes(since, at) >= P.get(
        "events", "supervisor_idle_minutes", 30
    ):
        collect_supervisor_idle(key, since)


def collect_supervisor_idle(key, since):
    def idle_now():
        return reachable_idle_hosts(load(HERE / "state.json", {}), P, now=time.time())

    if not idle_now():
        return
    try:
        issue = eligible_issue()
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"events: issue queue: {exc}", file=sys.stderr, flush=True)
        return
    if issue is None:
        return
    with locked("events-journal.lock"):
        journal = read_journal()
        # A heartbeat or host update may have arrived during the GitHub query.
        if activity_since(journal["supervisor"]) != since:
            return
        if hosts := idle_now():
            names = ", ".join(P.hosts[host].get("display", host) for host in hosts)
            at = timestamp()
            minutes = inactive_minutes(since, at)
            journal["events"].append(
                {
                    "id": key,
                    "at": at,
                    "message": (
                        f"SUPERVISOR-IDLE: no heartbeat for {minutes} min; idle hosts: "
                        f"{names}; eligible queue item: #{issue}"
                    ),
                }
            )
            save(HERE / JOURNAL, journal)


def collect_main():
    parser = argparse.ArgumentParser(description="Collect events independently of waiters.")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    interval = P.get("events", "interval", 90)
    with locked(PRODUCER_LOCK):
        while True:
            try:
                collect()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"event-collect: {exc}", file=sys.stderr, flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(interval)


def deliver(consumer, *, as_json):
    journal = read_journal()
    cursor = journal["consumers"].get(consumer, {}).get("cursor", 0)
    batch = journal["events"][cursor:]
    if not batch:
        return False
    body = json.dumps(batch) if as_json else "\n".join(row["message"] for row in batch)
    try:
        print(body, flush=True)
    except BrokenPipeError:
        # Don't retry the failed flush during interpreter shutdown.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        raise
    # A failed output never advances the cursor. The waiter lock keeps reads for
    # this consumer exclusive even while the producer appends to the journal.
    with locked("events-journal.lock"):
        journal = read_journal()
        journal["consumers"][consumer] = {
            "cursor": cursor + len(batch),
            "last_consumed": timestamp(),
        }
        save(HERE / JOURNAL, journal)
    return True


def wait_for_events(consumer, *, as_json, standalone_interval=None):
    poll_at = 0
    previous = None
    while True:
        try:
            stat = (HERE / JOURNAL).stat()
        except FileNotFoundError:
            pass
        else:
            revision = (stat.st_ino, stat.st_mtime_ns)
            if revision != previous:
                if deliver(consumer, as_json=as_json):
                    return
                previous = revision
        now = time.monotonic()
        if standalone_interval is not None and now >= poll_at:
            try:
                with locked(PRODUCER_LOCK, wait=False):
                    collect(legacy_baseline=True)
            except BlockingIOError:
                # The run service (or another legacy waiter) owns detection.
                pass
            else:
                poll_at = time.monotonic() + standalone_interval
                continue
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interval", nargs="?", type=int, help="legacy poll interval")
    parser.add_argument("--wait", action="store_true", help="read events collected by run")
    parser.add_argument(
        "--consumer", default=DEFAULT_CONSUMER, help="cursor name (default: supervisor)"
    )
    parser.add_argument("--json", action="store_true", help="include stable event IDs")
    args = parser.parse_args()
    if args.wait and args.interval is not None:
        parser.error("interval is for legacy polling, not --wait")
    if len(args.consumer) > 64 or not SLUG.fullmatch(args.consumer):
        parser.error("consumer must be a slug of at most 64 characters")
    interval = (
        args.interval if args.interval is not None else P.get("events", "interval", 90)
    )
    if interval < 1:
        parser.error("interval must be positive")
    with ExitStack() as stack:
        try:
            stack.enter_context(locked(f"events-wait-{args.consumer}.lock", wait=False))
        except BlockingIOError:
            print(
                f"events: consumer {args.consumer!r} already has a waiter",
                file=sys.stderr,
            )
            return 1
        wait_for_events(
            args.consumer,
            as_json=args.json,
            standalone_interval=None if args.wait else interval,
        )


if __name__ == "__main__":
    sys.exit(main())
