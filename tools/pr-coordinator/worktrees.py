"""Prepare a local branch checkout without replacing local commits."""

import subprocess
from pathlib import Path

from runtime import P


def git(wt, *args):
    return subprocess.run(
        ["git", "-C", str(wt), *args],
        capture_output=True,
        check=True,
        text=True,
        timeout=180,
    ).stdout.strip("\n")


def prepare(pr, info, kind):
    sha, branch = info["headRefOid"], info["headRefName"]
    remote_ref = f"refs/remotes/{P.remote}/{branch}"
    branch_ref = f"refs/heads/{branch}"
    git(P.checkout, "fetch", "-q", P.remote, f"+{branch_ref}:{remote_ref}")
    if git(P.checkout, "rev-parse", remote_ref) != sha:
        raise ValueError("published head changed while fetching; retry")
    destination = P.worktree(pr, kind)
    for entry in git(P.checkout, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = entry.split("\0")
        if fields[0].startswith("worktree ") and f"branch {branch_ref}" in fields:
            wt = Path(fields[0].removeprefix("worktree ")).resolve()
            break
    else:
        wt = destination
        if wt.exists() or wt.is_symlink():
            raise ValueError(f"worktree destination already exists: {wt}")
        if git(P.checkout, "branch", "--list", branch):
            git(P.checkout, "worktree", "add", "-q", str(wt), branch)
        else:
            git(
                P.checkout,
                "worktree",
                "add",
                "-q",
                "--track",
                "-b",
                branch,
                str(wt),
                remote_ref,
            )
    if wt == P.checkout or git(wt, "status", "--porcelain"):
        raise ValueError(f"refusing to use primary checkout or dirty worktree: {wt}")
    if git(wt, "rev-parse", "HEAD") != sha:
        if kind == "cert" and wt != destination:
            raise ValueError(f"existing worktree is not at the published head: {wt}")
        git(wt, "merge", "--ff-only", sha)
        if git(wt, "rev-parse", "HEAD") != sha:
            raise ValueError(f"worktree contains unpushed commits: {wt}")
    return wt
