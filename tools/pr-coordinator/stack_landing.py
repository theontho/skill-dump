"""Read native stack membership and track exact-head asynchronous landing."""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from runtime import P, gh, locked, save

SHA = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})\Z")
UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
POLL_SECONDS = 300


class StackError(RuntimeError):
    pass


def api(path, *args, timeout=30):
    try:
        endpoint = f"repos/{P.repo}" + (f"/{path}" if path else "")
        result = gh(
            "api",
            "--hostname",
            "github.com",
            "--include",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            *args,
            endpoint,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        headers, body = result.stdout.replace("\r\n", "\n").split("\n\n", 1)
        status = int(headers.splitlines()[0].split()[1])
        headers = {
            key.casefold(): value.strip()
            for line in headers.splitlines()[1:]
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
        # An HTTP error still has a useful body (notably a pending operation on 409).
        if result.returncode and status < 400:
            raise ValueError(result.stderr.strip())
        try:
            body = json.loads(body)
        except ValueError:
            if status < 400:
                raise
            body = {"message": result.stderr.strip() or f"HTTP {status}"}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        diagnostic = result.stderr.strip() if "result" in locals() else str(exc)
        raise StackError(f"{path}: {diagnostic or 'invalid HTTP response'}") from exc
    return status, headers, body


def message(body):
    if not isinstance(body, dict):
        return "unexpected response"
    details = body.get("details")
    return (details.get("message") if isinstance(details, dict) else None) or (
        body.get("message") or "unexpected response"
    )


def read(path):
    status, _, body = api(path)
    if status != 200:
        raise StackError(f"{path}: HTTP {status}: {message(body)}")
    return body


def number(value):
    if type(value) is not int or value < 1:
        raise StackError("invalid PR or stack number")
    return value


def membership_row(pr):
    data = read(f"stacks?pull_request={number(pr)}")
    if data == []:
        return None
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        number(data[0].get("number"))
        return data[0]
    raise StackError(f"PR #{pr}: invalid stack membership response")


def membership(pr):
    """Return a stack number or None only after a successful, empty probe."""
    row = membership_row(pr)
    return row["number"] if row is not None else None


@dataclass(frozen=True)
class Stack:
    number: int
    base: str
    members: tuple
    position: int

    @property
    def prefix(self):
        return self.members[: self.position]

    @property
    def upper(self):
        return [row["number"] for row in self.members[self.position :]]


def parse_stack(data, stack, pr):
    try:
        base = data["base"]["ref"]
        rows = data["pull_requests"]
        ids = [number(row["number"]) for row in rows]
        if (
            data["number"] != stack
            or not isinstance(base, str)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError
        return Stack(stack, base, tuple(rows), ids.index(pr) + 1)
    except (KeyError, TypeError, ValueError) as exc:
        raise StackError(f"invalid stack #{stack} detail for PR #{pr}") from exc


def detail(stack, pr):
    return parse_stack(read(f"stacks/{number(stack)}"), stack, pr)


def lookup(pr):
    stack = membership(pr)
    return detail(stack, pr) if stack is not None else None


def prefix_problem(stack):
    # A train entry authorizes this PR only. Do not infer consent for downstack PRs.
    lower = [row["number"] for row in stack.prefix[:-1] if not row.get("merged_at")]
    if lower:
        return "land the lower stack members first: " + ", ".join(
            f"#{n}" for n in lower
        )
    return None


def retry_after(headers):
    value = headers.get("retry-after", "")
    if value.isascii() and value.isdigit() and len(value) < 20:
        return float(value)
    try:
        return max(0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return 0


def merge(pr, head, stack, action, method, *, preflight):
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise StackError("invalid head SHA for stack merge")
    endpoint = f"pulls/{number(pr)}/merge-async"
    journal = P.data / "logs"
    journal.mkdir(mode=0o700, exist_ok=True)
    path = journal / f"stack-merge-{pr}-{head}.json"
    expected = {
        "pr": pr,
        "stack": stack.number,
        "head": head,
        "action": action,
        "method": method,
        "prefix": [row["number"] for row in stack.prefix],
    }

    def record(state, text, uuid=None, not_before=0):
        save(
            path,
            {
                **expected,
                "status": state,
                "message": text,
                "uuid": uuid,
                "upper": stack.upper,
                "not_before": not_before,
            },
        )

    def operation(body, *, conflict=False):
        fields = body.get("details") if isinstance(body, dict) else None
        if not isinstance(fields, dict):
            fields = body if conflict and isinstance(body, dict) else {}
        uuid = fields.get("uuid")
        if (
            not isinstance(uuid, str)
            or not UUID.fullmatch(uuid)
            or fields.get("merge_action") != action
            or fields.get("expected_head_sha") != head
            or fields.get("bypass_rules", False) is not False
            or (method is not None and fields.get("merge_method") != method)
        ):
            identity = (
                f" {uuid}" if isinstance(uuid, str) and UUID.fullmatch(uuid) else ""
            )
            raise StackError(
                f"pending operation{identity} does not match the authorized merge: {message(body)}"
            )
        return uuid

    with locked(f"stack-merge-{pr}.lock", data=P.data):
        delay = 1
        uuid = None
        try:
            if path.exists():
                previous = json.loads(path.read_text())
                if any(previous.get(k) != v for k, v in expected.items()):
                    raise StackError(
                        "existing operation record does not match this landing"
                    )
                state = previous["status"]
                uuid = previous.get("uuid")
                if state in ("enqueued", "merged"):
                    return state, path
                if (
                    state != "pending"
                    or not isinstance(uuid, str)
                    or not UUID.fullmatch(uuid)
                ):
                    raise StackError(
                        f"previous operation {state}: {previous['message']}"
                    )
                # Poll the recorded operation; never repeat an indeterminate PUT.
                not_before = previous.get("not_before", 0)
            else:
                preflight()
                record("submitting", "awaiting the merge-async response")
                fields = ["-f", f"sha={head}", "-f", f"merge_action={action}"]
                if method is not None:
                    fields += ["-f", f"merge_method={method}"]
                code, headers, body = api(endpoint, "--method", "PUT", *fields)
                state = body.get("status") if isinstance(body, dict) else None
                if code == 200 and state in ("enqueued", "merged"):
                    record(state, message(body))
                    return state, path
                if (code, state) not in (
                    (202, "pending"),
                    (409, "pending"),
                    (409, None),
                ):
                    record("failed", f"HTTP {code}: {message(body)}")
                    raise StackError(f"HTTP {code}: {message(body)}")
                try:
                    uuid = operation(body, conflict=code == 409)
                except StackError as exc:
                    record("unverified", str(exc))
                    raise
                not_before = time.time() + retry_after(headers)
                record("pending", message(body), uuid, not_before)
            deadline = time.monotonic() + POLL_SECONDS
            while True:
                wait = max(delay, not_before - time.time())
                remaining = deadline - time.monotonic()
                if wait >= remaining:
                    raise StackError("timed out waiting for merge-async")
                time.sleep(wait)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StackError("timed out waiting for merge-async")
                code, headers, body = api(
                    f"{endpoint}/{uuid}", timeout=min(30, remaining)
                )
                delay = min(delay * 2, 15)
                not_before = time.time() + retry_after(headers)
                if code in {429, 502, 503, 504} or (
                    code == 403 and "retry-after" in headers
                ):
                    record("pending", message(body), uuid, not_before)
                    continue
                if code != 200 or not isinstance(body, dict):
                    raise StackError(f"HTTP {code}: {message(body)}")
                state = body.get("status")
                if state == "pending" and operation(body) == uuid:
                    record("pending", message(body), uuid, not_before)
                    continue
                if state not in ("enqueued", "merged", "failed"):
                    raise StackError(f"unexpected operation result: {message(body)}")
                record(state, message(body), uuid)
                if state == "failed":
                    raise StackError(message(body))
                return state, path
        except StackError as exc:
            identity = f" operation {uuid};" if uuid else ""
            raise StackError(f"{exc};{identity} record: {path}") from exc
