"""Publish weekly system-efficiency evidence for a configured PR project."""

import argparse
import json
import math
import re
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path

from efficiency_github import GitHub, iso
from efficiency_local import certification, leases, sessions
from runtime import P, atomic_text, load, locked

DAY = 86400
WEEK = 7 * DAY
SCHEMA = 1
LABELS = {
    "merged_prs": "Merged PRs",
    "review_rounds_p50": "Copilot review rounds per merged PR (p50)",
    "review_rounds_p90": "Copilot review rounds per merged PR (p90)",
    "review_prs_at_least_4": "Merged PRs with ≥4 Copilot rounds",
    "pushes_after_ready": "Observed pushes after first ready",
    "superseded_hosted_runs": "Superseded hosted runs",
    "superseded_certifications": "Superseded certifications",
    "cert_attempts": "Certification log attempts",
    "cert_busy_refusals": "Certification busy/no-host refusals",
    "lease_queue_hours": "Lease queue hours",
    "lease_queue_hours_started": "Lease queue hours (started)",
    "lease_queue_hours_cancelled": "Lease queue hours (cancelled)",
    "rerun_only_passes": "Rerun-only passes on merged heads",
    "supervisor_idle_gaps_owner": "Supervisor idle gaps ended by owner",
    "supervisor_idle_hours_owner": "Supervisor idle hours ended by owner",
    "supervisor_idle_gaps_notification": "Supervisor idle gaps ended by notification",
    "supervisor_idle_hours_notification": "Supervisor idle hours ended by notification",
    "polling_hours": "Model-attached polling hours",
    "blocking_question_hours": "Blocking question hours",
}
SESSION_FIELD = re.compile(
    r"(?:polling_hours(?:\.[a-z_]+(?:\.[a-z_]+)?)?|blocking_question_hours|"
    r"supervisor_idle_(?:gaps|hours)_(?:owner|notification))"
)


def bounds(value):
    if not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", value):
        raise ValueError("week must use YYYY-Www (ISO week)")
    year, week = value.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    return int(datetime(monday.year, monday.month, monday.day, tzinfo=UTC).timestamp())


def week_of(value):
    year, week, _ = datetime.fromtimestamp(value, UTC).isocalendar()
    return f"{year:04d}-W{week:02d}"


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def imported_sessions(path, week):
    if not path:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["weeks"].get(week, {})
    if not isinstance(rows, dict) or any(
        not SESSION_FIELD.fullmatch(key) or not numeric(value)
        for key, value in rows.items()
    ):
        raise ValueError(f"invalid session metrics for {week}")
    return rows


def inputs(project, github, lo, hi, override, supervisors):
    notes = []
    logdir = project.data / "logs"
    markers = set(project.get("certification", "busy_markers", [])) | {
        "No available certification host",
        " is held by ",
    }
    certs, local, legacy = certification(logdir, lo, hi, markers)
    if not logdir.exists():
        notes.append("Certification logs unavailable.")
        local = {}
    if legacy:
        notes.append(
            f"{legacy} legacy certification exits dated by their file's last pre-upgrade modification time."
        )
    missing = github.missing_attempts
    gh_metrics = github.collect(lo, hi, certs)
    if not logdir.exists():
        gh_metrics = gh_metrics | {"superseded_certifications": None}
    if github.missing_attempts > missing:
        notes.append(
            f"{github.missing_attempts - missing} earlier Actions attempts were missing; rerun-only passes are a lower bound."
        )
    queue, queue_notes = leases(project, lo, hi)
    copilot, session_notes = sessions(project, lo, hi, supervisors)
    notes.extend(queue_notes)
    notes.extend(session_notes)
    if override:
        notes.append("Supplied session metrics override matching local metrics.")
    return gh_metrics | local | queue | copilot | override, notes


def report_data(project, week, lo, hi, metrics, notes, previous, prior_notes, targets):
    names = dict.fromkeys(
        [*LABELS, *sorted(metrics.keys() | previous.keys() | targets.keys())]
    )
    return {
        "schema": SCHEMA,
        "project": project.name,
        "repository": project.repo,
        "week": week,
        "start_utc": iso(lo),
        "end_utc_exclusive": iso(hi),
        "partial_week": hi < lo + WEEK,
        "previous_week": week_of(lo - WEEK),
        "metrics": {
            k: {
                "value": value,
                "previous": prior,
                "delta": round(value - prior, 6)
                if value is not None and prior is not None
                else None,
                "target_max": targets.get(k),
            }
            for k in names
            for value, prior in [(metrics.get(k), previous.get(k))]
        },
        "coverage_notes": notes,
        "previous_coverage_notes": prior_notes,
    }


def format_value(value):
    return "—" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")


def table_rows(report):
    for key, row in report["metrics"].items():
        yield (
            LABELS.get(key, f"Polling hours: {key.removeprefix('polling_hours.')}"),
            *(
                format_value(row[col])
                for col in ("value", "previous", "delta", "target_max")
            ),
        )


def dashboard_page(data):
    prefix = (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<title>System efficiency</title><nav><a href="/">PR backlog</a></nav>'
        "<h1>System efficiency</h1>"
    )
    for file in sorted((data / "efficiency").glob("????-W??.json"), reverse=True):
        report = load(file, {})
        if report.get("schema") != SCHEMA:
            continue
        rows = "".join(
            "<tr>"
            + f'<th scope="row">{escape(label)}</th>'
            + "".join(f"<td>{escape(cell)}</td>" for cell in cells)
            + "</tr>"
            for label, *cells in table_rows(report)
        )
        return (
            prefix
            + f"<p>{escape(report['week'])} vs. {escape(report['previous_week'])}"
            + (" (current week is partial)" if report["partial_week"] else "")
            + "</p><table><thead><tr><th scope='col'>Metric</th>"
            "<th scope='col'>This week</th><th scope='col'>Previous</th>"
            "<th scope='col'>Change</th><th scope='col'>Max target</th></tr></thead>"
            f"<tbody>{rows}</tbody></table><h2>Coverage</h2>"
            "<p>— means unavailable or no eligible sample.</p><ul>"
            + "".join(f"<li>{escape(note)}</li>" for note in report["coverage_notes"])
            + "</ul></html>"
        )
    return prefix + "<p>No weekly efficiency report has been published.</p></html>"


def markdown(report):
    result = [
        f"# {report['project']} system efficiency · {report['week']}",
        "",
        f"UTC window: {report['start_utc']} to {report['end_utc_exclusive']} (exclusive).",
        f"Comparison: {report['previous_week']} (full week).",
    ]
    if report["partial_week"]:
        result.append("This week is in progress; totals are not directly comparable.")
    result += [
        "",
        "| Metric | This week | Previous | Change | Max target |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    result.extend("| " + " | ".join(row) + " |" for row in table_rows(report))
    result += ["", "## Coverage", "", "— means unavailable or no eligible sample."]
    result.extend(f"- {note}" for note in report["coverage_notes"])
    result.extend(
        f"- Previous week: {note}" for note in report["previous_coverage_notes"]
    )
    return "\n".join(result) + "\n"


def publish(
    project, week, output_dir=None, session_file=None, supervisors=(), *, now=None
):
    now = int(time.time()) if now is None else now
    lo = bounds(week)
    hi = min(now + 1, lo + WEEK)
    if hi <= lo:
        raise ValueError("cannot report a future week")
    targets = project.get("efficiency", "targets", {})
    if any(
        (key not in LABELS and not SESSION_FIELD.fullmatch(key)) or not numeric(value)
        for key, value in targets.items()
    ):
        raise ValueError(
            "efficiency.targets must use report metric names and nonnegative numbers"
        )
    session_files = session_file or project.get("efficiency", "session_metrics_file")
    session_file = Path(session_files).expanduser() if session_files else None
    directory = project.data / "efficiency"
    github = GitHub(project)
    metrics, notes = inputs(
        project, github, lo, hi, imported_sessions(session_file, week), supervisors
    )
    prior_week = week_of(lo - WEEK)
    cached = load(directory / f"{prior_week}.json", {})
    if (
        cached.get("schema") == SCHEMA
        and cached.get("repository") == project.repo
        and cached.get("project") == project.name
        and cached.get("week") == prior_week
        and not cached["partial_week"]
        and not session_file
        and not supervisors
    ):
        previous = {key: row["value"] for key, row in cached["metrics"].items()}
        prior_notes = cached["coverage_notes"]
    else:
        previous, prior_notes = inputs(
            project,
            github,
            lo - WEEK,
            lo,
            imported_sessions(session_file, prior_week),
            supervisors,
        )
    info = report_data(
        project,
        week,
        lo,
        hi,
        metrics,
        notes,
        previous,
        prior_notes,
        targets,
    )
    destinations = {directory}
    if configured := project.get("efficiency", "repo_report_dir"):
        dest = (project.checkout / configured).resolve()
        if not dest.is_relative_to(project.checkout) or dest == project.checkout:
            raise ValueError(
                "efficiency.repo_report_dir must be inside the project checkout"
            )
        destinations.add(dest)
    if output_dir:
        destinations.add(output_dir.expanduser().resolve())
    for dest in sorted(destinations):
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_text(dest / f"{week}.md", markdown(info))
        atomic_text(
            dest / f"{week}.json", json.dumps(info, indent=2, allow_nan=False) + "\n"
        )
        print(dest / f"{week}.md", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--week", help="ISO week YYYY-Www (default: current UTC week)")
    when.add_argument(
        "--weekly",
        action="store_true",
        help="publish last complete week when due; retry hourly",
    )
    parser.add_argument("--output-dir", type=Path, help="also write a copy here")
    parser.add_argument(
        "--session-metrics",
        type=Path,
        help="override local session aggregates from JSON",
    )
    parser.add_argument(
        "--supervisor-session",
        action="append",
        default=[],
        help="also classify idle gaps for this local Copilot session ID (repeatable)",
    )
    args = parser.parse_args()
    while True:
        now = int(time.time())
        week = args.week or week_of(now - (WEEK if args.weekly else 0))
        try:
            with locked("efficiency-report.lock", wait=False):
                existing = load(P.data / "efficiency" / f"{week}.json", {})
                if not args.weekly or existing.get("partial_week", True):
                    publish(
                        P,
                        week,
                        args.output_dir,
                        args.session_metrics,
                        args.supervisor_session,
                        now=now,
                    )
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            if not args.weekly:
                raise
            print(f"weekly efficiency report: {exc}", file=sys.stderr, flush=True)
        if not args.weekly:
            return 0
        time.sleep(3600)
