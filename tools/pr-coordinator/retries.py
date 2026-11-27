"""Persistent, per-project limit on repeating a failed action unchanged."""

import hashlib
import json
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime

from runtime import atomic_text, locked, save, uuid4


class RetryLimit(RuntimeError):
    pass


ACTION_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    subprocess.SubprocessError,
)


def error_class(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exit-{exc.returncode}"
    return type(exc).__name__


class Retry:
    def __init__(self, data, command, **inputs):
        self.data = data
        self.command = command
        fingerprint = json.dumps([command, inputs], sort_keys=True, default=str)
        self.inputs = {
            key: inputs[key]
            for key in ("pr", "head", "target", "host")
            if key in inputs
        }
        self.key = hashlib.sha256(fingerprint.encode()).hexdigest()
        self.path = data / "retries" / f"{self.key}.json"

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def check(self):
        # The next error class is unknowable before running; stop if any class
        # has already failed twice for this action and these inputs.
        for kind, logs in self._read().items():
            if len(logs) >= 2:
                raise RetryLimit(
                    f"{self.command}: third identical attempt refused ({kind}); "
                    f"change the inputs or approach. Previous logs: {', '.join(logs[:2])}"
                )

    def previous(self, kind):
        return self._read().get(kind, [])

    def failed(self, kind, detail):
        self.path.parent.mkdir(exist_ok=True, mode=0o700)
        log_dir = self.data / "logs"
        log_dir.mkdir(exist_ok=True, mode=0o700)
        with locked(f"retry-{self.key}.lock", data=self.data):
            failures = self._read()
            logs = failures.setdefault(kind, [])
            if len(logs) < 2:
                log = log_dir / f"retry-{self.command}-{uuid4().hex[:12]}.log"
                inputs = json.dumps(self.inputs, sort_keys=True, default=str)
                atomic_text(
                    log,
                    f"{datetime.now(UTC).isoformat()} {self.command} {inputs}\n"
                    f"Input fingerprint: {self.key}\n"
                    f"{kind}: {detail}\n",
                )
                logs.append(str(log))
                save(self.path, failures)
            return logs[-1]

    @contextmanager
    def observe(self, step=""):
        self.check()
        try:
            yield
        except RetryLimit:
            raise
        except ACTION_ERRORS as exc:
            kind = error_class(exc)
            self.failed(f"{step}-{kind}" if step else kind, exc)
            raise
