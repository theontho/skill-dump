"""Launch configured implementers: tmux on POSIX hosts, WMI-detached wrappers on Windows."""

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from remote import record, remote
from runtime import SLUG, P

START = r"""
set -eu
root=$1 name=$2 session=$3
if [ -n "$session" ]; then
    tmux has-session -t "=$session" ||
        { echo "required tmux environment session is not running" >&2; exit 1; }
    if tmux list-windows -t "=$session" -F '#{window_name}' | grep -Fxq "agent-$name"; then
        echo "agent window already exists" >&2; exit 1
    fi
elif tmux has-session -t "=agent-$name" 2>/dev/null; then
    echo "agent session already exists" >&2; exit 1
fi
case "$root" in "~/"*) root=$HOME/${root#??} ;; esac
dir=$root/out/agents/$name
umask 077
mkdir -p "$dir"
python3 -c 'import json,sys; from pathlib import Path
dest=Path(sys.argv[1])
for name, text in json.load(sys.stdin).items():
    (dest / name).write_text(text)' "$dir"
command="$(python3 -c 'import shlex,sys; print(shlex.join(sys.argv[1:]))' bash "$dir/run.sh")"
if [ -n "$session" ]; then
    tmux new-window -d -t "=$session:" -n "agent-$name" "$command"
else
    tmux new-session -d -s "agent-$name" "$command"
fi
"""

ENV_SESSION = r"""
set -eu
session=$1
if tmux has-session -t "=$session" 2>/dev/null; then
    echo "environment session already running"
    exit
fi
IFS= read -r token
case "$token" in ""|*[!A-Za-z0-9_-]*) echo "invalid credential input" >&2; exit 1 ;; esac
tmux new-session -d -s "$session" 'while :; do sleep 86400; done'
# Use stdin rather than exposing the credential in tmux or SSH command arguments.
{
    printf 'set-environment -t %s GH_TOKEN %s\n' "$session" "$token"
    printf 'set-environment -t %s COPILOT_GITHUB_TOKEN %s\n' "$session" "$token"
} | tmux source-file -
echo "environment session started"
"""


def main(command):
    parser = argparse.ArgumentParser()
    parser.add_argument("host", choices=list(P.hosts))
    if command == "remote-agent":
        parser.add_argument("name", nargs="?")
        parser.add_argument("prompt", nargs="?", type=Path)
        windows = parser.add_argument_group("Windows hosts only")
        windows.add_argument(
            "--list", action="store_true", help="list agents and their state"
        )
        windows.add_argument(
            "--log", action="store_true", help="print the end of NAME's log"
        )
        windows.add_argument("--lines", type=int, help="lines for --log (40)")
        windows.add_argument(
            "--stop", action="store_true", help="stop NAME's process tree"
        )
    else:
        parser.add_argument(
            "--stop", action="store_true", help="Windows: stop the environment holder"
        )
    args = parser.parse_args()
    host = P.hosts[args.host]
    if P.ssh(args.host) and host.get("windows") and host.get("remote_agents"):
        from remote_agents_windows import main as windows_main

        return windows_main(command, args, parser)
    if not P.ssh(args.host) or host.get("windows") or not host.get("remote_agents"):
        parser.error("this host is not configured for remote agents")
    if (
        args.stop
        or getattr(args, "list", False)
        or getattr(args, "log", False)
        or getattr(args, "lines", None) is not None
    ):
        parser.error(
            "--list, --log, --lines and --stop are for Windows hosts; use tmux on POSIX"
        )
    if command == "remote-agent" and (not args.name or not args.prompt):
        parser.error("provide NAME and PROMPT_FILE")
    session = host.get("agent_env_session", "")
    if session and not re.fullmatch(r"[A-Za-z0-9_-]+", session):
        parser.error("invalid tmux environment session name")
    if command == "agent-env":
        if not session or sys.stdin.isatty():
            parser.error(
                "configure agent_env_session and provide credential input on stdin"
            )
        return remote(
            args.host, ["bash", "-c", ENV_SESSION, "agent-env", session], timeout=30
        ).returncode
    if not SLUG.fullmatch(args.name):
        parser.error("invalid agent name")
    argv = P.command("remote_agents", "command", host=args.host, name=args.name)
    reporter = P.command("remote_agents", "report", host=args.host, name=args.name)
    if not argv or not reporter or not host.get("checkout"):
        parser.error(
            "configure host checkout, remote_agents.command and remote_agents.report"
        )
    files = {"prompt.md": args.prompt.read_text(encoding="utf-8")}
    if runbook := P.get("remote_agents", "runbook"):
        if Path(runbook).name in {"prompt.md", "run.sh"}:
            parser.error("runbook filename conflicts with launch files")
        files[Path(runbook).name] = (
            Path(runbook).expanduser().read_text(encoding="utf-8")
        )
    report = shlex.join([*reporter, "agent", args.name, "done", "--task"])
    working = shlex.join(
        [
            *reporter,
            "agent",
            args.name,
            "working",
            "--machine",
            args.host,
            "--task",
            "started",
        ]
    )
    files["run.sh"] = f"""#!/bin/bash
{host.get("shell_prefix", "")}
dir="$(cd -- "$(dirname -- "$0")" && pwd)" || exit 1
cd "$dir/../../.." || exit 1
umask 077
{working} || true
{shlex.join(argv)} -p "$(cat "$dir/prompt.md")" > "$dir/output.log" 2>&1
rc=$?
printf 'EXIT %s\\n' "$rc" >> "$dir/output.log"
{report} "remote agent exited $rc; log $dir/output.log on $(hostname)" || true
exit "$rc"
"""
    record(
        "agent", args.name, "starting", "--machine", args.host, "--task", "launching"
    )
    try:
        remote(
            args.host,
            ["bash", "-c", START, "remote-agent", host["checkout"], args.name, session],
            input=json.dumps(files),
            text=True,
            timeout=60,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        record("agent", args.name, "error", "--task", "remote launch failed")
        raise
    print(f"started agent-{args.name} on {args.host}")
    return 0
