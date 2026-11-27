"""Track idle episodes from the host collector and dashboard work records."""

from uuid import uuid4


def probe_window(project):
    # Allow for serial probes (up to 40 seconds each) and collector scheduling.
    return 40 * len(project.hosts) + 3 * project.get("loops", "collect_seconds", 60)


def busy_hosts(state, hosts):
    active = {
        row["machine"].strip().casefold()
        for row in state.get("agents", [])
        if row.get("machine") and row.get("status") in {"working", "waiting"}
    }
    running = {
        row.get("host")
        for kind in ("certs", "runs")
        for row in state.get(kind, [])
        if row.get("status") == "running"
    }
    live = state.get("hosts", {})
    return {
        key
        for key, spec in hosts.items()
        if key in running
        or any(
            alias.strip().casefold() in active
            for alias in (key, spec.get("display"), spec.get("ssh"))
            if alias
        )
        or any(
            row.get("state") in {"held", "running"} or row.get("detached")
            for row in live.get(key, {}).get("leases", [])
        )
    }


def update_idle(state, project, *, observed_at=None):
    """Called under the state lock. Only a fresh host probe can start the clock."""
    hosts = project.hosts
    busy = busy_hosts(state, hosts)
    idle = state.setdefault("host_idle", {})
    for key in list(idle):
        if key not in hosts or key in busy:
            del idle[key]
    if observed_at is not None:
        previous = state.get("hosts_observed_at")
        if previous is None or not 0 <= observed_at - previous <= probe_window(project):
            for episode in idle.values():
                episode.pop("since", None)
        state["hosts_observed_at"] = observed_at
        for key in hosts.keys() - busy:
            observation = state["hosts"].get(key, {})
            if observation.get("reachable") and observation.get("activity_known", True):
                episode = idle.setdefault(key, {})
                if "since" not in episode:
                    episode["since"] = observed_at
                    episode.setdefault("id", uuid4().hex)
            elif key in idle:
                # Losing contact breaks the timer, but does not re-arm an alert.
                idle[key].pop("since", None)
    return busy
