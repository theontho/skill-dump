"""Collect live work on every host (agent-coord leases; heavy processes on the MacBook) into state.json."""

import fcntl
import json
import re
import subprocess
import time
from pathlib import Path

from coord import derive_machines
from host_activity import update_idle
from runtime import P, load, save

HERE = P.data

LEASE = re.compile(
    r"^\s+(L\d+)\s+(held|waiting)\s+([\d.]+)"
    r"(?: cores|-core cap(?:, [\d.]+ used)?)\s+([\d.]+G)\s+(\d+) min\s+(.*)$"
)


def run(cmd, timeout=40):
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def leases(prefix):
    cmd = ["agent-coord", "status", "--json"] if prefix is None else [
        *prefix, "agent-coord status --json"
    ]
    out = run(cmd, timeout=30)
    try:
        snapshot = json.loads(out)
    except ValueError:
        snapshot = None
    if isinstance(snapshot, dict) and isinstance(snapshot.get("leases"), list):
        now = time.time()
        rows = [
            {
                "id": x["id"], "state": state, "cores": x["cores"],
                "mem": f"{x.get('claimed_mem', x['mem']) / 1024**3:.1f}G",
                "minutes": max(0, round((now - x.get("granted", x.get("asked", now))) / 60)),
                "label": f"{x['agent']}: {x['label']}",
                "unit": x.get("unit") or x.get("run_unit"),
                "detached": bool(x.get("run_unit")),
                "restarting": "missing_since" in x,
                "warnings": list(x.get("exclusive_warnings", {}).values()),
            }
            for key, state in (("leases", "held"), ("queue", "waiting"))
            for x in snapshot.get(key, [])
        ]
        busy = snapshot.get("busy_cores")
        return {"leases": rows, "summary": f"cores busy now (measured): {busy:g}"
                if busy is not None else "", "reachable": True}
    # Old hosts may not accept --json. If they returned text anyway, use it;
    # otherwise retry the legacy command with only a short probe.
    if not out:
        cmd = ["agent-coord", "status"] if prefix is None else [*prefix, "agent-coord status"]
        out = run(cmd, timeout=5)
    rows, summary = [], ""
    for line in out.splitlines():
        if "cores busy now" in line:
            summary = line.strip()
        m = LEASE.match(line)
        if m:
            label = re.sub(r"\s*\(pid \d+\).*$", "", m.group(6)).strip()
            label = re.sub(r"\s*--\s*[\d.]+ of \d+ cores free$", "", label)
            rows.append(
                {
                    "id": m.group(1),
                    "state": m.group(2),
                    "cores": float(m.group(3)),
                    "mem": m.group(4),
                    "minutes": int(m.group(5)),
                    "label": label,
                }
            )
    return {"leases": rows, "summary": summary, "reachable": bool(out)}


def process_probe(host, probe, *, timeout=40):
    alias = P.hosts[host].get("process_probe_ssh")
    ssh = [*P.ssh(host)[:-1], alias] if alias and P.ssh(host) else P.ssh(host)
    out = run([*ssh, probe] if ssh else ["sh", "-c", probe], timeout=timeout)
    rows = []
    lines = out.splitlines()
    for line in lines[:-1]:
        parts = line.split(None, 2)
        if len(parts) == 3:
            try:
                cpu = float(parts[0])
            except ValueError:
                continue
            if cpu >= 50:
                cmd = parts[2]
                tag = re.search(
                    r"scratch/(certify-[0-9a-f]{9}|rt-[\w-]+|wt-[\w-]+)", cmd
                )
                rows.append(
                    {
                        "state": "running",
                        "cores": round(cpu / 100, 1),
                        "minutes": parts[1],
                        "label": (tag.group(1) + ": " if tag else "")
                        + Path(cmd.split()[0]).name,
                    }
                )
    load = lines[-1].strip() if lines else ""
    return {
        "leases": rows[:8],
        "summary": f"load {load}" if load else "",
        "reachable": bool(out),
    }


def main():
    hosts = {}
    for key, host in P.hosts.items():
        if host.get("agent_coord"):
            hosts[key] = leases(P.ssh(key) or None)
            if not hosts[key]["reachable"] and host.get("process_probe"):
                hosts[key] = process_probe(key, host["process_probe"], timeout=5)
                hosts[key]["activity_known"] = False
        elif host.get("process_probe"):
            hosts[key] = process_probe(key, host["process_probe"])
        else:
            hosts[key] = {"reachable": P.reachable(key)}
    with (HERE / "state.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = HERE / "state.json"
        state = load(path, {"prs": [], "machines": [], "log": []})
        state["hosts"] = hosts
        now = time.time()
        state["hosts_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        derive_machines(state, P.hosts, update_idle(state, P, observed_at=now))
        save(path, state)


if __name__ == "__main__":
    main()
