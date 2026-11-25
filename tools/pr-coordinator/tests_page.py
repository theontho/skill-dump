"""Offline, self-contained CI inventory for the coordinator dashboard."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from runtime import P
from tests_inventory import CACHE, load
from tests_policy import DETAILS, STAGES, stage_workflows, workflow_map


def esc(value: object) -> str:
    return html.escape(str(value))


def show(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")


def table(title: str, headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    if not rows:
        return '<p class="empty">No observations in this snapshot.</p>'
    header = "".join(f'<th scope="col">{esc(h)}</th>' for h in headers)
    body = "".join(
        f'<tr><th scope="row">{row[0]}</th>{"".join(f"<td>{cell}</td>" for cell in row[1:])}</tr>'
        for row in rows
    )
    return f'<div class="scroll" tabindex="0" role="region" aria-label="{esc(title)}"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'


def stage_overview(spec: dict[str, Any]) -> str:
    lines = []
    highlights = P.get("tests", "stage_highlights", {})
    for key, (title, desc) in STAGES.items():
        if key == "other":
            continue
        files = stage_workflows(spec, key)
        workflows = "".join(
            f'<li><strong>{esc(spec[file]["name"])}</strong> <span class="muted">· {esc(", ".join(spec[file]["jobs"]))}</span></li>'
            for file in files
        )
        detail = f'<ul class="workflow-list">{workflows}</ul>' if workflows else ""
        lines.append(
            f'<details class="stage"><summary><strong>{esc(title)}</strong><span>{esc(desc)}</span>'
            f'<span class="muted stage-lanes">{esc(highlights.get(key, ""))}</span></summary>'
            f'<div class="inset"><p>{esc(DETAILS.get(key, desc))}</p>{detail}</div></details>'
        )
    return "".join(lines)


CHECK_COLUMNS = (
    "Check / what it does",
    "Wall min",
    "Runner min",
    "Runs/day",
    "Runner h",
    "Pass",
    "Fail",
    "Cancelled",
    "Skipped",
    "Signal",
)


def check_rows(
    items: list[dict[str, Any]], spec: dict[str, Any]
) -> list[tuple[str, ...]]:
    rows = []
    for row in sorted(items, key=lambda r: (-r["hours"], r["workflow"], r["name"])):
        out, signals = row["outcomes"], row["signals"]
        label = spec.get(row["workflow"], {}).get("name", row["workflow"])
        signal = " · ".join(
            f"{signals[k]} {label}"
            for k, label in (
                ("real", "likely real"),
                ("flaky", "flaky"),
                ("infra", "infra"),
                ("summary", "summary"),
                ("unknown", "unclassified"),
            )
            if signals.get(k)
        )
        if (
            not signal
            and not out.get("failure")
            and row["hours"] >= 2
            and row["allocated"] >= 5
        ):
            signal = '<span class="amber">Cost, no failures</span>'
        elif (
            signals.get("flaky", 0) >= 2
            and signals["flaky"] > sum(signals.values()) / 2
        ):
            signal = (
                f'<span class="amber">Mostly flaky</span><small>{esc(signal)}</small>'
            )
        else:
            signal = esc(signal or "—")
        if out.get("other"):
            signal += f"<small>{out['other']:,} other outcome(s)</small>"
        rows.append(
            (
                f"{esc(row['name'])}<small>{esc(label)} · {esc(row['why'])}</small>",
                show(row["wall"]),
                show(row["minutes"]),
                show(row["per_day"]),
                show(row["hours"]),
                *(
                    f"{out.get(key, 0):,}"
                    for key in ("success", "failure", "cancelled", "skipped")
                ),
                signal,
            )
        )
    return rows


def checks(data: dict[str, Any], spec: dict[str, Any]) -> str:
    result = []
    for stage, (label, _) in STAGES.items():
        rows = [row for row in data.get("jobs", []) if row["stage"] == stage]
        active = sum(row["allocated"] for row in rows)
        hours = sum(row["hours"] for row in rows)
        status = (
            f"{len(rows):,} families · {active:,} runner allocations · {show(hours)} h"
            if rows
            else "No measured runs"
        )
        result.append(
            f'<details class="group"><summary><strong>{esc(label)}</strong><span class="muted">{esc(status)}</span></summary>'
            f"{table(label, CHECK_COLUMNS, check_rows(rows, spec))}</details>"
        )
    return "".join(result)


def breakdown(data: dict[str, Any], title: str, key: str) -> str:
    result = []
    for row in data.get(key, []):
        out, signals = row["outcomes"], row["signals"]
        hosted = [
            f"{lane}: {row['lanes'][lane]:,}"
            for lane in ("ubuntu", "macos", "windows", "linux-arm64")
            if lane in row["lanes"]
        ]
        name = esc(row["name"])
        if key == "files":
            name += f"<small>{esc(row['area'])} · {esc(row['tier'])} · source {esc(row.get('changed') or 'not mapped')}</small>"
        else:
            name += f"<small>{row['files']:,} files in current tree</small>"
        signal_text = signals_text(signals, row["prs"])
        result.append(
            (
                name,
                f"{row['tests']:,}",
                f"{row['runs']:,}",
                f"{row.get('reports', 0):,}",
                " / ".join(f"{out.get(k, 0):,}" for k in ("pass", "fail", "skip")),
                show(row["seconds"] / 3600),
                show(row["median"]),
                esc(signal_text),
                esc(" · ".join(hosted) or "No hosted lane observed"),
            )
        )
        if key == "files" and len(result) == 20:
            break
    return table(
        title,
        (
            "Group",
            "Test IDs",
            "Case runs",
            "Reports",
            "Pass / Fail / Skip",
            "Case h",
            "Median s/report",
            "Failure signal",
            "Hosted OS observations",
        ),
        result,
    )


def signals_text(signals: dict[str, Any], prs: int) -> str:
    return (
        " · ".join(
            [
                f"{signals[k]} {label}"
                for k, label in (
                    ("real", "likely real"),
                    ("persistent", "repeat failure"),
                    ("flaky", "flaky"),
                    ("infra", "environment"),
                    ("unknown", "unclassified"),
                )
                if signals.get(k)
            ]
            + ([f"{prs} PRs"] if prs else [])
        )
        or "—"
    )


def candidate_list(suggestions: dict[str, Any]) -> str:
    result = []
    for item in suggestions.get("expensive", [])[:3]:
        result.append(
            f"<li><strong>Review {esc(item['name'])} ({esc(STAGES[item['stage']][0])}).</strong> {show(item['hours'])} runner hours in "
            f"{item['allocated']:,} measured allocations, no observed failures. Check unique coverage before moving it.</li>"
        )
    for item in suggestions.get("flaky", [])[:2]:
        flaky = item["signals"]["flaky"]
        result.append(
            f"<li><strong>Fix the noise in {esc(item['name'])} first.</strong> {flaky:,} of "
            f"{sum(item['signals'].values()):,} failed jobs passed again on the same SHA; "
            f"{show(item['hours'])} runner hours across all allocations.</li>"
        )
    move = suggestions.get("move", [])
    if move:
        overlap = ", ".join(move[0]["overlap"])
        result.append(
            f"<li><strong>Try moving {len(move)} already-nightly tests out of repeated pre-merge runs.</strong> "
            f"Up to {show(suggestions['move_hours'])} case-hours in this sample, before accounting for parallelism and shared fixtures. "
            f"Largest candidate: <code>{esc(move[0]['file'])}::{esc(move[0]['name'])}</code> "
            f"({move[0]['outcomes']['pass']:,} passes; also seen nightly on {esc(overlap)}).</li>"
        )
    return (
        "<ol>" + "".join(result) + "</ol>"
        if result
        else '<p class="empty">Not enough evidence to suggest a change yet.</p>'
    )


def source_cards(evidence: dict[str, Any]) -> str:
    rows = []
    for source in evidence.get("sources", []):
        status = f"Unavailable: {source['error']}" if source.get("error") else ""
        if "found" in source:
            status += f"{source['found']:,} XML files found; {source['logs']:,} logs (not individual test outcomes)."
        if "selected" in source:
            status += ", ".join(
                f"{n} {STAGES[k][0]}" for k, n in source["selected"].items()
            )
            status += f"; {source['errors']} artifact collection errors."
        rows.append((esc(source["source"]), f"{source['reports']:,}", esc(status)))
    return table("Evidence sources", ("Source", "Usable reports", "Coverage"), rows)


@lru_cache(maxsize=1)
def render(_version: int) -> str:
    data = load(CACHE, {})
    spec = data.get("policy") or workflow_map()
    evidence = data.get("evidence", {})
    at = data.get("activity_at") or ""
    before = evidence.get("from") or ""
    end = evidence.get("to") or ""
    stats = data.get("stats", {})
    tests = evidence.get("tests", [])
    total = evidence.get("totals", {})
    suggestions = data.get("suggestions", {})
    selections = {
        "testData": tests,
        "allFiles": evidence.get("files", []),
        "moveKeys": [[t["file"], t["name"]] for t in suggestions.get("move", [])],
    }
    freshness = (
        f'Jobs snapshot <time datetime="{esc(at)}">{esc(at[:10])} {esc(at[11:16])} UTC</time>'
        if at
        else "No jobs snapshot yet. Run <code>pr-coord tests --refresh</code>."
    )
    tokens = {
        "FRESH": freshness,
        "POLICY": f"Current rules from checkout <code>{esc(data.get('head', 'HEAD'))}</code>. "
        + esc(P.get("tests", "policy_note", "")),
        "STAGES": stage_overview(spec),
        "JOB_STATS": (
            f"{stats.get('records', 0):,} workflow records · {stats.get('pending', 0):,} still running · "
            f"{stats.get('missing', 0):,} completed records without available jobs · {data.get('job_errors', 0):,} failed attempt fetches."
            + (
                " Collection is still partial; costs shown are lower bounds."
                if data.get("partial")
                else ""
            )
        ),
        "HISTORICAL_OS_COST": (
            f'<p class="section-note">Older main-push OS suites account for {show(data["historical_main_os_hours"])} '
            "recorded runner hours in this window. Use the stage policy above to see when those suites now run.</p>"
            if data.get("historical_main_os_hours")
            else ""
        ),
        "CHECKS": checks(data, spec),
        "EVIDENCE": (
            f"{len(tests):,} distinct test IDs, {total.get('runs', 0):,} observations and {show(total.get('seconds', 0) / 3600)} "
            f"case-hours from {total.get('reports', 0):,} reports across {len(evidence.get('files', [])):,} files "
            f"({evidence.get('tracked_files', 0):,} test files in the current tree). "
            f"{esc(before[:10])} to {esc(end[:10])} UTC."
            if tests
            else "No pytest reports loaded yet."
        ),
        "SOURCES": source_cards(evidence),
        "TIERS": breakdown(evidence, "Pytest tiers", "tiers"),
        "AREAS": breakdown(evidence, "Pytest areas", "areas"),
        "SLOW": breakdown(evidence, "Slowest test files", "files"),
        "CANDIDATES": candidate_list(suggestions),
        "DATA": json.dumps(
            selections, ensure_ascii=True, separators=(",", ":")
        ).replace("<", "\\u003c"),
    }
    return substitute(TEMPLATE.replace("__PR_WEB__", P.web), tokens)


def substitute(template: str, tokens: dict[str, str]) -> str:
    return re.sub(r"@@([A-Z_]+)@@", lambda match: tokens[match[1]], template)


def page() -> str:
    try:
        version = CACHE.stat().st_mtime_ns
    except OSError:
        version = 0
    return render(version)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test inventory · Coordinator</title>
<style>
:root{color-scheme:dark;--bg:#111;--surface:#191919;--line:#353535;--text:#eee;--muted:#b5b5b5;--link:#a8caff;--amber:#f3ca86}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1400px;margin:auto;padding:22px clamp(16px,4vw,56px) 60px}
a{color:var(--link);text-underline-offset:3px}
:focus-visible{outline:2px solid var(--link);outline-offset:3px}
::selection{background:#304c72;color:#fff}
nav{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px;color:var(--muted)}
h1,h2,h3,p{margin:0}h1{font-size:clamp(29px,4vw,39px);line-height:1.15;letter-spacing:-.027em;text-wrap:balance}
h2{font-size:22px;letter-spacing:-.018em;margin-bottom:4px}h3{font-size:16px;margin:18px 0 6px}
p{max-width:76ch}.intro p{margin-top:7px}.fresh,.muted,small{color:var(--muted)}
.fresh{font-size:12px}.anchors{display:flex;gap:8px 18px;flex-wrap:wrap;margin-top:15px;font-size:13px}
section{margin-top:33px;scroll-margin-top:12px}
.section-note{margin:0 0 12px;color:var(--muted)}.section-note + .section-note{margin-top:-6px}
details{border:1px solid var(--line);background:var(--surface);margin:0;border-radius:7px}
details+details{border-top:none;border-top-left-radius:0;border-top-right-radius:0}
details:has(+details){border-bottom-left-radius:0;border-bottom-right-radius:0}
summary{cursor:pointer;padding:9px 14px;line-height:1.5;list-style:none;display:grid;grid-template-columns:17px minmax(155px,auto) 1fr;align-items:baseline;gap:8px}
summary::-webkit-details-marker{display:none}
summary::before{content:"";width:6px;height:6px;border:solid var(--muted);border-width:0 1.5px 1.5px 0;transform:rotate(-45deg);align-self:center;margin-left:3px}
details[open]>summary::before{transform:rotate(45deg) translateY(-2px)}
summary:hover{background:#222}
summary>span{min-width:0}
.stage summary{grid-template-columns:17px 180px minmax(220px,1fr) minmax(160px,.68fr);gap:8px 12px}
.stage-lanes{font-size:12px}.inset{padding:0 20px 15px 43px;color:var(--muted)}
.workflow-list{margin:8px 0 0;padding-left:18px;columns:2;column-gap:30px}.workflow-list li{break-inside:avoid;padding:1px}
.group summary>span{text-align:right}.scroll{overflow:auto;border-top:1px solid var(--line);scrollbar-color:#555 #1b1b1b}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid #2c2c2c;white-space:nowrap;vertical-align:top}
th:first-child,td:first-child{text-align:left;white-space:normal;overflow-wrap:anywhere;min-width:190px}
thead th{font-weight:600;color:var(--muted);background:#202020}
tbody th{font-weight:600}tr:last-child>*{border-bottom:0}tbody tr:hover{background:#242424}
td:last-child:not(:first-child){text-align:left}small{display:block;font-size:11.5px;font-weight:normal;white-space:normal}
code{font:12px/1.45 ui-monospace,monospace;overflow-wrap:anywhere}
.method{margin:10px 0 0;background:transparent}
.method>summary,.evidence>summary{grid-template-columns:17px 1fr}
.method p+p{margin-top:8px}.empty{color:var(--muted);padding:12px 15px}
.toolbar{margin:13px 0 9px;display:flex;gap:8px;align-items:end;flex-wrap:wrap}
.toolbar label{display:grid;gap:3px;color:var(--muted);font-size:12px}
.toolbar input,.toolbar select,button{font:inherit;color:var(--text);background:#202020;border:1px solid #454545;border-radius:5px;padding:6px 8px}
button{cursor:pointer}button:disabled{cursor:default;opacity:.55}.paging{display:flex;align-items:center;gap:8px;margin-top:9px}
.test-id{display:block;font-size:12px;max-width:52ch}.amber{color:var(--amber)}
ol{padding-left:22px;margin:8px 0 0;max-width:95ch}li+li{margin-top:6px}
@media(max-width:730px){
  .stage summary{grid-template-columns:13px minmax(0,1fr);gap:2px 10px}
  .stage summary>span{grid-column:2}.workflow-list{columns:1}.group summary{grid-template-columns:13px 1fr;gap:2px 8px}
  .group summary>span{grid-column:2;text-align:left}.inset{padding-left:35px}
}
</style></head><body><main>
<nav aria-label="Coordinator"><a href="/">PR backlog coordinator</a><a href="/lifecycle">PR lifecycle</a><span aria-current="page">Tests</span></nav>
<header class="intro"><h1>Test inventory</h1>
<p>See what runs before a change lands, where the time goes, and which checks have caught problems.</p>
<p class="fresh">@@FRESH@@</p>
<div class="anchors"><a href="#stages">When things run</a><a href="#checks">Checks by stage</a><a href="#pytest">Pytest breakdown</a><a href="#tests">Individual tests</a><a href="#candidates">Candidates to review</a></div>
</header>

<section id="stages"><h2>When things run</h2><p class="section-note">@@POLICY@@</p>@@STAGES@@</section>

<section id="checks"><h2>Checks by stage</h2>
<p class="section-note">The last seven days of hosted jobs and retained local completion logs. Open a stage to see cost, outcomes and failure signals.</p>
<p class="section-note">@@JOB_STATS@@</p>
@@HISTORICAL_OS_COST@@
@@CHECKS@@
<details class="method"><summary>How check cost and failures are counted</summary><div class="inset">
<p>Runs/day counts attempts that acquired a runner; outcomes also count skipped and runner-free attempts. Shards are combined by job family per attempt. Wall time is the first-to-last job span; runner minutes add the individual job durations. These are elapsed runner hours, not billing or queue wait. For outcomes, one failed shard makes the attempt fail; failure signals count the failed jobs, not their family verdicts.</p>
<p>Likely real: the same diagnostic repeated, or the same check passed only after a later head on the PR. Flaky: a later success on the same SHA. Infra: runner, timeout or setup evidence. Summary failures do not count as bugs, and missing evidence stays unclassified. Whole-workflow skips are counted from the Actions status without requesting each skipped job.</p>
<p>Local certification reports only retained outcomes. Local hours cover logs with an exact duration; streamed remote results often give the outcome but no elapsed time.</p>
</div></details></section>

<section id="pytest"><h2>Pytest breakdown</h2><p class="section-note">@@EVIDENCE@@</p>
<details class="evidence"><summary>Evidence coverage by source</summary>@@SOURCES@@</details>
<details class="evidence"><summary>Core and integration tiers</summary>@@TIERS@@</details>
<details class="evidence"><summary>Product areas and observed operating systems</summary>@@AREAS@@</details>
<details class="evidence"><summary>20 test files with the most cumulative test time</summary>@@SLOW@@</details>
<p class="fresh" style="margin-top:8px">These are summed case times, including repeats and fixture time. The group median is per JUnit report, not elapsed suite time. Portable cases normally run on Ubuntu or in generic certification; OS counts show observed executions.</p>
</section>

<section id="tests"><h2>Individual tests</h2>
<p class="section-note">Never failing is a prompt to review a test, not a reason to delete it. A rarely changed feature or platform-specific check can still need that protection.</p>
<div class="toolbar">
<label>View<select id="view"><option value="never">Never failed</option><option value="real">Caught likely bugs</option><option value="flaky">Flaky</option><option value="failed">Any failures</option><option value="move">Nightly move estimate</option><option value="all">All tests</option><option value="files">All test files</option></select></label>
<label>Find a test, file or area<input id="search" type="search" autocomplete="off" placeholder="e.g. alignment"></label>
</div>
<div class="scroll" tabindex="0" role="region" aria-label="Individual pytest results"><table>
<thead><tr><th scope="col">Test / area</th><th scope="col">Runs</th><th scope="col">Pass rate</th><th scope="col">Pass / Fail / Skip</th><th scope="col">Total min</th><th scope="col">Median s</th><th scope="col">Failure evidence</th></tr></thead>
<tbody id="results"></tbody></table></div>
<div class="paging"><button id="prev" type="button">Previous</button><button id="next" type="button">Next</button><span class="muted" id="count" role="status"></span></div>
<details class="method"><summary>What per-test failure evidence means</summary><div class="inset">
<p>Pass rate leaves out skips. A likely bug is a failed test followed by a pass on a different head of the same PR after excluding confirmed same-SHA flakes. It is an association, not proof of which commit fixed it. A matching repeat failure stays visible separately. Local work that rsynced uncommitted changes has no proven SHA and cannot establish a flake or identify a fix.</p>
<p>Hosted JUnit is a bounded sample of configured workflow stages and JUnit reports. Reruns can replace older artifacts; cached tests and runs without a report do not count as passes. Local certification and remote-testing XML contribute when retained. A source date is the most recent change on current main to a directly imported repository module, when it can be mapped; it is not a complete dependency history.</p>
</div></details></section>

<section id="candidates"><h2>Candidates to review</h2>
<p class="section-note">Suggestions only. Keep targeted coverage when the area changes and protect high-risk code and unique platforms.</p>@@CANDIDATES@@</section>
</main>
<script type="application/json" id="evidence-data">@@DATA@@</script>
<script>
const {testData, allFiles, moveKeys} = JSON.parse(document.querySelector('#evidence-data').textContent);
const view = document.querySelector('#view'), search = document.querySelector('#search');
const next = document.querySelector('#next'), prev = document.querySelector('#prev');
const results = document.querySelector('#results'), count = document.querySelector('#count');
const moveSet = new Set(moveKeys.map(key => JSON.stringify(key)));
const nf = new Intl.NumberFormat('en', {maximumFractionDigits: 2});
const labels = {real:'likely real', persistent:'repeat failure', flaky:'flaky', infra:'environment', unknown:'unclassified'};
let pageIndex = 0;
function selected() {
  const query = search.value.trim().toLowerCase();
  let list = view.value === 'files' ? allFiles : testData;
  if (view.value === 'never') list = list.filter(t => t.outcomes.pass && !t.outcomes.fail);
  if (view.value === 'real') list = list.filter(t => t.signals.real).sort((a,b) => b.prs - a.prs || b.signals.real - a.signals.real);
  if (view.value === 'flaky') list = list.filter(t => t.signals.flaky).sort((a,b) => b.signals.flaky - a.signals.flaky || b.seconds - a.seconds);
  if (view.value === 'failed') list = list.filter(t => t.outcomes.fail).sort((a,b) => b.outcomes.fail - a.outcomes.fail);
  if (view.value === 'move') list = list.filter(t => moveSet.has(JSON.stringify([t.file,t.name]))).sort((a,b) => b.premerge - a.premerge);
  return query ? list.filter(t => `${t.file || ''} ${t.name} ${t.area}`.toLowerCase().includes(query)) : list;
}
function draw() {
  const list = selected(), start = pageIndex * 25;
  results.replaceChildren();
  for (const t of list.slice(start, start + 25)) {
    const tr = document.createElement('tr'), th = document.createElement('th');
    th.scope = 'row';
    const code = document.createElement('code');
    code.className = 'test-id'; code.textContent = t.name; th.append(code);
    const meta = document.createElement('small');
    meta.textContent = [t.file, t.area, `source ${t.changed || 'not mapped'}`].filter(Boolean).join(' · '); th.append(meta);
    tr.append(th);
    const pass = t.outcomes.pass || 0, fail = t.outcomes.fail || 0, skip = t.outcomes.skip || 0;
    const signal = Object.entries(t.signals).map(([k,v]) => `${v} ${labels[k]}`);
    if (t.prs) signal.push(`${t.prs} distinct PRs`);
    if (view.value === 'never') signal.push(`Never failed in ${nf.format(pass)} executed runs`);
    if (view.value === 'move') signal.push(`${nf.format(t.premerge/60)} min on ${t.overlap.join(', ')} before merge`);
    for (const value of [nf.format(view.value === 'files' ? t.reports || 0 : t.runs), pass+fail ? `${nf.format(100*pass/(pass+fail))}%` : '—',
      [pass,fail,skip].map(n => nf.format(n)).join(' / '), nf.format(t.seconds/60), nf.format(t.median), signal.join(' · ') || '—']) {
      tr.insertCell().textContent = value;
    }
    if (t.pr_ids?.length) {
      const links = document.createElement('small');
      for (const pr of t.pr_ids) {
        const a = document.createElement('a');
        a.href = `__PR_WEB__/pull/${pr}`;
        a.textContent = `PR ${pr}`; links.append(a, ' ');
      }
      tr.lastElementChild.append(links);
    }
    results.append(tr);
  }
  if (!list.length) {
    const td = results.insertRow().insertCell();
    td.colSpan = 7; td.className = 'empty';
    td.textContent = !search.value && view.value === 'real'
      ? 'No later PR fix could be linked to a failing test in these reports.'
      : 'No matching observations in this snapshot.';
  }
  count.textContent = list.length ? `${nf.format(start+1)}–${nf.format(Math.min(start+25,list.length))} of ${nf.format(list.length)}` : '0 results';
  prev.disabled = pageIndex === 0; next.disabled = start+25 >= list.length;
}
function reset() { pageIndex = 0; draw(); }
view.addEventListener('change', reset); search.addEventListener('input', reset);
next.addEventListener('click', () => {pageIndex++; draw();});
prev.addEventListener('click', () => {pageIndex--; draw();});
draw();
</script></body></html>"""
