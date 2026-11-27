"""Remote test runs and multi-host local certification."""

import argparse
import base64
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from uuid import uuid4

from cert_policy import (
    planned_path,
    review_state,
    routed,
    routed_targets,
    router_layers,
    statuses,
)
from landing import assert_owner, eligible, json_gh, review
from retries import ACTION_ERRORS, Retry, RetryLimit, error_class
from runtime import P, agent_coord_status, cli, load, locked, save

RT_NOT_ADMITTED = 75


def run(argv, *, cwd=None, check=True, **kwargs):
    return subprocess.run(argv, cwd=cwd or P.checkout, check=check, **kwargs)


def git(wt, *args):
    return run(
        ["git", "-C", str(wt), *args], stdout=subprocess.PIPE, text=True, timeout=180
    ).stdout.strip()


def record(*args):
    # Dashboard bookkeeping must never fail the work it describes.
    run(cli(*args), timeout=90, check=False)


def remote(host, argv, *, cwd=None, **kwargs):
    ssh = P.ssh(host)
    if not ssh:
        return run(argv, cwd=Path(cwd).expanduser() if cwd else None, **kwargs)
    if P.hosts[host].get("windows"):

        def ps(arg):
            # PowerShell also ends a single-quoted string at U+2018..U+201B.
            return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", str(arg)) + "'"

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
    parser.add_argument(
        "--admit-now",
        action="store_true",
        help="decline with exit 75 instead of waiting for remote capacity",
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
    context = {
        "host": host,
        "sha": sha,
        "worktree": str(wt),
        "remote_worktree": dest,
        "host_checkout": root,
        "envs": ",".join(envs.replace(",", " ").split()),
    }

    admission = P.expand(
        spec.get("rt_admission", []), label=f"hosts.{host}.rt_admission", **context
    )
    prefix = P.command(
        "remote_tests", "prefix", default=spec.get("rt_prefix", []), **context
    )
    retry = Retry(
        P.data,
        "rt",
        head=sha,
        host=host,
        worktree=wt,
        envs=context["envs"],
        command_line=[*prefix, *gate],
        settings=P.settings.get("remote_tests"),
        spec=spec,
    )

    def admitted():
        try:
            return bool(
                admission
                and remote(host, admission, check=False, timeout=30).returncode == 0
            )
        except (OSError, subprocess.SubprocessError):
            return False

    def declined():
        print(f"Remote tests: {host} cannot admit this run now.", file=sys.stderr)
        return RT_NOT_ADMITTED

    with ExitStack() as stack:
        try:
            stack.enter_context(locked(f"rt-{host}.lock", wait=not args.admit_now))
        except BlockingIOError:
            return declined()
        if args.admit_now and not admitted():
            return declined()
        prep = P.command("remote_tests", "prepare", **context)
        if not prep:
            raise ValueError(
                "remote_tests.prepare must provision the configured remote worktree"
            )
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
        provision = P.command("remote_tests", "provision", **context)
        retry.check()
        step = "prepare"
        try:
            print(
                f"Remote tests: preparing {host}:{dest} at {sha[:9]}",
                file=sys.stderr,
                flush=True,
            )
            remote(host, prep, timeout=1800)
            step = "synchronize"
            run(
                [*argv, f"{wt}/", f"{spec['ssh']}:{dest.removeprefix('~/')}/"],
                timeout=1800,
            )
            step = "provision"
            if provision:
                remote(host, provision, cwd=dest, timeout=1800)
        except ACTION_ERRORS as exc:
            retry.failed(f"{step}-{error_class(exc)}", exc)
            raise
        rid = f"rt-{uuid4().hex[:12]}"
        if args.admit_now and not admitted():
            return declined()
        record(
            "run-state",
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
            try:
                result = remote(
                    host, [*prefix, *gate], cwd=dest, timeout=5400, check=False
                ).returncode
            except ACTION_ERRORS as exc:
                retry.failed(f"test-{error_class(exc)}", exc)
                raise
            if result:
                retry.failed(f"test-exit-{result}", f"{rid} on {host} exited {result}")
            # Keep capacity denial distinguishable from a failed remote command.
            return 1 if args.admit_now and result == RT_NOT_ADMITTED else result
        finally:
            record("run-state", rid, "done")


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


def cert_reservation(snapshot, names, *, whole_host, long_hold_seconds):
    """Classify a host reservation separately from ordinary cert contention."""
    now = time.time()
    cert_labels = {"certification", "local certification", "remote certification"}
    holds = [
        (lock.get("reason"), (lock.get("expires") or now) - now)
        for name, lock in snapshot.get("locks", {}).items()
        if name in names
    ]
    holds.extend(
        (lease.get("label"), lease.get("granted", now) + lease.get("expect", 0) - now)
        for lease in snapshot.get("leases", [])
        if names.intersection(lease.get("locks", []))
        or (whole_host and lease.get("exclusive"))
    )
    if any(
        label not in cert_labels or remaining > long_hold_seconds
        for label, remaining in holds
    ):
        return "reserved"
    return "busy" if holds else None


def cert_host_states(slots):
    controller = agent_coord_status()
    threshold = P.get("certification", "long_hold_seconds", 3600)
    for key in slots:
        if key not in P.hosts:
            raise ValueError(f"unknown certification host {key!r}")
        spec = P.hosts[key]
        alias = spec.get("ssh")
        names = {key, alias} if alias else {key}
        state = cert_reservation(
            controller, names, whole_host=not alias, long_hold_seconds=threshold
        )
        if state == "reserved":
            yield key, state
            continue
        if not P.reachable(key):
            yield key, "offline"
            continue
        if alias and spec.get("agent_coord"):
            worker = agent_coord_status(partial(remote, key))
            worker_lock = spec.get("cert_worker_lock")
            remote_state = cert_reservation(
                worker,
                names | {worker_lock} if worker_lock else names,
                whole_host=True,
                long_hold_seconds=threshold,
            )
            if remote_state == "reserved" or state is None:
                state = remote_state
        yield key, state


def certify(wt, pr, target, sha, base, _attempt=0, *, admitted=None, on_status=None):
    slots = P.get("certification", "slots", {}).get(target, [])
    if not slots:
        raise ValueError(f"no hosts configured in certification.slots.{target}")
    path = P.data / "logs"
    path.mkdir(exist_ok=True, mode=0o700)
    short = sha[:9]
    host = admitted or "pending"

    def status(phase):
        if on_status:
            on_status(phase)
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
        required = target in P.get("certification", "must_run", [])
        retries = {key: cert_retry(pr, target, sha, base, key) for key in slots}
        deadline = time.monotonic() + P.get("certification", "slot_timeout", 2700)
        while host == "pending":
            blocked = blocked_hosts(retries)
            if len(blocked) == len(retries):
                status("error")
                raise RetryLimit(
                    "; ".join(f"{key}: {exc}" for key, exc in blocked.items())
                )
            waiting = {
                c.get("target")
                for c in load(P.data / "state.json", {}).get("certs", [])
                if c.get("status") == "queued"
            }
            unavailable = []
            for key, state in cert_host_states(slots):
                if key in blocked:
                    unavailable.append(f"{key} (retry limit)")
                    continue
                if state in {"offline", "reserved"}:
                    unavailable.append(f"{key} ({state})")
                    continue
                if state == "busy":
                    continue
                preferred = P.hosts[key].get("cert_priority")
                if preferred != target and preferred in waiting:
                    continue
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
            if host == "pending":
                if not required and len(unavailable) == len(slots):
                    if blocked:
                        status("error")
                        raise RetryLimit(
                            f"No alternative host available; {next(iter(blocked.values()))}"
                        )
                    status("skipped")
                    record(
                        "log",
                        f"PR #{pr}: {target} cert skipped; no host available "
                        f"({', '.join(unavailable)})",
                    )
                    return 0
                if not required and time.monotonic() >= deadline:
                    status("skipped")
                    return 0
                time.sleep(30)
        try:
            with retries[host].observe("head-check"):
                info = json_gh("pr", "view", str(pr), "--json", "headRefOid")
        except ACTION_ERRORS:
            status("error")
            raise
        if info["headRefOid"] != sha:
            status("superseded")
            return 0
        status("running")
        try:
            with retries[host].observe("configuration"):
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
                if admitted:
                    command += P.command("certification", f"routed_{kind}_args", **ctx)
        except ACTION_ERRORS:
            status("error")
            raise
        log_path = path / f"cert-{pr}-{target}-{short}.log"
        previous = log_path.stat() if log_path.exists() else None
        start = previous.st_size if previous else 0
        code = 1
        failure_class = None
        with log_path.open("a", encoding="utf-8") as log:
            started = datetime.now(timezone.utc).isoformat()
            print(
                f"CERT START {started} {previous.st_mtime if previous else 0}",
                file=log,
                flush=True,
            )
            output = {"stdout": log, "stderr": subprocess.STDOUT}
            try:
                envs = (
                    P.command("certification", "environments", **ctx)
                    if admitted is None
                    else []
                )
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
                if provision and admitted is None:
                    if is_remote:
                        remote(host, provision, timeout=3600, **output)
                    else:
                        run(provision, cwd=wt, timeout=3600, **output)
                code = run(
                    command, cwd=wt, timeout=6 * 3600, check=False, **output
                ).returncode
            except (OSError, subprocess.SubprocessError) as exc:
                failure_class = error_class(exc)
                print(exc, file=log, flush=True)
            finally:
                cleanup = (
                    P.expand(P.hosts[host]["cert_cleanup"], **ctx)
                    if "cert_cleanup" in P.hosts[host]
                    else (P.command("certification", f"{kind}_cleanup", **ctx))
                )
                if cleanup and admitted is None:
                    try:
                        if is_remote:
                            remote(host, cleanup, timeout=1800, **output)
                        else:
                            run(cleanup, cwd=wt, timeout=1800, **output)
                    except (OSError, subprocess.SubprocessError) as exc:
                        print(f"Cleanup failed: {exc}", file=log, flush=True)
                        code = code or 1
                        failure_class = failure_class or f"cleanup-{error_class(exc)}"
                ended = datetime.now(timezone.utc).isoformat()
                print(f"\nCERT END {ended}\nEXIT {code}", file=log)
        with log_path.open("rb") as log:
            log.seek(start)
            text = log.read().decode("utf-8", errors="replace")
        # The project's controller may refuse a host it finds busy (e.g. a lock taken outside these slots):
        # wait for it instead of reporting an error.
        busy = code != 0 and any(
            marker in text for marker in P.get("certification", "busy_markers", [])
        )
        failure = any(
            marker in text
            for marker in P.get("certification", "test_failure_markers", [])
        )
        if code:
            retries[host].failed(
                "busy"
                if busy
                else "tests"
                if failure
                else failure_class or f"exit-{code}",
                f"{host} exited {code}; output: {log_path} (byte offset {start})",
            )
        retry = (
            admitted is None
            and busy
            and _attempt < min(1, P.get("certification", "busy_retries", 1))
        )
        if retry:
            try:
                retries[host].check()
            except RetryLimit as exc:
                record("log", str(exc))
                print(exc, file=sys.stderr, flush=True)
                retry = False
        if retry:
            status("queued")
            record("log", f"PR #{pr}: {target} cert host {host} busy; retrying")
        else:
            status("pass" if code == 0 else "fail" if failure else "error")
    if retry:
        time.sleep(P.get("certification", "busy_retry_seconds", 120))
        return certify(wt, pr, target, sha, base, _attempt + 1)
    message = f"PR #{pr}: {target} cert exit {code} on {short} ({host}); log {log_path}"
    record("log", message)
    notify = P.command("certification", "notify", message=message, **ctx)
    if notify:
        run(notify, check=False, timeout=30)
    return code


def cert_retry(pr, target, sha, base, host):
    return Retry(
        P.data,
        "cert",
        pr=pr,
        head=sha,
        target=target,
        base=base,
        host=host,
        settings=getattr(P, "settings", {}).get("certification"),
        spec=P.hosts[host],
    )


def blocked_hosts(retries):
    blocked = {}
    for host, action in retries.items():
        try:
            action.check()
        except RetryLimit as exc:
            blocked[host] = exc
    return blocked


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
    parser.add_argument("--head", help="require an exact published PR head")
    parser.add_argument("--force", action="store_true", help="allow an unsettled head")
    args = parser.parse_args()
    assert_owner()
    targets = list(
        dict.fromkeys(args.targets or P.get("certification", "default_targets", []))
    )
    slots = P.get("certification", "slots", {})
    if set(args.targets) - slots.keys() or (
        not routed() and (not targets or set(targets) - slots.keys())
    ):
        parser.error("provide configured certification targets")
    pr = json_gh(
        "pr",
        "view",
        str(args.pr),
        "--json",
        "headRefName,headRefOid,baseRefOid,isCrossRepository",
    )
    if pr["isCrossRepository"]:
        raise ValueError("cert-pr needs a branch in the configured repository")
    from worktrees import prepare

    sha = pr["headRefOid"]
    if args.head and sha != args.head:
        raise ValueError("head changed before certification was requested")
    check_cert_review(args.pr, sha, args.force, warn=True)
    # Per head: a push while an older head certifies must start the new head (the old run is superseded).
    with ExitStack() as stack:
        try:
            stack.enter_context(locked(f"cert-pr-{args.pr}-{sha}.lock", wait=False))
        except BlockingIOError:
            print(f"PR #{args.pr} head {sha[:9]}: certification already running")
            return 0
        if routed():
            return certify_route(args.pr, pr, args.targets, slots, args.force, stack)
        runnable = []
        for target in targets:
            blocked = blocked_hosts(
                {
                    host: cert_retry(args.pr, target, sha, pr["baseRefOid"], host)
                    for host in slots[target]
                }
            )
            if blocked and len(blocked) == len(slots[target]):
                for host, exc in blocked.items():
                    print(f"PR #{args.pr} {target} on {host}: {exc}", file=sys.stderr)
            else:
                runnable.append(target)
        declined = int(len(runnable) != len(targets))
        if not runnable:
            return declined
        retry = Retry(
            P.data,
            "cert-pr",
            pr=args.pr,
            head=sha,
            targets=sorted(runnable),
            settings=P.settings.get("certification"),
        )
        with retry.observe("prepare"):
            check_cert_review(args.pr, sha, args.force)
            with locked(f"worktree-pr-{args.pr}.lock"):
                wt = prepare(args.pr, pr, "cert")
            prep = P.command(
                "certification", "controller_prepare", pr=args.pr, sha=sha, worktree=wt
            )
            if prep:
                run(prep, cwd=wt, timeout=1800)
        check_cert_review(args.pr, sha, args.force)
        with ThreadPoolExecutor(max_workers=len(runnable)) as pool:
            return max(
                declined,
                *pool.map(
                    lambda target: (
                        run(
                            cli("cert", wt, target, "--pr", args.pr), check=False
                        ).returncode
                    ),
                    runnable,
                ),
            )


def check_cert_review(pr, head, force, *, warn=False):
    row = review(pr)
    if row["headRefOid"] != head:
        raise ValueError("head changed before certification")
    if not eligible({**row, "isDraft": False}):
        raise ValueError(
            "cert-pr needs an eligible owner PR in the configured repository"
        )
    if not review_state(pr, head, row).settled:
        if not force:
            raise ValueError(
                "review is unsettled on this head; use --force to override"
            )
        if warn:
            print(
                f"WARNING: forcing certification of unsettled head {head[:9]}",
                file=sys.stderr,
            )


def check_cert_plan(pr, sha, base, force):
    check_cert_review(pr, sha, force)
    row = json_gh("pr", "view", str(pr), "--json", "headRefOid,baseRefOid")
    if row["headRefOid"] != sha or row["baseRefOid"] != base:
        raise ValueError(
            "PR head or base changed; abandon the stale certification plan"
        )


def admit_cert_host(hosts, stack):
    for host, state in cert_host_states(hosts):
        if state is not None:
            continue
        for slot in range(P.hosts[host].get("slots", 1)):
            try:
                stack.enter_context(locked(f"cert-host-{host}-{slot}.lock", wait=False))
            except BlockingIOError:
                continue
            return host
    return None


def certify_route(pr, row, requested, slots, force, stack):
    sha, base = row["headRefOid"], row["baseRefOid"]
    path = planned_path(pr, sha, base)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    check_cert_plan(pr, sha, base, force)
    try:
        layers = router_layers(pr, sha, base)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        if requested:
            raise ValueError(f"ready router unavailable: {exc}") from exc
        check_cert_plan(pr, sha, base, force)
        print(
            f"PR #{pr}: ready router unavailable; hosted fallback ({exc})",
            file=sys.stderr,
        )
        save(path, {"outcome": "complete", "hosted": ["unknown"]})
        return 0

    mapping = routed_targets(layers, slots)
    if set(requested) - {target for target, hosts in mapping.items() if hosts}:
        raise ValueError(
            "explicit certification targets must match the ready router plan"
        )
    latest = statuses(sha)
    if any(value == "failure" for value in latest.values()):
        raise ValueError(
            "local tests failed on this head; fix the failure before readiness"
        )
    saved = load(path, {})
    if saved.get("outcome") == "failure":
        raise ValueError("local tests failed on this certification plan")
    if saved.get("outcome") == "complete" and not requested:
        print(f"PR #{pr} head {sha[:9]}: certification plan already settled")
        return 0
    attempt_path = path.with_name(f"{pr}-{sha}-attempts.json")
    attempted = load(attempt_path, {})
    if "fail" in attempted.values():
        raise ValueError("local tests failed on this certification head")
    completed = {
        layer for layer in layers if latest.get(P.cert_prefix + layer) == "success"
    }
    hosted = set()
    reserved = {}
    candidates = requested or mapping
    for layer in sorted(
        layers - completed, key=lambda layer: (layer != "generic", layer)
    ):
        if (
            latest.get(P.cert_prefix + layer) in {"pending", "error"}
            or layer in attempted
        ):
            hosted.add(layer)
            continue
        for target in candidates:
            hosts = [
                host
                for host, candidate in mapping[target].items()
                if candidate == layer
            ]
            if hosts and (host := admit_cert_host(hosts, stack)):
                reserved[layer] = (target, host)
                break
        else:
            hosted.add(layer)
    if reserved:
        check_cert_plan(pr, sha, base, force)
        from worktrees import prepare

        try:
            with locked(f"worktree-pr-{pr}.lock", wait=False):
                wt = prepare(pr, row, "cert")
        except BlockingIOError:
            hosted.update(reserved)
            reserved.clear()
    if reserved:
        check_cert_plan(pr, sha, base, force)
        attempt_lock = threading.Lock()

        def execute(layer, target, host):
            phase = ""

            def capture(value):
                nonlocal phase
                phase = value

            check_cert_plan(pr, sha, base, force)
            with attempt_lock:
                attempted[layer] = "started"
                save(attempt_path, attempted)
            certify(wt, pr, target, sha, base, admitted=host, on_status=capture)
            with attempt_lock:
                attempted[layer] = phase
                save(attempt_path, attempted)
            return layer, phase

        with ThreadPoolExecutor(max_workers=len(reserved)) as pool:
            jobs = [
                pool.submit(execute, layer, *job) for layer, job in reserved.items()
            ]
            outcomes = dict(job.result() for job in jobs)
        if "fail" in outcomes.values():
            save(path, {"outcome": "failure"})
            return 1
        hosted.update(layer for layer, result in outcomes.items() if result != "pass")
    check_cert_plan(pr, sha, base, force)
    if hosted:
        print(
            f"PR #{pr} head {sha[:9]}: hosted fallback for {', '.join(sorted(hosted))}"
        )
    if not requested:
        save(path, {"outcome": "complete", "hosted": sorted(hosted)})
    return 0
