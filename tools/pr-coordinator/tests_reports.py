"""Bounded, read-only collection of hosted and local pytest evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from runtime import P, gh_argv
from tests_inventory import (
    BASE,
    HERE,
    ROOT,
    UTC,
    api,
    load,
    save,
    stamp,
    workflows_runs,
)

REPORTS = HERE / "tests_inventory_reports.json"
REPORT_CACHE = HERE / "tests_inventory_reports"
WINDOW = P.get("tests", "report_days", 14)
MAX_XML = 16 * 1024 * 1024
MAX_HOSTED_RUNS_PER_STAGE = max(2, P.get("tests", "max_hosted_runs_per_stage", 54))
MAX_LOCAL_PER_HOST = P.get("tests", "max_local_reports_per_host", 240)
HOSTED = P.get("tests", "hosted_workflows", {})
TEST_ARTIFACT = re.compile(P.get("tests", "artifact_pattern", r"tests(?:-(.*))?"))
XML_FILES = set(P.get("tests", "artifact_xml_names", ["pytest.xml"]))
CLASS_PATTERN = re.compile(
    P.get(
        "tests",
        "junit_class_pattern",
        r"((?:tests\.)?(?:[\w-]+\.)*test_[\w-]+)(?:\.(.+))?",
    )
)
FILE_PATTERN = re.compile(P.get("tests", "junit_file_pattern", r"tests/[\w./-]+\.py"))
PATH_PREFIX = P.get("tests", "junit_path_prefix", "tests/")
ENV = re.compile(
    r"no space left on device|disk quota exceeded|connection (?:reset|refused|timed out)|"
    r"temporary failure in name resolution|runner (?:lost|disconnected)|"
    r"resource temporarily unavailable|no matching distribution found|"
    r"failed to (?:install|download) (?:locked|dependency|dependencies)|"
    r"(?:command not found|dependency unavailable)",
    re.I,
)


def cases(data: bytes) -> list[list[Any]]:
    if len(data) > MAX_XML or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise ValueError("oversized XML or unsupported document type")
    result = []
    for case in ET.fromstring(data).iter("testcase"):
        klass = case.get("classname", "")
        match = CLASS_PATTERN.fullmatch(klass)
        raw_path = (case.get("file") or "").replace("\\", "/")
        if match:
            module, test_class = match.groups()
            file = module.replace(".", "/") + ".py"
            if not file.startswith(PATH_PREFIX):
                file = PATH_PREFIX + file
        elif FILE_PATTERN.fullmatch(raw_path):
            file, test_class = raw_path, None
        else:
            continue
        name = "::".join(filter(None, (test_class, case.get("name"))))
        if not name:
            continue
        failure = next(
            (child for child in case if child.tag in {"failure", "error"}), None
        )
        if failure is not None:
            text = (failure.get("message") or "") + " " + (failure.text or "")
            last = (failure.get("message") or text.strip().split("\n")[-1]).strip()
            signature = re.sub(r"0x[0-9a-fA-F]+|\b\d{3,}\b", "#", last)
            fingerprint = (
                hashlib.sha256(signature.encode()).hexdigest()[:16] if signature else ""
            )
            result.append(
                [file, name, "fail", seconds(case), bool(ENV.search(text)), fingerprint]
            )
        else:
            result.append(
                [
                    file,
                    name,
                    "skip" if case.find("skipped") is not None else "pass",
                    seconds(case),
                    False,
                    "",
                ]
            )
    return result


def seconds(case: ET.Element) -> float:
    try:
        value = float(case.get("time", "0"))
    except ValueError:
        return 0.0
    return round(value, 4) if math.isfinite(value) and value > 0 else 0.0


def pr_number(run: dict[str, Any]) -> int | None:
    prs = {p["number"] for p in run.get("pull_requests", [])}
    if len(prs) == 1:
        return int(prs.pop())
    match = re.search(r"(?:^|/)pr-(\d+)(?:-|$)", run.get("head_branch") or "")
    return int(match[1]) if match and run["event"] == "merge_group" else None


def hosted_stage(run: dict[str, Any]) -> str:
    path = Path(run["path"].split("@", 1)[0]).name
    return HOSTED.get(path, {}).get(run["event"], "")


def hosted_report(run: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    rid = run["id"]
    attempt = run["run_attempt"]
    index_path = REPORT_CACHE / f"index-{rid}-{attempt}.json"
    try:
        index = load(index_path, None)
        if index is None:
            fields = ("id", "name", "expired", "size_in_bytes", "created_at")
            index = [
                {key: a[key] for key in fields}
                for a in api(f"actions/runs/{rid}/artifacts", "artifacts")
                if TEST_ARTIFACT.fullmatch(a["name"])
            ]
            save(index_path, index)
        reports = []
        artifacts = {item["id"]: item for item in index}
        for prior in range(1, attempt):
            for item in load(REPORT_CACHE / f"index-{rid}-{prior}.json", []):
                if (REPORT_CACHE / f"{item['id']}.json").is_file():
                    artifacts.setdefault(item["id"], item)
        for artifact in artifacts.values():
            aid = artifact["id"]
            path = REPORT_CACHE / f"{aid}.json"
            tests = load(path, None)
            if tests is None:
                if artifact["expired"] or artifact["size_in_bytes"] > MAX_XML:
                    continue
                zip_bytes = subprocess.run(
                    gh_argv("api", f"{BASE}/actions/artifacts/{aid}/zip"),
                    capture_output=True,
                    check=True,
                    timeout=60,
                    cwd=ROOT,
                ).stdout
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
                    tests = []
                    for info in archive.infolist():
                        if (
                            Path(info.filename).name in XML_FILES
                            and info.file_size <= MAX_XML
                        ):
                            tests.extend(cases(archive.read(info)))
                save(path, tests)
            if not tests:
                continue
            lane = TEST_ARTIFACT.fullmatch(artifact["name"])
            if lane is None:
                continue
            reports.append(
                {
                    "id": f"artifact:{aid}",
                    "run": str(rid),
                    "source": "GitHub Actions",
                    "stage": hosted_stage(run),
                    "lane": (lane[1] if lane.lastindex else None) or artifact["name"],
                    "observed_sha": run["head_sha"],
                    "sha": run["head_sha"],
                    "pr": pr_number(run),
                    "at": artifact["created_at"],
                    "tests": tests,
                }
            )
        return reports, ""
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        ValueError,
        ET.ParseError,
        zipfile.BadZipFile,
    ) as exc:
        return [], f"GitHub run {rid}: {type(exc).__name__}"


POSIX_SCAN = r"""
import base64, json, os, re, sys
from pathlib import Path
home = Path.home()
after, limit, max_xml = map(int, sys.argv[1:4])
opts = json.loads(sys.argv[4])
import glob
from collections import Counter
def paths(patterns):
    return list(dict.fromkeys(Path(path) for pattern in patterns for path in glob.glob(os.path.expanduser(pattern))))
certs = paths(opts.get("cert_worktrees", []))
others = paths(opts.get("rt_worktrees", []))
reports, logs, layers = [], 0, []
for wt in certs + others:
    out = wt / "out"
    if wt in certs:
        for log in wt.glob(opts.get("cert_log", "out/certify/*/*/run.log")):
            at = log.stat().st_mtime
            if at >= after:
                logs += 1
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 65536))
                    end = stream.read()
                match = re.search(rb"(?m)^Executed \d+ pytest tests in (\d+)s$", end)
                if match:
                    layers.append([at, log.parent.name, int(match[1]), "success"])
                elif re.search(rb"(?m)^=+.*\d+ (?:failed|errors?).* in [\d.]+s.*=+$", end):
                    layers.append([at, log.parent.name, None, "failure"])
        for log in wt.glob(opts.get("remote_cert_log", "out/certify/*/remote-*/*/run.log")):
            at = log.stat().st_mtime
            if at < after:
                continue
            logs += 1
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 65536))
                end = stream.read()
            match = re.search(rb"(?m)^" + re.escape(opts.get("cert_result_marker", "CERTIFICATION_RESULT").encode()) + rb" (\{[^\r\n]+\})", end)
            if match:
                try:
                    summary = json.loads(match[1])
                except ValueError:
                    continue
                if summary.get("sha_after") == log.parents[2].name and summary.get("state") in {"success", "failure"}:
                    layers.append([at, log.parent.name, None, summary["state"]])
        for file in wt.glob(opts.get("cert_xml", "out/certify/*/*/*.xml")):
            if file.stat().st_mtime >= after:
                reports.append((file.stat().st_mtime, file, file.parent.parent.name, file.parent.name, wt))
    else:
        # Remote tests may include uncommitted changes; don't attribute them to the worktree SHA.
        for file in wt.glob(opts.get("rt_xml", "out/**/*.xml")):
            if file.is_file() and file.stat().st_mtime >= after:
                reports.append((file.stat().st_mtime, file, "", "remote test", wt))
        logs += sum(file.stat().st_mtime >= after for file in wt.glob(opts.get("rt_log", "out/**/*.log")) if file.is_file())
print(json.dumps({"found": len(reports), "logs": logs, "layers": layers}), flush=True)
for at, file, sha, lane, wt in sorted(reports, reverse=True)[:limit]:
    if file.stat().st_size > max_xml:
        continue
    match = re.search(r"(?:^|[-_])(?:pr[-_]?)?(\d{3,})(?:[-_]|$)", wt.name)
    print(json.dumps([at, str(file.relative_to(home) if file.is_relative_to(home) else file), sha, lane, int(match[1]) if match else None,
                      base64.b64encode(file.read_bytes()).decode()]), flush=True)
"""


WINDOWS_SCAN = r"""
$after = [DateTimeOffset]::FromUnixTimeSeconds(AFTER).UtcDateTime
$homeDir = $env:USERPROFILE
$opts = ConvertFrom-Json -InputObject ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('OPTIONS')))
function Expand-Pattern($p) { if ($p.StartsWith('~/')) {return (Join-Path $homeDir ($p.Substring(2)))}; return $p }
$sources = @(foreach ($p in $opts.cert_worktrees) { Get-Item -Path (Expand-Pattern $p) -ErrorAction SilentlyContinue })
$reports = @()
$logs = 0
$layers = @()
foreach ($wt in $sources) {
  if ($null -eq $wt) { continue }
  $certXml = Join-Path $wt.FullName $opts.cert_xml
  $certLog = Join-Path $wt.FullName $opts.cert_log
  $reports += @(Get-ChildItem $certXml -File -ErrorAction SilentlyContinue | Where-Object {$_.LastWriteTimeUtc -ge $after})
  foreach ($log in @(Get-ChildItem $certLog -File -ErrorAction SilentlyContinue | Where-Object {$_.LastWriteTimeUtc -ge $after})) {
    $logs++
    $tail = Get-Content -LiteralPath $log.FullName -Tail 4 | Select-String -Pattern '^Executed \d+ pytest tests in (\d+)s$'
    if ($tail) {
      $layers += ,@(([DateTimeOffset]$log.LastWriteTimeUtc).ToUnixTimeSeconds(), $log.Directory.Name, [int]$tail.Matches[0].Groups[1].Value, 'success')
    }
  }
}
ConvertTo-Json -Compress @{found=$reports.Count;logs=$logs;layers=$layers}
foreach ($file in ($reports | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First LIMIT)) {
  if ($file.Length -gt MAXXML) { continue }
  $rel = $file.FullName
  $pr = $null
  if ($rel -match '(?:^|[-\\])(?:pr[-_]?)?(\d{3,})(?:[-_\\]|$)') { $pr = [int]$Matches[1] }
  ConvertTo-Json -Compress -InputObject @(([DateTimeOffset]$file.LastWriteTimeUtc).ToUnixTimeSeconds(), $rel,
    $file.Directory.Parent.Name, $file.Directory.Name, $pr, [Convert]::ToBase64String([IO.File]::ReadAllBytes($file.FullName)))
}
"""


def machine(host: str, after: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limit = (str(int(after.timestamp())), str(MAX_LOCAL_PER_HOST), str(MAX_XML))
    import shlex

    ssh = P.ssh(host)
    opts = P.hosts[host]["junit"]
    if P.hosts[host].get("windows"):
        opts = {
            "cert_xml": "out/certify/*/*/*.xml",
            "cert_log": "out/certify/*/*/run.log",
            **opts,
        }
        script = (
            WINDOWS_SCAN.replace("AFTER", limit[0])
            .replace("LIMIT", limit[1])
            .replace("MAXXML", limit[2])
            .replace("OPTIONS", base64.b64encode(json.dumps(opts).encode()).decode())
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        argv, script = (
            [*ssh, "powershell", "-NoProfile", "-EncodedCommand", encoded],
            "",
        )
    else:
        argv = ["python3", "-", *limit, json.dumps(opts)]
        if ssh:
            argv = [*ssh, P.hosts[host].get("shell_prefix", "") + shlex.join(argv)]
        script = POSIX_SCAN
    try:
        action = subprocess.run(
            argv, input=script.encode(), capture_output=True, timeout=110, check=True
        )
        lines = iter(action.stdout.splitlines())
        status = json.loads(next(lines))
        reports = []
        for line in lines:
            at, file, sha, lane, pr, data = json.loads(line)
            report_id = f"{host}:{file}:{at}"
            try:
                tests = cases(base64.b64decode(data))
            except (ValueError, ET.ParseError):
                continue
            if tests:
                reports.append(
                    {
                        "id": report_id,
                        "run": f"{host}:{Path(file).parent}:{int(at // 60)}",
                        "source": host,
                        "stage": "local",
                        "lane": lane,
                        "sha": sha,
                        "observed_sha": sha,
                        "pr": pr,
                        "at": stamp(datetime.fromtimestamp(at, UTC)),
                        "tests": tests,
                    }
                )
        return reports, {"source": host, "reports": len(reports), **status}
    except (OSError, subprocess.SubprocessError, StopIteration, ValueError) as exc:
        return [], {"source": host, "reports": 0, "error": type(exc).__name__}


def pick_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for stage in ("ready", "queue", "nightly"):
        items = sorted(
            (
                r
                for r in runs
                if hosted_stage(r) == stage
                and r["status"] == "completed"
                and not r["name"].lower().endswith("/ draft")
                and r["conclusion"] in {"success", "failure", "cancelled", "timed_out"}
            ),
            key=lambda r: r["created_at"],
        )
        if len(items) > MAX_HOSTED_RUNS_PER_STAGE:
            count = MAX_HOSTED_RUNS_PER_STAGE
            items = [items[i * (len(items) - 1) // (count - 1)] for i in range(count)]
        targets.extend(items)
    return targets


def refresh(*, local_only: bool = False) -> None:
    now = datetime.now(UTC)
    after = now - timedelta(days=WINDOW)
    REPORT_CACHE.mkdir(exist_ok=True)
    hosts = [name for name, spec in P.hosts.items() if spec.get("junit")]
    prior = load(REPORTS, {}) if local_only else {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        local = [pool.submit(machine, host, after) for host in hosts]
        targets = (
            [] if local_only else pick_runs(workflows_runs(sorted(HOSTED), after, now))
        )
        print(
            f"Selected {Counter(hosted_stage(r) for r in targets)} hosted workflows",
            flush=True,
        )
        hosted = list(pool.map(hosted_report, targets))
        evidence: list[dict[str, Any]] = []
        sources = []
        for job in local:
            reports, status = job.result()
            evidence.extend(reports)
            sources.append(status)
        evidence.extend(report for reports, _ in hosted for report in reports)
        evidence.extend(
            report
            for report in prior.get("reports", [])
            if report["source"] == "GitHub Actions"
        )
    errors = [error for _, error in hosted if error]
    sources.extend(
        [
            source
            for source in prior.get("sources", [])
            if source["source"] == "GitHub Actions"
        ]
        if local_only
        else [
            {
                "source": "GitHub Actions",
                "reports": sum(r["source"] == "GitHub Actions" for r in evidence),
                "selected": dict(Counter(hosted_stage(r) for r in targets)),
                "errors": len(errors),
            }
        ]
    )
    save(
        REPORTS,
        {
            "from": stamp(after),
            "to": stamp(now),
            "sources": sources,
            "reports": evidence,
        },
    )
    print(f"{len(evidence)} usable reports; {len(errors)} hosted errors", flush=True)
    for source in sources:
        print(
            {k: v if k != "layers" else len(v) for k, v in source.items()}, flush=True
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-only", action="store_true")
    refresh(local_only=parser.parse_args().local_only)
