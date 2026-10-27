#!/usr/bin/env python3
"""Private-terminal sudo authentication shared with fresh agent shells."""

import argparse
import array
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import select
import shlex
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "sudo-session"
ADDRESS = RUNTIME / "control.sock"
SUDO = "/usr/bin/sudo"
TMUX = ["tmux", "-L", "sudo-session", "-f", "/dev/null"]
DURATION = 3600

HELP = """\
sudo-session - share a time-limited sudo credential across agent shells

USAGE
  sudo-session start [--ssh] [--duration DURATION]
  sudo-session extend --duration DURATION
  sudo-session status
  sudo-session run [--] COMMAND [ARG ...]
  sudo-session exec [--] COMMAND [ARG ...]
  sudo-session stop
  sudo-session help [start|extend]
  sudo-session --help

COMMANDS
  start     Authenticate directly with sudo in a private local or SSH terminal.
            Default lifetime: 1h. --duration accepts positive whole numbers
            with s (seconds), m (minutes), h (hours), or d (days).
            No suffix means seconds. Examples: 90s, 30m, 12h, 1d.
            --ssh is an optional marker; both modes need a real terminal.
            An existing active session is left unchanged, not extended.
  extend    Give an active session at least DURATION remaining from now.
            Example: extend --duration 12h. Never shortens a longer session.
            Uses the active sudo credential, logs the change, and cannot
            revive a stopped or expired session.
  status    Show whether the session is active and how much time remains.
  run       Run a command through noninteractive sudo on the authenticated
            terminal. Arguments, working directory, stdin, stdout, stderr,
            and command exit status are preserved.
  exec      Alias for run. "sudo-session COMMAND ARG ..." is also supported;
            use run -- if the command name conflicts with a helper command.
  stop      Revoke the session immediately. Already-running commands are
            allowed to finish; new commands are rejected.
  help      Show this help. "start --help" and "extend --help" show options.

EXAMPLES
  sudo-session start                          # authenticate for one hour
  sudo-session start --duration 12h            # authenticate for twelve hours
  ssh -t USER@HOST sudo-session start --ssh --duration 12h
  sudo-session status
  sudo-session extend --duration 12h           # at least 12 hours from now
  sudo-session run -- id -u
  sudo-session run -- systemctl restart YOUR_SERVICE
  sudo-session stop

CHANGING THE LIFETIME
  Active session: sudo-session extend --duration 12h
  Stopped session: sudo-session start --duration 12h in a private terminal.
  To shorten a lifetime, stop the session and authenticate again.
  Starting again does not reset or extend an existing deadline.

TERMINAL AND PASSWORD SAFETY
  Enter the password only at sudo's private terminal prompt, never in agent
  chat, arguments, or files. Once active, detach with Ctrl-b, then d.
  The dedicated tmux terminal keeps running after SSH disconnects.
  To reattach: tmux -L sudo-session attach -t auth
  Ctrl-c in that terminal ends the session; detach instead to keep it active.

SCOPE AND EXPIRY
  This does not unlock bare sudo in unrelated shells or add a passwordless
  sudoers rule. Use sudo-session run for privileged commands.
  Normal sudo policy and auditing apply. Other processes running as your
  user can also use this session. Longer lifetimes increase that exposure.
  Credentials are refreshed while active, then invalidated on stop/expiry.
  Reboot or loss of the authentication terminal ends the session.
  Session audit log: journalctl -t sudo-session

EXIT STATUS
  status: 0 when active, 1 when stopped. stop is safe when already stopped.
  run/exec: the command's exit status; helper errors are printed to stderr.
"""


def parse_duration(value):
    match = re.fullmatch(r"([0-9]+)([smhd]?)", value)
    if not match:
        raise argparse.ArgumentTypeError("Use a positive whole-number duration, such as 30m or 12h")
    try:
        seconds = int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    except ValueError:
        raise argparse.ArgumentTypeError("Duration exceeds the system limit") from None
    if not 0 < seconds <= sys.maxsize:
        raise argparse.ArgumentTypeError("Duration must be positive and within the system limit")
    return seconds


def format_duration(seconds):
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, seconds = divmod(int(seconds), size)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts) or "0s"


def start_parser(prog="sudo-session start"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Authenticate in a private terminal, then detach with Ctrl-b, d.",
        epilog=(
            "Examples: sudo-session start --duration 12h; "
            "ssh -t USER@HOST sudo-session start --ssh --duration 12h. "
            "An existing session is not extended: use sudo-session extend "
            "--duration 12h, or stop it and authenticate again to shorten its "
            "lifetime. Never enter the password in agent chat."
        ),
    )
    parser.add_argument("--ssh", action="store_true", help="mark an SSH-terminal login (no desktop needed)")
    parser.add_argument(
        "--duration", type=parse_duration, default=DURATION, metavar="DURATION",
        help="session lifetime, e.g. 90s, 30m, 12h, 1d; default: 1h; bare numbers mean seconds",
    )
    return parser


def extend_parser():
    parser = argparse.ArgumentParser(
        prog="sudo-session extend",
        description="Give an active session at least the requested time remaining from now.",
        epilog=(
            "Example: sudo-session extend --duration 12h. Does not shorten a "
            "longer session or revive an expired one. Uses the active sudo "
            "credential; no new password prompt. The change is audited."
        ),
    )
    parser.add_argument(
        "--duration", type=parse_duration, required=True, metavar="DURATION",
        help="minimum remaining lifetime, e.g. 30m, 12h, 1d; bare numbers mean seconds",
    )
    return parser


def audit(message):
    subprocess.run(["logger", "-t", "sudo-session", "--", message], check=True)


def prepare_runtime():
    RUNTIME.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = RUNTIME.lstat()
    if RUNTIME.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"Unsafe runtime directory: {RUNTIME}")


def request(payload, descriptors=()):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.connect(str(ADDRESS))
        ancillary = []
        if descriptors:
            ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors))]
        data = json.dumps(payload).encode()
        if len(data) > 65536:
            raise RuntimeError("Command request exceeds 64 KiB")
        connection.sendmsg([data], ancillary)
        reply = connection.recv(65536)
        if not reply:
            raise RuntimeError("Credential session disconnected before returning a result")
        return json.loads(reply)


def respond(connection, payload):
    try:
        connection.send(json.dumps(payload).encode())
    except (BrokenPipeError, ConnectionResetError):
        audit("Client disconnected before receiving command result")
    finally:
        connection.close()


def remove_legacy_grant():
    unit = f"sudo-session-expire-{os.getuid()}.timer"
    timer = subprocess.run(["systemctl", "is-active", "--quiet", unit])
    if timer.returncode not in (0, 3, 4):
        raise RuntimeError("Unable to check for a legacy sudo grant")
    if timer.returncode == 0:
        grant = f"/etc/sudoers.d/90-sudo-session-{os.getuid()}"
        subprocess.run([SUDO, "-n", "--", "/usr/bin/rm", "-f", grant], check=True)
        subprocess.run([SUDO, "-k"], check=True)
        audit("Removed legacy passwordless grant; requiring fresh terminal authentication")
        print("Removed the old passwordless grant. Authenticate again below.", flush=True)
        if subprocess.run([SUDO, "-v"]).returncode:
            raise RuntimeError("Sudo authentication failed after removing the legacy grant")


def serve(duration=None):
    if duration is None:
        duration = DURATION
    prepare_runtime()
    with (RUNTIME / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A credential session is already running") from None
        ADDRESS.unlink(missing_ok=True)
        # Password entry happens only on this private, controlling terminal.
        if subprocess.run([SUDO, "-v"]).returncode:
            raise RuntimeError("Sudo authentication failed")
        remove_legacy_grant()
        deadline = time.monotonic() + duration
        refresh = time.monotonic() + 30
        jobs = []
        active = True
        server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            server.bind(str(ADDRESS))
            os.chmod(ADDRESS, 0o600)
            server.listen(16)
            lifetime = format_duration(duration)
            audit(f"Started {lifetime} authenticated terminal session ({duration} seconds)")
            print(f"Sudo session active for {lifetime}. Detach with Ctrl-b, then d.", flush=True)
            while active or jobs:
                now = time.monotonic()
                if active and now >= refresh:
                    active = subprocess.run([SUDO, "-n", "-v"]).returncode == 0
                    refresh = now + 30
                if active and now >= deadline:
                    active = False
                if not active and server.fileno() != -1:
                    server.close()
                    ADDRESS.unlink(missing_ok=True)
                    subprocess.run([SUDO, "-k"], check=True)
                    audit("Revoked credential session; already-running commands may finish")
                for process, connection in jobs[:]:
                    code = process.poll()
                    if code is not None:
                        respond(connection, {"code": code})
                        jobs.remove((process, connection))
                if not active:
                    time.sleep(0.1)
                    continue
                if not select.select([server], [], [], 0.1)[0]:
                    continue
                connection, _ = server.accept()
                connection.settimeout(2)
                descriptors = array.array("i")
                try:
                    data, ancillary, flags, _ = connection.recvmsg(
                        65536, socket.CMSG_SPACE(3 * descriptors.itemsize)
                    )
                    for level, kind, value in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            descriptors.frombytes(value[:len(value) - len(value) % descriptors.itemsize])
                    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                        raise ValueError("Truncated command request")
                    payload = json.loads(data)
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid command request")
                    action = payload.get("action")
                    if time.monotonic() >= deadline:
                        active = False
                        respond(connection, {"code": 1, "error": "Credential session expired"})
                    elif action == "status":
                        active = subprocess.run([SUDO, "-n", "-v"]).returncode == 0
                        respond(connection, {
                            "code": 0 if active else 1,
                            "remaining": max(0, int(deadline - time.monotonic())),
                        })
                    elif action == "stop":
                        subprocess.run([SUDO, "-k"], check=True)
                        active = False
                        respond(connection, {"code": 0})
                    elif action == "extend":
                        duration = payload.get("duration")
                        if type(duration) is not int or not 0 < duration <= sys.maxsize:
                            raise ValueError("Invalid extension duration")
                        active = subprocess.run([SUDO, "-n", "-v"]).returncode == 0
                        if not active:
                            respond(connection, {"code": 1, "error": "Sudo credential is no longer valid"})
                        elif time.monotonic() >= deadline:
                            active = False
                            respond(connection, {"code": 1, "error": "Credential session expired"})
                        else:
                            deadline = max(deadline, time.monotonic() + duration)
                            remaining = max(0, int(deadline - time.monotonic()))
                            audit(f"Extended authenticated terminal session: {remaining} seconds remaining")
                            respond(connection, {"code": 0, "remaining": remaining})
                    elif action == "exec":
                        args = payload.get("args")
                        cwd = payload.get("cwd")
                        if (not isinstance(args, list) or not args
                                or not all(isinstance(arg, str) and "\0" not in arg for arg in args)
                                or not isinstance(cwd, str) or len(descriptors) != 3):
                            raise ValueError("Invalid command arguments or standard streams")
                        process = subprocess.Popen(
                            [SUDO, "-n", "--", *args], cwd=cwd,
                            stdin=descriptors[0], stdout=descriptors[1], stderr=descriptors[2],
                        )
                        jobs.append((process, connection))
                    else:
                        raise ValueError("Unknown session action")
                except (ValueError, OSError) as error:
                    respond(connection, {"code": 1, "error": str(error)})
                finally:
                    for descriptor in descriptors:
                        os.close(descriptor)
        finally:
            server.close()
            ADDRESS.unlink(missing_ok=True)
            subprocess.run([SUDO, "-k"], check=True)
            audit("Closed authenticated terminal session")


def start(duration=DURATION):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError(
            "Start from a private SSH terminal:\n"
            f"  ssh -t {getpass.getuser()}"
            f"@{socket.gethostname()} sudo-session start --ssh --duration {duration}s\n"
            "Never send the password through agent chat."
        )
    prepare_runtime()
    try:
        result = request({"action": "status"})
    except (FileNotFoundError, ConnectionRefusedError):
        result = {"code": 1}
    if result["code"] == 0:
        print(
            f"Sudo session already active ({format_duration(result['remaining'])} remaining).\n"
            "Existing session unchanged. To extend it, run:\n"
            f"  sudo-session extend --duration {duration}s\n"
            "To shorten it, stop the session and authenticate again."
        )
        return 0
    command = shlex.join([sys.executable, str(ROOT), "_serve", "--duration", f"{duration}s"])
    return subprocess.call([*TMUX, "new-session", "-A", "-s", "auth", command])


def main():
    args = sys.argv[1:]
    action = args.pop(0) if args else "help"
    if action == "help" and args in (["start"], ["extend"]):
        (start_parser() if args[0] == "start" else extend_parser()).print_help()
        return 0
    if action == "_serve":
        options = start_parser("sudo-session _serve").parse_args(args)
        serve(options.duration)
        return 0
    if action == "start":
        options = start_parser().parse_args(args)
        return start(options.duration)
    if action == "extend":
        options = extend_parser().parse_args(args)
        try:
            reply = request({"action": "extend", "duration": options.duration})
        except (FileNotFoundError, ConnectionRefusedError):
            raise RuntimeError(
                "No active sudo session to extend; authenticate in a private terminal with "
                f"sudo-session start --duration {options.duration}s"
            ) from None
        if reply.get("error") == "Unknown session action":
            raise RuntimeError(
                "The running session predates extension support. Once, run sudo-session stop,\n"
                "then authenticate in a private terminal with "
                f"sudo-session start --duration {options.duration}s.\n"
                "New sessions support sudo-session extend."
            )
        if reply.get("error"):
            raise RuntimeError(reply["error"])
        print(f"active ({format_duration(reply['remaining'])} remaining)")
        return reply["code"]
    if action in ("status", "stop"):
        if args:
            raise RuntimeError(f"Usage: sudo-session {action}")
        try:
            reply = request({"action": action})
        except (FileNotFoundError, ConnectionRefusedError):
            print("stopped")
            return 1 if action == "status" else 0
        if reply.get("error"):
            raise RuntimeError(reply["error"])
        if action == "status":
            print(f"active ({format_duration(reply['remaining'])} remaining)" if reply["code"] == 0 else "stopped")
        else:
            print("sudo session stopped")
        return reply["code"]
    if action in ("help", "--help", "-h"):
        if args:
            raise RuntimeError("Usage: sudo-session help [start|extend]")
        print(HELP, end="")
        return 0
    if action not in ("run", "exec"):
        args.insert(0, action)
    elif args and args[0] == "--":
        args.pop(0)
    if not args:
        raise RuntimeError("No command specified")
    try:
        reply = request(
            {"action": "exec", "args": args, "cwd": os.getcwd()},
            [sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()],
        )
    except (FileNotFoundError, ConnectionRefusedError):
        raise RuntimeError("No active sudo session; authenticate over SSH with sudo-session start --ssh") from None
    if reply.get("error"):
        raise RuntimeError(reply["error"])
    return reply["code"] if reply["code"] >= 0 else 128 - reply["code"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as error:
        print(f"sudo-session: {error}", file=sys.stderr)
        sys.exit(1)
