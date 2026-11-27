"""Read local efficiency evidence without uploading session or log contents."""

import json
import re
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from efficiency_github import iso, stamp


def json_lines(stream):
    for line in stream:
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            if line.endswith("\n"):
                raise
            # The last line may be in the process of being appended.


def certification(logdir, lo, hi, markers):
    by_pr = defaultdict(list)
    busy = legacy = count = 0
    for path in sorted(logdir.glob("cert-*.log")):
        match = re.fullmatch(r"cert-(\d+)-.+-([0-9a-f]{7,40})\.log", path.name)
        if not match:
            continue
        mtime = path.stat().st_mtime
        if mtime < lo:
            continue
        refused = False
        undated = []
        legacy_at = None
        ended = start = 0
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith("CERT START "):
                    started = line.removeprefix("CERT START ").strip().split()
                    start = stamp(started[0])
                    if legacy_at is None and len(started) == 2:
                        legacy_at = float(started[1])
                elif line.startswith("CERT END "):
                    ended = stamp(line.removeprefix("CERT END ").strip())
                elif exit_code := re.fullmatch(r"EXIT (\d+)\s*", line):
                    refusal = bool(int(exit_code[1]) and refused)
                    if not ended:
                        undated.append(refusal)
                    elif lo <= ended < hi:
                        count += 1
                        if refusal:
                            busy += 1
                        else:
                            by_pr[int(match[1])].append(
                                (match[2], start or ended, ended)
                            )
                    refused = False
                    ended = start = 0
                else:
                    refused |= any(marker in line for marker in markers)
        at = legacy_at if legacy_at is not None else mtime
        if lo <= at < hi:
            count += len(undated)
            legacy += len(undated)
            busy += sum(undated)
            by_pr[int(match[1])].extend(
                (match[2], at, at) for refusal in undated if not refusal
            )
    return (
        by_pr,
        {
            "cert_busy_refusals": busy,
            "cert_attempts": count,
        },
        legacy,
    )


def queue_metrics(sources, identity, lo, hi):
    counts = Counter(started=0.0, cancelled=0.0)
    for rows in sources:
        seen = set()
        for row in rows:
            event = row["event"]
            if event not in counts or not identity.fullmatch(row.get("agent", "")):
                continue
            if not lo <= row["time"] < hi:
                continue
            key = (row["id"], row["time"], event)
            if key not in seen:
                counts[event] += row.get("wait", 0) / 3600
                seen.add(key)
    return {
        "lease_queue_hours": sum(counts.values()),
        **{f"lease_queue_hours_{event}": hours for event, hours in counts.items()},
    }


def leases(project, lo, hi):
    identity = re.compile(
        project.get(
            "efficiency",
            "lease_agent_pattern",
            "|".join(map(re.escape, {project.name, project.checkout.name})),
        )
    )
    extra = project.get("efficiency", "lease_history_files", [])
    sources = []
    notes = []
    try:
        seconds = max(1, int(time.time() - lo) + 2)
        raw = subprocess.run(
            ["agent-coord", "history", "--since", f"{seconds}s", "--json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=90,
        ).stdout
        sources.append(list(json_lines(raw.splitlines(keepends=True))))
    except (OSError, ValueError, subprocess.SubprocessError):
        notes.append("Local agent-coord history unavailable.")
    for i, path in enumerate(extra, 1):
        try:
            with Path(path).expanduser().open(encoding="utf-8") as stream:
                sources.append(list(json_lines(stream)))
        except (OSError, ValueError):
            notes.append(f"Configured lease history export {i} unavailable.")
    notes.append(
        f"Lease histories read: {len(sources)}/{1 + len(extra)}; retained, matching agent names only."
    )
    return (queue_metrics(sources, identity, lo, hi) if sources else {}), notes


def poll_kind(tool, args):
    if tool == "read_bash" or (tool == "read_agent" and args.get("wait")):
        return tool
    if tool != "bash" or args.get("mode") == "async":
        return None
    command = args.get("command", "")
    for name, pattern in (
        ("bash.sleep", r"\bsleep\s+[\d.]"),
        ("bash.gh_watch", r"\bgh\s+(?:run\s+watch|pr\s+checks\b[^\n]*--watch)"),
        ("bash.pr_coord_events", r"\bpr-coord\s+[^\n;|]*\bevents\b"),
    ):
        if re.search(pattern, command):
            return name
    return None


def session_metrics(paths, supervisors, lo, hi, idle_seconds):
    metrics = Counter(blocking_question_hours=0.0, polling_hours=0.0)
    for tool in (
        "read_bash",
        "read_agent",
        "bash.sleep",
        "bash.gh_watch",
        "bash.pr_coord_events",
    ):
        metrics[f"polling_hours.{tool}"] = 0.0
    if supervisors:
        for who in ("owner", "notification"):
            metrics[f"supervisor_idle_hours_{who}"] = 0.0
            metrics[f"supervisor_idle_gaps_{who}"] = 0

    def hours(start, end):
        return max(0, min(end, hi) - max(start, lo)) / 3600

    for session_id, path in paths:
        active = {}
        final = 0
        resume = None
        supervisor = session_id in supervisors
        with path.open(encoding="utf-8") as stream:
            for event in json_lines(stream):
                kind = event["type"]
                if kind not in {
                    "tool.execution_start",
                    "tool.execution_complete",
                    "assistant.message",
                    "assistant.turn_start",
                    "user.message",
                    "system.notification",
                }:
                    continue
                at = stamp(event["timestamp"])
                data = event.get("data", {})
                if kind == "tool.execution_start":
                    tool = data["toolName"].removeprefix("functions.")
                    args = data.get("arguments") or {}
                    category = (
                        "question" if tool == "ask_user" else poll_kind(tool, args)
                    )
                    if category:
                        active[data["toolCallId"]] = (at, category)
                elif kind == "tool.execution_complete" and (
                    item := active.pop(data["toolCallId"], None)
                ):
                    duration = hours(item[0], at)
                    if item[1] == "question":
                        metrics["blocking_question_hours"] += duration
                    elif duration:
                        metrics["polling_hours"] += duration
                        metrics[f"polling_hours.{item[1]}"] += duration
                if not supervisor:
                    continue
                if kind == "assistant.message" and not data.get("toolRequests"):
                    final, resume = at, None
                elif kind in {"system.notification", "user.message"}:
                    notification = data.get("kind")
                    if isinstance(notification, dict):
                        notification = notification.get("type")
                    if kind == "system.notification" and notification in {
                        "instruction_discovered",
                        "instruction_updated",
                    }:
                        continue
                    # Agent and scheduled follow-ups are not owner messages.
                    who = (
                        "owner"
                        if kind == "user.message"
                        and data.get("source") in (None, "user")
                        and not data.get("agentTaskId")
                        else "notification"
                    )
                    if final:
                        resume = (at, who)
                elif kind == "assistant.turn_start" and final:
                    if resume is not None:
                        end, who = resume
                        if end - final >= idle_seconds and lo <= end < hi:
                            metrics[f"supervisor_idle_hours_{who}"] += (
                                end - final
                            ) / 3600
                            metrics[f"supervisor_idle_gaps_{who}"] += 1
                    final, resume = 0, None
    return dict(metrics)


def sessions(project, lo, hi, supervisors=()):
    home = Path(project.get("efficiency", "copilot_dir", "~/.copilot")).expanduser()
    supervisor_ids = set(project.get("efficiency", "supervisor_sessions", [])) | set(
        supervisors
    )
    aliases = {
        project.repo.casefold(),
        *map(str.casefold, project.get("efficiency", "repository_aliases", [])),
    }
    notes = []
    try:
        uri = (home.resolve() / "session-store.db").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute(
                "SELECT id, repository, cwd FROM sessions "
                "WHERE substr(created_at,1,10) <= ?",
                (iso(hi)[:10],),
            ).fetchall()
        paths = []
        for session_id, repo, cwd in rows:
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id):
                continue
            if (
                session_id in supervisor_ids
                or (repo or "").casefold() in aliases
                or cwd == str(project.checkout)
            ):
                path = home / "session-state" / session_id / "events.jsonl"
                if path.is_file() and path.stat().st_mtime >= lo:
                    paths.append((session_id, path))
        available_supervisors = supervisor_ids & {session_id for session_id, _ in paths}
        metrics = (
            session_metrics(
                paths,
                available_supervisors,
                lo,
                hi,
                60 * project.get("efficiency", "idle_gap_minutes", 30),
            )
            if paths
            else {}
        )
        notes.append(
            f"Local Copilot event files: {len(paths)}; supervisor files: {len(available_supervisors)}."
        )
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
        metrics = {}
        notes.append("Local Copilot session evidence unavailable.")
    if not any(key.startswith("supervisor_idle_") for key in metrics):
        notes.append(
            "Supervisor idle gaps unavailable without supervisor session evidence."
        )
    return metrics, notes
