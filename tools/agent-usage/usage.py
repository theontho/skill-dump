"""Counts-only aggregation, public-rate estimates, and merged-PR denominators."""

import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

COUNTS = (
    "calls",
    "session_records",
    "prompt_uncached",
    "prompt_cached",
    "cache_write",
    "output",
    "reasoning",
)


def canonical_model(model):
    model = re.sub(r"(claude-(?:opus|sonnet|haiku)-\d+)-(\d+)", r"\1.\2", model)
    return re.sub(r"-\d{8}$", "", model)


def estimate(record, rates):
    model = canonical_model(record["model"])
    rate = rates["models"].get(model)
    if rate is None:
        return None
    base, cached, write, output = rate
    prompt = record["prompt_uncached"] + record["prompt_cached"]
    if model.startswith("gpt-") and record["calls"] and prompt > 272_000:
        base *= 2
        cached *= 2
        write *= 2
        output *= 1.5
    elif model == "claude-haiku-5.5" and record["calls"] and prompt > 100_000:
        base *= 5
        cached *= 5
        write *= 5
        output *= 5
    ttl = record["cache_ttl_seconds"]
    if model.startswith("gpt-") and ttl > 1800:
        write *= 2
    elif model.startswith("claude-") and ttl > 300:
        write *= 1.6
    return (
        (record["prompt_uncached"] - record["cache_write"]) * base
        + record["prompt_cached"] * cached
        + record["cache_write"] * write
        + record["output"] * output
    ) / 1_000_000


def summarize(rows):
    totals = {key: sum(row.get(key, 0) for row in rows) for key in COUNTS}
    sizes: Counter[str] = Counter()
    for row in rows:
        sizes.update(row.get("prompt_histogram", {}))
    count = sum(sizes.values())
    totals["prompt_mean"] = (
        sum(int(size) * n for size, n in sizes.items()) / count if count else None
    )
    rank = math.ceil(count * 0.9)
    cumulative = 0
    totals["prompt_p90"] = None
    for size, n in sorted(sizes.items(), key=lambda item: int(item[0])):
        cumulative += n
        if cumulative >= rank:
            totals["prompt_p90"] = int(size)
            break
    totals["estimated_usd_known"] = sum(
        row.get("estimated_usd_known", 0) for row in rows
    )
    totals["unpriced_records"] = sum(row.get("unpriced_records", 0) for row in rows)
    totals["estimated_usd"] = (
        None if totals["unpriced_records"] else totals["estimated_usd_known"]
    )
    totals["prompt_histogram"] = dict(sizes)
    return totals


def aggregate(records, start, end, zone, rates):
    groups = defaultdict(list)
    for record in records:
        if not record["timestamp"]:
            continue
        stamp = datetime.fromisoformat(record["timestamp"])
        if stamp.tzinfo is None:
            raise ValueError("usage record timestamp has no timezone")
        local_day = stamp.astimezone(zone).date()
        if not start <= local_day <= end:
            continue
        key = (
            local_day.isoformat(),
            record["repository"],
            record["source"],
            record["agent"],
            record["role"],
            canonical_model(record["model"]),
        )
        groups[key].append(record)
    days = defaultdict(list)
    for (day, repo, source, agent, role, model), items in sorted(groups.items()):
        row = {
            "repository": repo,
            "source": source,
            "agent": agent,
            "role": role,
            "model": model,
        }
        row.update({key: sum(item[key] for item in items) for key in COUNTS})
        row["prompt_histogram"] = dict(
            Counter(
                str(item["prompt_uncached"] + item["prompt_cached"])
                for item in items
                if item["calls"]
            )
        )
        costs = [estimate(item, rates) for item in items]
        row["unpriced_records"] = sum(cost is None for cost in costs)
        row["estimated_usd_known"] = sum(cost for cost in costs if cost is not None)
        row.update(summarize([row]))
        days[day].append(row)
    return days


def gh(*arguments):
    return subprocess.check_output(["gh", *arguments], text=True, timeout=120)


def merged_counts(repo, start, end, zone):
    # Search in UTC with a one-day margin, then bucket mergedAt in the same zone
    # as usage. Fetch metadata only, never titles, bodies, or comments.
    search = (
        f"repo:{repo} is:pr is:merged "
        f"merged:{start - timedelta(days=1)}..{end + timedelta(days=1)}"
    )
    query = """query($search:String!,$cursor:String) {
      search(query:$search,type:ISSUE,first:100,after:$cursor) {
        issueCount pageInfo { hasNextPage endCursor }
        nodes { ... on PullRequest { mergedAt } }
      }
    }"""
    counts: Counter[str] = Counter()
    cursor = None
    while True:
        args = ["api", "graphql", "-f", f"query={query}", "-f", f"search={search}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        payload = json.loads(gh(*args))
        if payload.get("errors"):
            raise ValueError("GitHub merged-PR query failed")
        result = payload["data"]["search"]
        if result["issueCount"] > 1000:
            raise ValueError("merged-PR search exceeds GitHub's 1000-result limit")
        for node in result["nodes"]:
            if node and node.get("mergedAt"):
                day = datetime.fromisoformat(node["mergedAt"]).astimezone(zone).date()
                if start <= day <= end:
                    counts[day.isoformat()] += 1
        if not result["pageInfo"]["hasNextPage"]:
            break
        cursor = result["pageInfo"]["endCursor"]
    return counts


def document(day, machine, zone, rows, repositories, merges, rates):
    repos = {}
    for repo in repositories:
        summary = summarize([row for row in rows if row["repository"] == repo])
        merged = merges[repo].get(day, 0) if merges[repo] is not None else None
        total = (
            summary["prompt_uncached"] + summary["prompt_cached"] + summary["output"]
        )
        summary["merged_prs"] = merged
        summary["tokens_per_merged_pr"] = total / merged if merged else None
        repos[repo] = summary
    return {
        "schema_version": 1,
        "day": day,
        "machine": machine,
        "timezone": str(zone),
        "collected_at": datetime.now(UTC).isoformat(),
        "rates_as_of": rates["as_of"],
        "repositories": repos,
        "agents": rows,
        "limitations": [
            "Polling share omitted: message/tool bodies are never decoded.",
            "Supervisor means root session; implementer means a child call/session, not job intent.",
            "OpenCode session_v2 contains lifetime totals bucketed on creation day, not calls.",
            "Prompt mean/p90 excludes OpenCode session aggregates.",
            "USD is a public standard API-rate estimate, not subscription cost or Copilot credits.",
        ],
    }
