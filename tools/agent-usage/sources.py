"""Local counts-only readers. No source text or identifiers are persisted."""

import hashlib
import math
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from metadata import project

USAGE_FIELDS: dict[str, object] = dict.fromkeys(
    (
        "inputTokens",
        "outputTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
        "reasoningTokens",
        "cost",
        "model",
        "parentToolCallId",
        "cacheTtlSeconds",
    ),
    True,
)
USAGE_FIELDS["accounting"] = {
    "sourceSessionId": True,
    "sequence": True,
    "usageId": True,
}
COPILOT_FIELDS = {
    "type": True,
    "timestamp": True,
    "id": True,
    "data": {"usage": USAGE_FIELDS},
}
CLAUDE_FIELDS = {
    "type": True,
    "timestamp": True,
    "cwd": True,
    "isSidechain": True,
    "requestId": True,
    "message": {
        "id": True,
        "model": True,
        "usage": dict.fromkeys(
            (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ),
            True,
        ),
    },
}


def number(value):
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return int(value)
    return 0


def agent_key(source, identifier):
    return hashlib.sha256(f"{source}:{identifier}".encode()).hexdigest()[:16]


def repository(config, directory="", name=""):
    if name:
        return next(
            (
                repo["name"]
                for repo in config["repositories"]
                if name.casefold().removesuffix(".git") == repo["name"].casefold()
            ),
            None,
        )
    for repo in config["repositories"]:
        for root in repo.get("paths", []):
            root = str(Path(root).expanduser()).rstrip("/")
            if directory == root or directory.startswith(root + "/"):
                return repo["name"]
    return None


def workspace(path):
    result = {}
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            match = re.match(r"^(cwd|git_root|repository):\s*(.*?)\s*$", line)
            if match:
                result[match[1]] = match[2].strip("'\"")
    return result


def record(
    source,
    identifier,
    repo,
    stamp,
    model,
    role,
    uncached,
    cached,
    writes,
    output,
    reasoning=0,
    calls=1,
    ttl=300,
):
    return {
        "source": source,
        "agent": agent_key(source, identifier),
        "repository": repo,
        "timestamp": stamp,
        "model": model or "unknown",
        "role": role,
        "calls": calls,
        "session_records": int(source == "opencode"),
        "prompt_uncached": uncached,
        "prompt_cached": cached,
        "cache_write": writes,
        "output": output,
        "reasoning": reasoning,
        "cache_ttl_seconds": ttl,
    }


def copilot(config):
    seen = set()
    for path in sorted(Path(config["copilot"]).expanduser().glob("*/events.jsonl")):
        ws = workspace(path.parent / "workspace.yaml")
        repo = repository(config, ws.get("cwd", ""), ws.get("repository", ""))
        if not repo:
            continue
        with path.open(errors="replace") as stream:
            for line in stream:
                if '"session.usage_record"' not in line:
                    continue
                try:
                    event = project(line, COPILOT_FIELDS)
                except ValueError:
                    continue  # An actively written final line may be incomplete.
                if event.get("type") != "session.usage_record":
                    continue
                usage = event.get("data", {}).get("usage", {})
                accounting = usage.get("accounting", {})
                identity = accounting.get("usageId") or event.get("id")
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                cached = number(usage.get("cacheReadTokens"))
                total = number(usage.get("inputTokens"))
                child = bool(usage.get("parentToolCallId"))
                source_id = accounting.get("sourceSessionId") or path.parent.name
                yield record(
                    "copilot",
                    source_id,
                    repo,
                    event.get("timestamp"),
                    usage.get("model"),
                    "implementer" if child else "supervisor",
                    max(0, total - cached),
                    cached,
                    min(number(usage.get("cacheWriteTokens")), max(0, total - cached)),
                    number(usage.get("outputTokens")),
                    number(usage.get("reasoningTokens")),
                    ttl=number(usage.get("cacheTtlSeconds")) or 300,
                )


def opencode(config, state):
    source = Path(config["opencode"]).expanduser()
    if not source.exists():
        return
    # Copy the database and WAL, never open the live database with SQLite.
    # The private snapshot is deleted even if the schema is unsupported.
    with tempfile.TemporaryDirectory(prefix=".opencode-", dir=state) as directory:
        snapshot = Path(directory) / "usage.db"
        originals = [source, Path(str(source) + "-wal")]

        def versions():
            result: list[tuple[int, int, int] | None] = []
            for path in originals:
                try:
                    stat = path.stat()
                    result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
                except FileNotFoundError:
                    result.append(None)
            return result

        for _attempt in range(5):
            before = versions()
            try:
                for original, suffix in zip(originals, ("", "-wal"), strict=True):
                    target = Path(str(snapshot) + suffix)
                    if original.exists():
                        shutil.copyfile(original, target)
                    else:
                        target.unlink(missing_ok=True)
            except FileNotFoundError:
                continue  # WAL rotated while taking the copy.
            if before == versions():
                break
        else:
            raise ValueError(
                "OpenCode database changed during snapshot; retry collection"
            )
        with closing(sqlite3.connect(snapshot)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(session_v2)")
            }
            if not columns:
                raise ValueError("OpenCode has no session_v2 usage table")
            parent = "parent_id" if "parent_id" in columns else "NULL"
            query = (
                "SELECT id, directory, time_created, model, tokens_input, tokens_output, "
                "tokens_reasoning, tokens_cache_read, tokens_cache_write, cost, "
                f"{parent} FROM session_v2"
            )
            for row in connection.execute(query):
                (
                    sid,
                    directory,
                    stamp,
                    model,
                    tin,
                    tout,
                    reasoning,
                    cached,
                    writes,
                    _,
                    parent,
                ) = row
                repo = repository(config, directory or "")
                if not repo:
                    continue
                if model and model.startswith("{"):
                    model = project(model, {"id": True}).get("id")
                stamp = datetime.fromtimestamp(stamp / 1000).astimezone().isoformat()
                yield record(
                    "opencode",
                    sid,
                    repo,
                    stamp,
                    model,
                    "implementer" if parent else "supervisor",
                    number(tin) + number(writes),
                    number(cached),
                    number(writes),
                    number(tout),
                    number(reasoning),
                    calls=0,
                )


def claude(config):
    seen = set()
    root = Path(config["claude"]).expanduser()
    for path in sorted(root.glob("**/*.jsonl")):
        with path.open(errors="replace") as stream:
            for line in stream:
                if '"usage"' not in line:
                    continue
                try:
                    event = project(line, CLAUDE_FIELDS)
                except ValueError:
                    continue
                if event.get("type") != "assistant":
                    continue
                message = event.get("message", {})
                usage = message.get("usage", {})
                if not usage:
                    continue
                repo = repository(config, event.get("cwd", ""))
                if not repo:
                    continue
                identity = (event.get("requestId"), message.get("id"))
                if any(identity):
                    if identity in seen:
                        continue
                    seen.add(identity)
                writes = number(usage.get("cache_creation_input_tokens"))
                yield record(
                    "claude",
                    path.stem,
                    repo,
                    event.get("timestamp"),
                    message.get("model"),
                    "implementer"
                    if event.get("isSidechain") or "subagents" in path.parts
                    else "supervisor",
                    number(usage.get("input_tokens")) + writes,
                    number(usage.get("cache_read_input_tokens")),
                    writes,
                    number(usage.get("output_tokens")),
                )
