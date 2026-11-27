"""Review evidence and ready-router plans for exact-head certification."""

import json
import re
import subprocess

from landing import json_gh, reviewed
from runtime import P, load


def review_state(pr, head, row):
    """Return whether review is settled and whether another request is appropriate."""
    if row["headRefOid"] != head:
        return False, False
    pattern = P.get(
        "drafts",
        "reviewer_pattern",
        r"(?i)^(?:app/)?copilot(?:-pull-request-reviewer)?(?:\[bot\])?$",
    )
    reviews = json_gh(
        "api", f"repos/{P.repo}/pulls/{pr}/reviews", "--paginate", "--slurp"
    )
    exact = [
        entry for page in reviews for entry in page if entry.get("commit_id") == head
    ]
    accepted = any(
        entry.get("state") in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}
        and (user := entry.get("user"))
        and user.get("type") == "Bot"
        and re.fullmatch(pattern, user["login"])
        for entry in exact
    )
    requests = row.get("reviewRequests", {})
    pending = (
        requests.get("nodes")
        or requests.get("pageInfo", {}).get("hasNextPage")
        or any(entry.get("state") == "PENDING" for entry in exact)
    )
    return bool(accepted and not pending and reviewed(row)), not (accepted or pending)


def statuses(head):
    pages = json_gh(
        "api", f"repos/{P.repo}/commits/{head}/statuses", "--paginate", "--slurp"
    )
    latest = {}
    for page in pages:
        for row in page:
            if row["context"].startswith(P.cert_prefix):
                latest.setdefault(row["context"], row["state"])
    return latest


def routed():
    return bool(P.get("certification", "ready_router"))


def planned_path(pr, head, base):
    return P.data / "cert-plans" / f"{pr}-{head}-{base}.json"


def plan_outcome(pr, head, base):
    return load(planned_path(pr, head, base), {}).get("outcome")


def router_layers(pr, head, base):
    """Run a trusted project router against the pinned PR and base commits."""
    if not all(re.fullmatch(r"[a-f0-9]{40}", oid) for oid in (head, base)):
        raise ValueError("certification router needs full base and head commit IDs")
    subprocess.run(
        ["git", "fetch", "-q", "--no-tags", P.remote, P.base, f"pull/{pr}/head"],
        cwd=P.checkout,
        check=True,
        timeout=120,
    )
    command = P.command("certification", "ready_router", pr=pr, head=head, base=base)
    output = subprocess.run(
        command, cwd=P.checkout, capture_output=True, text=True, check=True, timeout=90
    ).stdout
    if P.get("certification", "ready_router_format", "json") == "actions":
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if fields.get("routed") != "true" or fields.get("cli") not in {"true", "false"}:
            raise ValueError("ready router did not return valid route flags")
        # Hosted smoke/optional workflows in a policy-only diff are not local lanes.
        layers = json.loads(fields["required_layers"])
        if fields["cli"] == "false" or json.loads(fields["move_modules"]):
            layers = ["generic"]
    else:
        plan = json.loads(output)
        if plan["head"] != head or plan["base"] != base:
            raise ValueError(
                "ready router returned a plan for a different head or base"
            )
        layers = plan["layers"]
    if (
        not isinstance(layers, list)
        or "generic" not in layers
        or not all(
            isinstance(layer, str) and re.fullmatch(r"[a-z0-9-]+", layer)
            for layer in layers
        )
    ):
        raise ValueError("ready router returned invalid certification layers")
    return set(layers)


def routed_targets(layers, slots):
    """Map attestations to aliases and slots without substituting native platforms."""
    mapping = P.get("certification", "layers", {})
    return {
        target: {
            host: layer
            for host in hosts
            if (
                layer := P.hosts[host].get("platform")
                if mapping.get(target) == "platform"
                else mapping.get(target, target)
            )
            in layers
        }
        for target, hosts in slots.items()
    }
