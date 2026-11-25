"""Project configuration, command scope and shared data primitives."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

CODE = Path(__file__).resolve().parent
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
REPO_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def home_path(variable, fallback):
    return Path(os.environ.get(variable) or Path.home() / fallback).expanduser()


def local_path(value):
    return Path(value).expanduser().resolve()


class Project:
    def __init__(
        self,
        name="default",
        *,
        config_file=None,
        data_dir=None,
        required=False,
        read_config=True,
    ):
        if not SLUG.fullmatch(name):
            raise ValueError(f"invalid project name: {name!r}")
        self.name = name
        self.config_file = local_path(
            config_file
            or home_path("XDG_CONFIG_HOME", ".config")
            / "pr-coordinator"
            / f"{name}.toml"
        )
        self.settings = {}
        if read_config:
            try:
                with self.config_file.open("rb") as stream:
                    self.settings = tomllib.load(stream)
            except FileNotFoundError:
                if required or config_file:
                    raise
        self.repo = self.get("github", "repo", "example/project")
        if not isinstance(self.repo, str) or not REPO_SLUG.fullmatch(self.repo):
            raise ValueError("github.repo must be owner/name")
        self.repo_owner, self.repo_name = self.repo.split("/")
        self.owner = self.get("github", "owner", "")
        if required and (
            not isinstance(self.owner, str)
            or not self.owner
            or "repo" not in self.settings.get("github", {})
        ):
            raise ValueError("github.repo and github.owner are required")
        self.authors = set(self.get("github", "tracked_authors", [])) | {self.owner}
        self.base = self.get("github", "base_branch", "main")
        self.remote = self.get("github", "remote", "origin")
        self.web = f"https://github.com/{self.repo}"
        self.checkout = local_path(self.get("paths", "checkout", Path.cwd()))
        self.worktrees = local_path(
            self.get("paths", "worktree_root", self.checkout.parent)
        )
        self.data = local_path(
            data_dir
            or self.get(
                "paths",
                "data_dir",
                home_path("XDG_STATE_HOME", ".local/state") / "pr-coordinator" / name,
            )
        )
        self.hosts = self.settings.get("hosts", {})
        for key, host in self.hosts.items():
            if not SLUG.fullmatch(key) or not isinstance(host, dict):
                raise ValueError(f"invalid host entry: {key!r}")
            for field in ("ssh", "process_probe_ssh"):
                if field in host and (
                    not isinstance(host[field], str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", host[field])
                ):
                    raise ValueError(f"invalid {field} alias for {key}")
        if required and (
            not self.base or self.base.startswith("-") or self.remote.startswith("-")
        ):
            raise ValueError("invalid github.base_branch or github.remote")
        self.generated = re.compile(
            "|".join(f"(?:{r})" for r in self.get("lane", "generated", [])) or r"(?!)"
        )
        self.priority = self.get("labels", "priority", "priority")
        self.hands_off = self.get("labels", "hands_off", "hands-off")
        self.land_asap = self.get("labels", "land_asap", "land-asap")
        self.cert_prefix = self.get("certification", "status_prefix", "local-ci/")

    def get(self, section, key, default=None):
        return self.settings.get(section, {}).get(key, default)

    @property
    def base_ref(self):
        return f"{self.remote}/{self.base}"

    def command(self, section, key, *, default=(), **values):
        argv = self.get(section, key, default)
        return self.expand(argv, label=f"{section}.{key}", **values)

    def expand(self, argv, *, label="command", **values):
        if isinstance(argv, str) or not all(isinstance(arg, str) for arg in argv):
            raise ValueError(f"{label} must be an array of strings")
        values = {**vars(self), **values}
        return [
            re.sub(r"\{([a-z_]+)\}", lambda m: str(values.get(m[1], m[0])), arg)
            for arg in argv
        ]

    def worktree(self, pr, kind="train"):
        key = "cert_pattern" if kind == "cert" else "worktree_pattern"
        fallback = "wt-cert{pr}" if kind == "cert" else "wt-pr{pr}"
        return self.worktrees / self.get("paths", key, fallback).format(pr=int(pr))

    def ssh(self, key):
        alias = self.hosts[key].get("ssh")
        return (
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias]
            if alias
            else []
        )

    def prepare(self):
        self.data.mkdir(parents=True, mode=0o700, exist_ok=True)
        (self.data / "locks").mkdir(mode=0o700, exist_ok=True)

    def graphql(self, selection):
        return (
            "{repository(owner:"
            + json.dumps(self.repo_owner)
            + ",name:"
            + json.dumps(self.repo_name)
            + "){"
            + selection
            + "}}"
        )


P = Project(read_config=False)


def gh_argv(*args):
    if args and args[0] in {"pr", "run", "workflow"}:
        return ["gh", *args, "--repo", P.repo]
    return ["gh", *args]


def gh(*args, **kwargs):
    return subprocess.run(gh_argv(*args), **kwargs)


def cli(*args):
    return [
        sys.executable,
        str(CODE / "pr-coord"),
        "--project",
        P.name,
        "--config",
        str(P.config_file),
        "--data-dir",
        str(P.data),
        *map(str, args),
    ]


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_text(path, data):
    tmp = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        with os.fdopen(
            os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
            "w",
            encoding="utf-8",
        ) as file:
            file.write(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def save(path, data):
    atomic_text(path, json.dumps(data, indent=1))


@contextmanager
def locked(name, *, wait=True):
    with (P.data / "locks" / name).open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield


def questions(update, *, agent=False):
    name = "agent_questions.json" if agent else "questions.json"
    with locked(name + ".lock"):
        path = P.data / name
        data = update(load(path, []))
        save(path, data)
        return data
