"""Current CI routing, with short descriptions for dashboard readers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from runtime import P
from tests_inventory import ROOT

NIGHTLY = set(P.get("tests", "nightly_workflows", []))
RELEASE = set(P.get("tests", "release_workflows", []))
STAGES = {
    "ready": ("Ready PR", "Validate the current PR head."),
    "queue": ("Merge queue", "Validate the proposed merge against the current base."),
    "main": ("Push to base", "Checks run after changes reach the base branch."),
    "nightly": ("Nightly backstop", "Run broader checks on a schedule."),
    "release": (
        "Release",
        "Validate distributable artifacts and release-critical checks.",
    ),
    "scheduled": ("Scheduled", "Recurring checks and maintenance."),
    "local": ("Local certification", "Record evidence from configured hosts."),
    "other": ("Other", "Checks outside the recurring pipeline."),
}
STAGES.update(P.get("tests", "stage_descriptions", {}))
DETAILS = P.get("tests", "stage_details", {})

WORKFLOW_PURPOSE = P.get("tests", "workflow_purpose", {})
JOB_PURPOSE = P.get("tests", "job_purpose", {})


def workflow_map() -> dict[str, dict[str, Any]]:
    result = {}
    for file in sorted(
        ROOT.glob(P.get("tests", "workflow_glob", ".github/workflows/*.y*ml"))
    ):
        text = file.read_text()
        name = next(
            (
                line[6:].strip()
                for line in text.splitlines()
                if line.startswith("name: ")
            ),
            file.name,
        )
        triggers = section(text, "on")
        jobs = section(text, "jobs")
        result[file.name] = {
            "name": name,
            "triggers": re.findall(r"^  ([\w-]+):", triggers, re.M),
            "jobs": re.findall(r"^  ([\w-]+):", jobs, re.M),
            "cron": re.findall(r'^\s+- cron: ["\']?([^"\'\n#]+)', triggers, re.M),
            "purpose": WORKFLOW_PURPOSE.get(file.stem, ""),
        }
    return result


def section(text: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*\n((?:[ \t#].*\n|\n)*)", text, re.M)
    return match[1] if match else ""


def stage(workflow: str, event: str, branch: str) -> str:
    stages = P.get("tests", "event_stages", {})
    if event in stages:
        return stages[event]
    if event == "merge_group":
        return "queue"
    if event in {"pull_request", "pull_request_target"}:
        return "ready"
    if workflow in P.get("tests", "release_always", []) or (
        workflow in RELEASE
        and event == "workflow_dispatch"
        and re.fullmatch(
            P.get("tests", "release_branch_pattern", r"v\d+\.\d+\.\d+(?:[-+][\w.-]+)?"),
            branch,
        )
    ):
        return "release"
    if event == "schedule":
        return "nightly" if workflow in NIGHTLY else "scheduled"
    if event == "push" and branch == P.base:
        return "main"
    return "other"


def stage_workflows(policy: dict[str, dict[str, Any]], key: str) -> list[str]:
    def matches(file: str, triggers: list[str]) -> bool:
        explicit = P.get("tests", "stage_workflows", {}).get(key)
        if explicit is not None:
            return file in explicit
        if key in P.get("tests", "event_stages", {}).values():
            return any(
                P.get("tests", "event_stages", {}).get(trigger) == key
                for trigger in triggers
            )
        if key == "release":
            return file in RELEASE
        if key == "ready":
            return "pull_request" in triggers or "pull_request_target" in triggers
        if key == "queue":
            return "merge_group" in triggers
        if key == "main":
            return "push" in triggers and file not in RELEASE | NIGHTLY
        if key == "nightly":
            return file in NIGHTLY
        if key == "scheduled":
            return "schedule" in triggers and file not in NIGHTLY
        return key == "other" and "workflow_dispatch" in triggers

    return [file for file, spec in policy.items() if matches(file, spec["triggers"])]


def family(name: str) -> str:
    if name == "required (draft)":
        return "required"
    if " / " in name:
        parts = name.split(" / ")
        if re.fullmatch(r"all|native|core(?:-\d+)?|integration|shard-\d+", parts[-1]):
            return " / ".join(parts[:-1])
    return re.sub(r" \([^()]*(?:, |\bx64\b|\barm64\b|\blatest\b)[^()]*\)$", "", name)


def job_purpose(workflow: str, name: str) -> str:
    last = name.rsplit(" / ", 1)[-1].lower()
    if last in JOB_PURPOSE:
        return JOB_PURPOSE[last]
    if any(
        key in last
        for key in ("test-ubuntu", "test-windows", "test-macos", "native-arm64")
    ):
        return "Runs the CLI tests on the named operating system; shards share the workload."
    if last == "gitleaks":
        return WORKFLOW_PURPOSE.get("gitleaks", "Scans for leaked secrets.")
    if last == "optional-tools":
        return WORKFLOW_PURPOSE.get("optional_tools", "Checks optional tools.")
    return WORKFLOW_PURPOSE.get(
        Path(workflow).stem, "Runs validation described in the workflow definition."
    )
