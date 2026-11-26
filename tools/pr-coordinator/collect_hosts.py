"""Collect live work on every host (agent-coord leases; heavy processes on the MacBook) into state.json."""

import fcntl
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
    cmd = (
        ["agent-coord", "status"] if prefix is None else [*prefix, "agent-coord status"]
    )
    out = run(cmd)
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


def process_probe(host, probe):
    alias = P.hosts[host].get("process_probe_ssh")
    ssh = [*P.ssh(host)[:-1], alias] if alias and P.ssh(host) else P.ssh(host)
    out = run([*ssh, probe] if ssh else ["sh", "-c", probe])
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
