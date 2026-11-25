"""Block until something needs the coordinator, print the events, and exit (the exit notifies the coordinator).

Events: a failed check on main's head commit; a PR removed from the merge queue for a reason other than
manual/merged; an owner answer on the dashboard; a new agent question for the coordinator.
Seen events are remembered in events_seen.json so each is reported once.
"""

import argparse
import json
import subprocess
import sys
import time

from runtime import P, cli, gh_argv, load, save

HERE = P.data
SEEN = HERE / "events_seen.json"
REPO = P.repo
IGNORED_CHECKS = tuple(P.get("events", "ignored_check_prefixes", [P.cert_prefix]))
KNOWN_FILE = HERE / "events_known.json"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "interval", nargs="?", type=int, default=P.get("events", "interval", 90)
    )
    interval = parser.parse_args().interval
    if interval < 1:
        parser.error("interval must be positive")
    seen = set(load(SEEN, []))
    first = not SEEN.exists()
    while True:
        events = answers() + agent_questions()
        for collect in (main_failures, queue_removals):
            try:
                events += collect()
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                print(f"events: {exc}", file=sys.stderr, flush=True)
        new = [(k, msg) for k, msg in events if k not in seen]
        if new:
            seen.update(k for k, _ in new)
            save(SEEN, sorted(seen))
            if not first:
                for _, msg in new:
                    subprocess.run([*cli(), "log", "EVENT " + msg[:300]])
                print("\n".join(msg for _, msg in new))
                return
        first = False
        time.sleep(interval)


if __name__ == "__main__":
    main()
