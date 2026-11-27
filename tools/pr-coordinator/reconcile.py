"""Reconcile stopped certification jobs with GitHub's published status."""

import subprocess

from landing import json_gh
from runtime import P, cli, load


def cert_pid(row, *, legacy=False):
    try:
        pid = int(row.get("pid", 0))
    except ValueError:
        return None
    if pid < 2:
        return None
    ps = subprocess.run(
        ["ps", "-p", str(pid), "-o", "args="],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ps.returncode:
        return None
    if (
        "pr-coord" in ps.stdout
        and any(command in ps.stdout for command in (" cert ", " cert-pr "))
        and (str(P.config_file) in ps.stdout or f"--project {P.name} " in ps.stdout)
    ):
        return pid
    if legacy and "cert.sh " in ps.stdout:
        return pid
    return None


def main():
    for row in load(P.data / "state.json", {}).get("certs", []):
        if row.get("status") not in {"queued", "running"} or cert_pid(row, legacy=True):
            continue
        sha = row.get("sha")
        if not sha:
            continue
        target = row["target"]
        layer = P.get("certification", "layers", {}).get(target, target)
        if layer == "platform":
            layer = P.hosts.get(row.get("host"), {}).get("platform")
        statuses = json_gh("api", f"repos/{P.repo}/commits/{sha}/statuses")
        verdict = next(
            (
                r["state"]
                for r in statuses
                if r["context"] == P.cert_prefix + str(layer)
            ),
            None,
        )
        status = {"success": "pass", "failure": "fail"}.get(verdict, "error")
        subprocess.run(
            cli(
                "cert-state",
                row["pr"],
                status,
                "--target",
                target,
                "--sha",
                sha,
                "--machine",
                row.get("host") or "",
            ),
            check=True,
            timeout=90,
        )
