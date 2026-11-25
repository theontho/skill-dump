"""Remote test runs and multi-host local certification."""

import argparse
import base64
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from landing import assert_owner, json_gh
from runtime import P, cli, load, locked


def run(argv, *, cwd=None, check=True, **kwargs):
    return subprocess.run(argv, cwd=cwd or P.checkout, check=check, **kwargs)


def git(wt, *args):
    return run(
        ["git", "-C", str(wt), *args], stdout=subprocess.PIPE, text=True, timeout=180
    ).stdout.strip()


def record(*args):
    run(cli(*args), timeout=90)


def remote(host, argv, *, cwd=None, **kwargs):
    ssh = P.ssh(host)
    if not ssh:
        return run(argv, cwd=Path(cwd).expanduser() if cwd else None, **kwargs)
    if P.hosts[host].get("windows"):

        def ps(arg):
            return "'" + str(arg).replace("'", "''") + "'"

        script = "$ErrorActionPreference='Stop'; "
        if cwd:
            path = cwd.removeprefix("~/")
            directory = f"(Join-Path $HOME {ps(path)})" if path != cwd else ps(cwd)
            script += f"Set-Location -LiteralPath {directory}; "
        if argv[0] == "powershell-script":
            script += "& {\n" + argv[1] + "\n} " + " ".join(map(ps, argv[2:]))
        else:
            script += "& " + " ".join(map(ps, argv))
        script += "; exit $LASTEXITCODE"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        return run(
            [*ssh, "powershell", "-NoProfile", "-EncodedCommand", encoded], **kwargs
        )
    prefix = P.hosts[host].get("shell_prefix", "")
    directory = (
        '"$HOME"/' + shlex.quote(cwd[2:])
        if cwd and cwd.startswith("~/")
        else shlex.quote(cwd)
        if cwd
        else ""
    )
    command = (f"cd {directory} && " if directory else "") + shlex.join(argv)
    return run([*ssh, prefix + command], **kwargs)


def rt_main():
    parser = argparse.ArgumentParser(
        description="Run a test command against a POSIX host's synchronized worktree."
    )
    parser.add_argument("host", choices=list(P.hosts))
    parser.add_argument("worktree", type=Path)
    parser.add_argument(
        "--envs", help="override environments sent to the preparation hook"
    )
    raw = sys.argv[1:]
    split = raw.index("--") if "--" in raw else 2
    gate = raw[split + (split < len(raw) and raw[split] == "--") :]
    args = parser.parse_args(raw[:split])
    if not gate:
        parser.error("provide a test command after --")
    host, wt = args.host, args.worktree.expanduser().resolve()
    spec = P.hosts[host]
    if not spec.get("ssh") or spec.get("windows") or not spec.get("rt", False):
        parser.error("this host is not configured for POSIX remote testing")
    sha = git(wt, "rev-parse", "HEAD")
    root = spec["checkout"].rstrip("/")
    dest = f"{root}/{spec.get('rt_root', 'scratch')}/rt-{wt.name}"
    if any(c in wt.name for c in "\r\n/") or ":" in wt.name:
        parser.error("worktree name contains unsupported characters")
    envs = args.envs if args.envs is not None else P.get("remote_tests", "envs", "")
    args = dict(
        host=host,
        sha=sha,
        worktree=str(wt),
        remote_worktree=dest,
        host_checkout=root,
        envs=",".join(envs.replace(",", " ").split()),
    )
    with locked(f"rt-{host}.lock"):
        prep = P.command("remote_tests", "prepare", **args)
        if not prep:
            raise ValueError(
                "remote_tests.prepare must provision the configured remote worktree"
            )
        remote(host, prep, timeout=1800)
        excludes = P.get(
            "remote_tests",
            "excludes",
            [".git", "out", ".venv", "node_modules", "target"],
        )
        argv = ["rsync", "-as", "--delete"]
        if spec.get("rsync_path"):
            argv += ["--rsync-path", spec["rsync_path"]]
        argv += [arg for path in excludes for arg in ("--exclude", path)]
        if ".git" not in excludes:
            raise ValueError("remote_tests.excludes must keep .git")
        run(
            [*argv, f"{wt}/", f"{spec['ssh']}:{dest.removeprefix('~/')}/"], timeout=1800
        )
        provision = P.command("remote_tests", "provision", **args)
        if provision:
            remote(host, provision, cwd=dest, timeout=1800)
        rid = f"rt-{uuid4().hex[:12]}"
        prefix = P.command(
            "remote_tests", "prefix", default=spec.get("rt_prefix", []), **args
        )
        record(
            "run",
            rid,
            "running",
            "--machine",
            host,
            "--task",
            shlex.join(gate),
            "--title",
            wt.name,
        )
        try:
            return remote(
                host, [*prefix, *gate], cwd=dest, timeout=5400, check=False
            ).returncode
        finally:
            record("run", rid, "done")


def cert_args(wt, pr, target, sha, base, host):
    spec = P.hosts[host]
    platform = spec.get("platform", "")
    root = spec.get("checkout", "")
    dest = f"{root.rstrip('/')}/{spec.get('cert_root', 'scratch')}/certify-{sha}"
    layer = P.get("certification", "layers", {}).get(target, target)
    if layer == "platform":
        layer = platform
    config = P.get("certification", "remote_config", "")
    return dict(
        worktree=str(wt),
        pr=pr,
        target=target,
        sha=sha,
        base=base,
        host=host,
        platform=platform,
        layer=layer,
        remote_worktree=dest,
        host_checkout=root,
        remote_config=str((P.config_file.parent / Path(config).expanduser()).resolve())
        if config
        else "",
    )


def certify(wt, pr, target, sha, base):
    slots = P.get("certification", "slots", {}).get(target, [])
    if not slots:
        raise ValueError(f"no hosts configured in certification.slots.{target}")
    path = P.data / "logs"
    path.mkdir(exist_ok=True, mode=0o700)
    short = sha[:9]
    host = "pending"

    def status(phase):
        record(
            "cert-state",
            pr,
            phase,
            "--target",
            target,
            "--sha",
            short,
            "--machine",
            host,
            "--task",
            os.getpid(),
        )

    with ExitStack() as stack:
        try:
            stack.enter_context(locked(f"job-{pr}-{target}-{sha}.lock", wait=False))
        except BlockingIOError:
            return 0
        status("queued")
        deadline = time.monotonic() + P.get("certification", "slot_timeout", 2700)
        while host == "pending":
            waiting = {
                c.get("target")
                for c in load(P.data / "state.json", {}).get("certs", [])
                if c.get("status") == "queued"
            }
            for key in slots:
                preferred = P.hosts[key].get("cert_priority")
                if preferred != target and preferred in waiting:
                    continue
                try:
                    for slot in range(P.hosts[key].get("slots", 1)):
                        try:
                            stack.enter_context(
                                locked(f"cert-host-{key}-{slot}.lock", wait=False)
                            )
                            host = key
                            break
                        except BlockingIOError:
                            continue
                    if host != "pending":
                        break
                except KeyError:
                    raise ValueError(f"unknown certification host {key!r}") from None
            if host == "pending":
                if (
                    target not in P.get("certification", "must_run", [])
                    and time.monotonic() >= deadline
                ):
                    status("skipped")
                    return 0
                time.sleep(30)
        info = json_gh("pr", "view", str(pr), "--json", "headRefOid")
        if info["headRefOid"] != sha:
            status("superseded")
            return 0
        status("running")
        ctx = cert_args(wt, pr, target, sha, base, host)
        is_remote = bool(P.ssh(host))
        kind = "remote" if is_remote else "local"
        command = P.command(
            "certification",
            f"{kind}_command",
            **ctx,
            default=["just", "certify"] if not is_remote else [],
        )
        if not command:
            raise ValueError(f"certification.{kind}_command is required")
        command += P.command("certification", target + "_args", **ctx)
        command += P.expand(P.hosts[host].get("cert_args", []), **ctx)
        log_path = path / f"cert-{pr}-{target}-{short}.log"
        code = 1
        with log_path.open("a", encoding="utf-8") as log:
            output = dict(stdout=log, stderr=subprocess.STDOUT)
            try:
                envs = P.command("certification", "environments", **ctx)
                if envs:
                    ctx["envs"] = run(
                        envs, cwd=wt, stdout=subprocess.PIPE, text=True, timeout=180
                    ).stdout.strip()
                else:
                    ctx["envs"] = ""
                provision = (
                    P.expand(P.hosts[host]["cert_prepare"], **ctx)
                    if "cert_prepare" in P.hosts[host]
                    else (P.command("certification", f"{kind}_prepare", **ctx))
                )
                if provision:
                    if is_remote:
                        remote(host, provision, timeout=3600, **output)
                    else:
                        run(provision, cwd=wt, timeout=3600, **output)
                code = run(
                    command, cwd=wt, timeout=6 * 3600, check=False, **output
                ).returncode
            except (OSError, subprocess.SubprocessError) as exc:
                print(exc, file=log, flush=True)
            finally:
                cleanup = (
                    P.expand(P.hosts[host]["cert_cleanup"], **ctx)
                    if "cert_cleanup" in P.hosts[host]
                    else (P.command("certification", f"{kind}_cleanup", **ctx))
                )
                if cleanup:
                    try:
                        if is_remote:
                            remote(host, cleanup, timeout=1800, **output)
                        else:
                            run(cleanup, cwd=wt, timeout=1800, **output)
                    except (OSError, subprocess.SubprocessError) as exc:
                        print(f"Cleanup failed: {exc}", file=log, flush=True)
                        code = code or 1
                print(f"\nEXIT {code}", file=log)
        text = log_path.read_text(encoding="utf-8", errors="replace")
        failure = any(
            marker in text
            for marker in P.get("certification", "test_failure_markers", [])
        )
        status("pass" if code == 0 else "fail" if failure else "error")
        message = (
            f"PR #{pr}: {target} cert exit {code} on {short} ({host}); log {log_path}"
        )
        record("log", message)
        notify = P.command("certification", "notify", message=message, **ctx)
        if notify:
            run(notify, check=False, timeout=30)
        return code


def cert_main():
    parser = argparse.ArgumentParser(
        description="Certify the exact published head locally or on a configured host."
    )
    parser.add_argument("worktree", type=Path)
    parser.add_argument("target", choices=list(P.get("certification", "slots", {})))
    parser.add_argument("--pr", type=int)
    args = parser.parse_args()
    assert_owner()
    wt = args.worktree.expanduser().resolve()
    pr_ref = str(args.pr) if args.pr else git(wt, "symbolic-ref", "--short", "HEAD")
    pr = json_gh("pr", "view", pr_ref, "--json", "number,headRefOid,baseRefOid")
    sha = git(wt, "rev-parse", "HEAD")
    if sha != pr["headRefOid"]:
        raise ValueError(
            "certification worktree must be at the current published PR head"
        )
    return certify(wt, pr["number"], args.target, sha, pr["baseRefOid"])


def cert_pr_main():
    parser = argparse.ArgumentParser(
        description="Prepare a certification worktree for a PR and run configured targets."
    )
    parser.add_argument("pr", type=int)
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args()
    assert_owner()
    targets = args.targets or P.get("certification", "default_targets", [])
    if not targets or set(targets) - P.get("certification", "slots", {}).keys():
        parser.error("provide configured certification targets")
    pr = json_gh(
        "pr", "view", str(args.pr), "--json", "headRefName,headRefOid,isCrossRepository"
    )
    if pr["isCrossRepository"]:
        raise ValueError("cert-pr needs a branch in the configured repository")
    from worktrees import prepare

    sha = pr["headRefOid"]
    with locked(f"cert-pr-{args.pr}.lock", wait=False):
        with locked(f"worktree-pr-{args.pr}.lock"):
            wt = prepare(args.pr, pr, "cert")
        prep = P.command(
            "certification", "controller_prepare", pr=args.pr, sha=sha, worktree=wt
        )
        if prep:
            run(prep, cwd=wt, timeout=1800)
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            return max(
                pool.map(
                    lambda target: (
                        run(
                            cli("cert", wt, target, "--pr", args.pr), check=False
                        ).returncode
                    ),
                    targets,
                )
            )
