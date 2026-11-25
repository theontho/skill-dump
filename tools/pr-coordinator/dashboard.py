"""PR-backlog coordinator dashboard: serves state.json and an answerable question queue."""

import html
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from secrets import compare_digest, token_urlsafe
from urllib.parse import parse_qs

import lifecycle_page
import tests_page
from runtime import P, questions

HERE = P.data
STATE = HERE / "state.json"
QUESTIONS = HERE / "questions.json"
ANSWER_TOKEN = token_urlsafe(32)
READ_ONLY = True

# GitHub Octicons (MIT): pull request states.
OCTICONS = {
    "open": (
        "#3fb950",
        "M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z",
    ),
    "draft": (
        "#8b949e",
        "M3.25 1A2.25 2.25 0 0 1 4 5.372v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.251 2.251 0 0 1 3.25 1Zm9.5 14a2.25 2.25 0 1 1 0-4.5 2.25 2.25 0 0 1 0 4.5ZM2.5 3.25a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0ZM3.25 12a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm9.5 0a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5ZM14 7.5a1.25 1.25 0 1 1-2.5 0 1.25 1.25 0 0 1 2.5 0Zm0-4.25a1.25 1.25 0 1 1-2.5 0 1.25 1.25 0 0 1 2.5 0Z",
    ),
    "merged": (
        "#a371f7",
        "M5.45 5.154A4.25 4.25 0 0 0 9.25 7.5h1.378a2.251 2.251 0 1 1 0 1.5H9.25A5.734 5.734 0 0 1 5 7.123v3.505a2.25 2.25 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.95-.218ZM4.25 13.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm8.5-4.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5ZM5 3.25a.75.75 0 1 0 0 .005V3.25Z",
    ),
    "queued": (
        "#d29922",
        "M3.75 4.5a1.25 1.25 0 1 0 0-2.5 1.25 1.25 0 0 0 0 2.5ZM3 7.75a.75.75 0 0 1 1.5 0v2.878a2.251 2.251 0 1 1-1.5 0Zm.75 5.75a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm5-7.75a1.25 1.25 0 1 1-2.5 0 1.25 1.25 0 0 1 2.5 0Zm5.75 2.5a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Zm-1.5 0a.75.75 0 1 0-1.5 0 .75.75 0 0 0 1.5 0Z",
    ),
    "closed": (
        "#f85149",
        "M3.25 1A2.25 2.25 0 0 1 4 5.372v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.251 2.251 0 0 1 3.25 1Zm9.5 5.5a.75.75 0 0 1 .75.75v3.378a2.251 2.251 0 1 1-1.5 0V7.25a.75.75 0 0 1 .75-.75Zm-2.03-5.273a.75.75 0 0 1 1.06 0l.97.97.97-.97a.748.748 0 0 1 1.265.332.75.75 0 0 1-.205.729l-.97.97.97.97a.751.751 0 0 1-.018 1.042.751.751 0 0 1-1.042.018l-.97-.97-.97.97a.749.749 0 0 1-1.275-.326.749.749 0 0 1 .215-.734l.97-.97-.97-.97a.75.75 0 0 1 0-1.06ZM2.5 3.25a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0ZM3.25 12a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm9.5 0a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z",
    ),
}


def octicon(state):
    color, path = OCTICONS[state]
    return (
        f"<svg title='{state}' width=16 height=16 viewBox='0 0 16 16' style='vertical-align:text-bottom' "
        f"fill='{color}'><title>{state}</title><path d='{path}'/></svg>"
    )


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def esc(value):
    return html.escape(str(value if value is not None else ""))


STAGE_COLORS = {
    "merged": "#2e7d32",
    "queued": "#1565c0",
    "dropped": "#b71c1c",
    "ready": "#1976d2",
    "certifying": "#6a1b9a",
    "review": "#ef6c00",
    "fixing": "#c62828",
    "conflict": "#b71c1c",
    "blocked": "#616161",
    "waiting": "#455a64",
    "todo": "#9e9e9e",
}


def graph_svg(g):
    """Layered SVG (queued/lane → waiting → conflicting → free) with hover/click details and a conflict matrix."""
    nodes = {x["n"]: x for x in g.get("nodes", [])}
    if not nodes:
        return "<p>No graph yet.</p>"
    conflicts = g.get("conflicts", [])
    conflicted = {c["a"] for c in conflicts} | {c["b"] for c in conflicts}
    waiting = {w["from"] for w in g.get("waits", [])}

    def col(n):
        x = nodes[n]
        if x.get("queue") or x.get("holder"):
            return 0
        if n in waiting:
            return 1
        return 2 if n in conflicted else 3

    cols = {}
    for n in sorted(nodes):
        cols.setdefault(col(n), []).append(n)
    W, H, CW, RH = 270, 38, 330, 50
    pos = {
        n: (20 + c * CW, 44 + i * RH)
        for c, ns in cols.items()
        for i, n in enumerate(ns)
    }
    height = 64 + max(len(v) for v in cols.values()) * RH
    out = [
        f"<div style='display:flex;gap:12px;align-items:flex-start'><svg id=pg width='{20 + 4 * CW}' height='{height}' "
        "style='background:#151515;border-radius:6px;flex:none'>",
        "<defs><marker id='arr' markerWidth='9' markerHeight='9' refX='9' refY='4.5' orient='auto'>"
        "<path d='M0,0 L9,4.5 L0,9 z' fill='#ef5350'/></marker></defs>",
    ]
    for i, title in enumerate(
        [
            "① In queue / holding lane",
            "② Waiting on ①",
            "③ Conflicts with others",
            "④ Free to land",
        ]
    ):
        out.append(
            f"<text x='{20 + i * CW}' y='24' fill='#bbb' font-size='14'>{title}</text>"
        )
    for w in g.get("waits", []):
        (x1, y1), (x2, y2) = pos[w["from"]], pos[w["to"]]
        out.append(
            f"<path class='wait' data-a='{w['from']}' data-b='{w['to']}' d='M{x1},{y1 + H / 2} C{x1 - 40},{y1 + H / 2} "
            f"{x2 + W + 40},{y2 + H / 2} {x2 + W},{y2 + H / 2}' fill='none' stroke='#ef5350' stroke-opacity='.55' "
            f"stroke-width='1.6' marker-end='url(#arr)'/>"
        )
    for n, (x, y) in pos.items():
        v = nodes[n]
        fill = (
            "#1565c0"
            if v.get("queue")
            else "#6a1b9a"
            if v.get("holder")
            else "#37474f"
            if v.get("draft")
            else "#2e3b2e"
        )
        stroke = "#ff9800" if P.priority in v.get("labels", []) else "#888"
        deg = sum(1 for c in conflicts if n in (c["a"], c["b"]))
        tag = (
            "queued"
            if v.get("queue")
            else "holds lane"
            if v.get("holder")
            else "draft"
            if v.get("draft")
            else "conflicts w/ main"
            if v.get("merge") == "DIRTY"
            else "ready"
        )
        out.append(
            f"<g class='node' data-n='{n}' style='cursor:pointer'>"
            f"<rect x='{x}' y='{y}' width='{W}' height='{H}' rx='6' fill='{fill}' stroke='{stroke}' stroke-width='2'/>"
            f"<text x='{x + 8}' y='{y + 16}' fill='#fff' font-size='13' font-weight='bold'>#{n} · {esc(tag)}"
            f"{' · ' + str(deg) + ' ✕' if deg else ''}</text>"
            f"<text x='{x + 8}' y='{y + 31}' fill='#ddd' font-size='12'>{esc(v['title'][:40])}</text></g>"
        )
    out.append(
        "</svg><div id=pgd class=card style='min-width:320px;max-width:420px;position:sticky;top:8px'>"
        "<b>Hover or click a PR</b><br><small>Shows what it waits for and every PR it conflicts with, with files.</small></div></div>"
    )
    data = {
        "titles": {n: v["title"] for n, v in nodes.items()},
        "conflicts": conflicts,
        "waits": g.get("waits", []),
    }
    out.append(
        "<script>const PG="
        + json.dumps(data).replace("</", "<\\/")
        + """;
(function(){const svg=document.getElementById('pg'),det=document.getElementById('pgd');let pinned=null;
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function show(n){const rel=new Set([n]);let h='<b>#'+n+'</b> '+esc(PG.titles[n]||'')+'<br>';
 const w=PG.waits.filter(x=>x.from==n).map(x=>'#'+x.to);if(w.length)h+='<p><b style=color:#ef5350>Waits for</b> '+w.join(', ')+'</p>';
 const wb=PG.waits.filter(x=>x.to==n).map(x=>'#'+x.from);if(wb.length)h+='<p><b>Blocking</b> '+wb.join(', ')+'</p>';
 const cs=PG.conflicts.filter(c=>c.a==n||c.b==n);h+='<p><b>Conflicts ('+cs.length+')</b></p><ul style=padding-left:16px>';
 for(const c of cs){const o=c.a==n?c.b:c.a;rel.add(o);h+='<li><a href="__REPO_WEB__/pull/'+o+'">#'+o+'</a> '+esc((PG.titles[o]||'').slice(0,50))+'<br><small>'+c.files.map(esc).join('<br>')+'</small></li>';}
 h+='</ul><a href="__REPO_WEB__/pull/'+n+'">Open #'+n+' on GitHub</a>';det.innerHTML=h;
 svg.querySelectorAll('.node').forEach(g=>g.style.opacity=rel.has(+g.dataset.n)?1:.25);
 svg.querySelectorAll('.wait').forEach(p=>{const on=p.dataset.a==n||p.dataset.b==n;p.style.strokeOpacity=on?1:.08;p.style.strokeWidth=on?3:1.6;});}
function clear(){svg.querySelectorAll('.node').forEach(g=>g.style.opacity=1);svg.querySelectorAll('.wait').forEach(p=>{p.style.strokeOpacity=.55;p.style.strokeWidth=1.6;});}
svg.querySelectorAll('.node').forEach(g=>{g.addEventListener('mouseenter',()=>{if(!pinned)show(+g.dataset.n);});
 g.addEventListener('mouseleave',()=>{if(!pinned)clear();});
 g.addEventListener('click',()=>{const n=+g.dataset.n;if(pinned==n){pinned=null;clear();}else{pinned=n;show(n);}});});})();</script>"""
    )
    # conflict matrix
    ns = sorted(n for n in nodes if n in conflicted)
    cmap = {(min(c["a"], c["b"]), max(c["a"], c["b"])): c["files"] for c in conflicts}
    m = [
        "<details style='margin-top:8px'><summary><b>Conflict matrix</b> (hover a cell for files)</summary>"
        "<table style='width:auto;font-size:12px'><tr><th></th>"
        + "".join(f"<th>#{n}</th>" for n in ns)
        + "</tr>"
    ]
    for r in ns:
        m.append(f"<tr><th title='{esc(nodes[r]['title'])}'>#{r}</th>")
        for c in ns:
            f = cmap.get((min(r, c), max(r, c)))
            m.append(
                "<td style='background:#2a2a2a'></td>"
                if r == c
                else f"<td title='{esc(chr(10).join(f))}' style='background:#b71c1c;text-align:center'>{len(f)}</td>"
                if f
                else "<td></td>"
            )
        m.append("</tr>")
    m.append("</table></details>")
    return "".join(out) + "".join(m)


def runners_panel(s):
    u = s.get("runners")
    if not u:
        return "<p>No runner data yet.</p>"
    oses = sorted(set(u["running"]) | set(u["waiting"]))
    rows = "".join(
        f"<tr><td>{esc(o)}</td><td>{u['running'].get(o, 0)}</td><td>{u['waiting'].get(o, 0)}</td></tr>"
        for o in oses
    )
    kinds = (
        ", ".join(
            f"{esc(k)}: {v}"
            for k, v in sorted(u["by_kind"].items(), key=lambda x: -x[1])
        )
        or "none"
    )
    waits = (
        ", ".join(f"{esc(k)} {v} min" for k, v in u["oldest_wait_min"].items())
        or "none"
    )
    hist = s.get("runner_history", [])[-120:]
    spark = ""
    if hist:
        W, H = 480, 60
        limit = P.get("dashboard", "runner_limit", 0)
        mx = max(max(r + w for _, r, w in hist), limit, 1)
        pts = lambda idx: " ".join(
            f"{i * W / max(len(hist) - 1, 1):.1f},{H - h[idx] * H / mx:.1f}"
            for i, h in enumerate(hist)
        )
        spark = (
            f"<svg width={W} height={H + 14} style='background:#151515'>"
            + (
                f"<line x1=0 x2={W} y1={H - limit * H / mx:.1f} y2={H - limit * H / mx:.1f} stroke='#555' stroke-dasharray='4,3'/>"
                f"<text x=4 y={max(10, H - limit * H / mx - 3):.1f} fill='#888' font-size=10>{limit} runner limit</text>"
                if limit
                else ""
            )
            + f"<polyline fill=none stroke='#66bb6a' stroke-width=2 points='{pts(1)}'/><polyline fill=none stroke='#ef5350' stroke-width=2 points='{pts(2)}'/>"
            f"<text x=4 y={H + 12} fill='#888' font-size=10>{esc(hist[0][0])} → {esc(hist[-1][0])}: green running, red waiting</text></svg>"
        )
    return (
        f"<div class=grid><div class=card><b>Hosted runner jobs</b> <small>as of {esc(u.get('at'))}</small>"
        f"<table style='width:auto'><tr><th>OS</th><th>Running</th><th>Waiting</th></tr>{rows}"
        f"<tr><th>Total</th><th>{sum(u['running'].values())}</th><th>{sum(u['waiting'].values())}</th></tr></table>"
        f"<div><small>Running by kind: {kinds}</small></div><div><small>Longest wait: {waits}</small></div></div>"
        f"<div class=card>{spark}</div></div>"
    )


def page():
    s = load(STATE, {})
    qs = load(QUESTIONS, [])
    triage = {}
    try:
        for line in (HERE / "triage.jsonl").read_text().splitlines():
            t = json.loads(line)
            triage[t["n"]] = t
    except (OSError, ValueError):
        pass
    repo = P.web

    def pr_row(i, pr):
        t = triage.get(pr.get("number"), {})
        stage = pr.get("stage", "")
        color = STAGE_COLORS.get(stage, "#555")
        if stage in ("merged", "closed"):
            icon = octicon(stage)
        elif t.get("queue"):
            icon = octicon("queued")
        elif t:
            icon = octicon("draft" if t.get("draft") else "open")
        else:
            icon = ""
        gh = []
        if t and stage not in ("merged", "closed"):
            gh.append((t.get("merge") or "").lower())
            if t.get("queue"):
                gh.append("queue: " + t["queue"].lower())
            if t.get("unresolved"):
                gh.append(f"{t['unresolved']} open threads")
        issues = " ".join(
            f"<a href='{repo}/issues/{n}'>#{n}</a>" for n in t.get("issues", [])
        )
        return (
            f"<tr><td>{i}.</td><td><a href='{repo}/pull/{esc(pr.get('number'))}'>#{esc(pr.get('number'))}</a></td>"
            f"<td>{issues}</td><td>{icon} {esc(pr.get('title'))}</td><td><span class=tag style='background:{color}'>{esc(stage)}</span></td>"
            f"<td><small>{esc(', '.join(x for x in gh if x))}</small></td>"
            f"<td>{esc(pr.get('machine'))}</td><td>{esc(pr.get('note'))}</td></tr>"
        )

    def bucket(pr):
        t = triage.get(pr.get("number"), {})
        if pr.get("stage") in ("merged", "closed"):
            return "done"
        if t.get("queue"):
            return "queue"
        if not t:
            return "unknown"
        if t.get("draft"):
            return "draft"
        if t.get("merge") == "DIRTY":
            return "conflict"
        return "ready"

    groups = [
        ("queue", "In merge queue"),
        ("ready", "Ready (open, not draft)"),
        ("conflict", "Ready but conflicting with main"),
        ("draft", "Draft"),
        ("unknown", "Other"),
    ]
    by = {}
    for pr in s.get("prs", []):
        by.setdefault(bucket(pr), []).append(pr)
    head = "<tr><th></th><th>PR</th><th>Issue</th><th>Title</th><th>Stage</th><th>GitHub</th><th>Machine</th><th>Note</th></tr>"
    rows = []
    i = 0
    for key, label in groups:
        prs = by.get(key, [])
        if not prs:
            continue
        rows.append(
            f"<tr><th colspan=8 style='padding-top:14px;color:#8ab4f8'>{label} ({len(prs)})</th></tr>"
        )
        for pr in prs:
            i += 1
            rows.append(pr_row(i, pr))
    done = sorted(by.get("done", []), key=lambda p: -p["number"])
    done_rows = "".join(pr_row(j, pr) for j, pr in enumerate(done, 1))
    pr_tables = (
        f"<table>{head}{''.join(rows)}</table>"
        f"<details><summary><b>Merged / closed ({len(done)})</b></summary><table>{head}{done_rows}</table></details>"
    )
    host_names = {host.get("display", key): key for key, host in P.hosts.items()}
    live = s.get("hosts", {})

    def live_rows(m):
        h = live.get(host_names.get(m.get("name"), m.get("name")), {})
        if not h:
            return ""
        if not h.get("reachable"):
            return "<div><small>unreachable</small></div>"
        rows = "".join(
            f"<div><span class=tag style='background:{'#2e7d32' if r['state'] in ('held', 'running') else '#455a64'}'>"
            f"{esc(r['state'])}</span> {esc(r['label'])} <small>{esc(r['cores'])} cores"
            f"{(' · ' + esc(r['mem'])) if r.get('mem') else ''} · {esc(r['minutes'])}{' min' if r.get('id') else ''}</small></div>"
            for r in h.get("leases", [])
        )
        return (
            rows or "<div><small>no leases / heavy processes</small></div>"
        ) + f"<div><small>{esc(h.get('summary'))}</small></div>"

    machines = "".join(
        f"<div class=card><b>{esc(m.get('name'))}</b> <small>{esc(m.get('os'))}</small><br>"
        f"<span class=tag style='background:{'#2e7d32' if m.get('status') == 'busy' else '#757575'}'>{esc(m.get('status'))}</span>"
        f"<div>{esc(m.get('task'))}</div>{live_rows(m)}</div>"
        for m in s.get("machines", [])
    )
    cert_colors = {
        "pass": "#2e7d32",
        "fail": "#c62828",
        "error": "#ef6c00",
        "running": "#6a1b9a",
        "queued": "#455a64",
        "skipped": "#9e9e9e",
        "superseded": "#9e9e9e",
    }
    certs = s.get("certs", [])
    hosts = {}
    for c in certs:
        if c.get("status") in ("queued", "running"):
            hosts.setdefault(c.get("host") or "?", []).append(c)
    cert_hosts = (
        "".join(
            f"<div class=card><b>{esc(h)}</b><br>"
            + "".join(
                f"<div><span class=tag style='background:{cert_colors.get(c['status'], '#555')}'>{esc(c['status'])}</span> "
                f"#{esc(c['pr'])} {esc(c['target'])} <small>{esc(c.get('sha'))} since {esc(c.get('at'))}</small></div>"
                for c in sorted(cs, key=lambda c: c["status"] != "running")
            )
            + "</div>"
            for h, cs in sorted(hosts.items())
        )
        or "<p>No certification running.</p>"
    )
    recent = "".join(
        f"<tr><td>#{esc(c['pr'])}</td><td>{esc(c['target'])}</td><td>{esc(c.get('sha'))}</td>"
        f"<td><span class=tag style='background:{cert_colors.get(c['status'], '#555')}'>{esc(c['status'])}</span></td><td>{esc(c.get('at'))}</td></tr>"
        for c in sorted(certs, key=lambda c: c.get("at") or "", reverse=True)[:30]
    )
    agent_colors = {
        "working": "#1565c0",
        "waiting": "#455a64",
        "done": "#2e7d32",
        "blocked": "#c62828",
    }

    def agent_row(g):
        return (
            f"<tr><td>{esc(g['name'])}</td><td><span class=tag style='background:{agent_colors.get(g.get('status'), '#555')}'>{esc(g.get('status'))}</span></td>"
            f"<td>{esc(g.get('pr'))}</td><td>{esc(g.get('task'))}</td><td>{esc(g.get('machine'))}</td><td><small>{esc(g.get('at'))}</small></td></tr>"
        )

    agent_groups = [
        ("working", "Working"),
        ("waiting", "Waiting (CI, review, lane, certification)"),
        ("blocked", "Blocked"),
    ]
    all_agents = sorted(s.get("agents", []), key=lambda g: g["name"])
    agents = ""
    for key, label in agent_groups:
        gs = [g for g in all_agents if g.get("status") == key]
        if gs:
            agents += (
                f"<tr><th colspan=6 style='padding-top:14px;color:#8ab4f8'>{label} ({len(gs)})</th></tr>"
                + "".join(map(agent_row, gs))
            )
    done_agents = sorted(
        (g for g in all_agents if g.get("status") == "done"),
        key=lambda g: g.get("at") or "",
        reverse=True,
    )
    agents_done = "".join(map(agent_row, done_agents))
    agents_done_count = len(done_agents)
    runs = "".join(
        f"<tr><td>{esc(r.get('host'))}</td><td>{esc(r.get('label'))}</td><td><small>{esc(r.get('cmd'))}</small></td>"
        f"<td>{esc(r.get('status'))}</td><td><small>{esc(r.get('started'))}</small></td></tr>"
        for r in reversed(s.get("runs", [])[-12:])
    )
    open_q = [q for q in qs if not q.get("answer")]
    done_q = [q for q in qs if q.get("answer")]
    qhtml = (
        "".join(
            f"<div class='card q'><div><b>Q{esc(q['id'])}</b> <small>{esc(q.get('asked'))}</small></div>"
            f"<p>{esc(q['question'])}</p>"
            + (
                f"<p><i>Default if unanswered: {esc(q.get('default'))}</i></p>"
                if q.get("default")
                else ""
            )
            + (
                ""
                if READ_ONLY
                else (
                    f"<form method=post action=/answer><input type=hidden name=id value='{esc(q['id'])}'>"
                    f"<input type=hidden name=token value='{ANSWER_TOKEN}'>"
                    "<textarea name=answer rows=2></textarea><button>Answer</button></form>"
                )
            )
            + "</div>"
            for q in open_q
        )
        or "<p>No open questions.</p>"
    )
    dq = "".join(
        f"<li><b>Q{esc(q['id'])}</b> {esc(q['question'])}<br>&rarr; <i>{esc(q['answer'])}</i></li>"
        for q in done_q[-10:]
    )
    log = "".join(
        f"<li><small>{esc(t)}</small> {esc(m)}</li>"
        for t, m in reversed(s.get("log", [])[-40:])
    )
    counts = {}
    for pr in s.get("prs", []):
        if pr.get("stage") in ("merged", "closed"):
            continue
        counts[pr.get("stage")] = counts.get(pr.get("stage"), 0) + 1
    summary = " · ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    return f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=60>
<meta name=viewport content='width=device-width,initial-scale=1'><title>PR backlog coordinator</title>
<style>body{{font:14px system-ui;margin:16px;background:#111;color:#ddd}}a{{color:#8ab4f8}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #333;padding:4px 6px;text-align:left;vertical-align:top}}
.tag{{color:#fff;padding:1px 6px;border-radius:8px;font-size:12px;white-space:nowrap}}.grid{{display:flex;gap:8px;flex-wrap:wrap}}
.card{{background:#1c1c1c;border:1px solid #333;border-radius:6px;padding:8px;min-width:220px;flex:1}}
.q{{border-color:#ef6c00}}textarea{{width:100%;background:#000;color:#ddd}}h2{{margin-top:22px}}</style></head><body>
<h1>PR backlog coordinator</h1><nav><a href="/lifecycle">PR lifecycle</a> · <a href="/tests">Tests</a></nav><div>Updated {esc(s.get("updated"))} — {esc(s.get("headline"))}</div><div>{esc(summary)}</div>
<h2>PR dependency graph</h2><div><small>red arrow = waits for (conflicts with a queued PR or lane holder); ✕ = number of PRs it conflicts with; orange border = priority. Hover/click a PR for details. Updated {esc(s.get("graph", {}).get("at"))}</small></div>
<div style='overflow-x:auto'>{graph_svg(s.get("graph", {}))}</div>
<h2>Questions for you ({len(open_q)})</h2>{qhtml}
<h2>Active work</h2><table><tr><th>Agent</th><th>Status</th><th>PR / issue</th><th>Doing</th><th>Runs on</th><th>Updated</th></tr>{agents}</table>
<details><summary><b>Finished ({agents_done_count})</b></summary><table><tr><th>Agent</th><th>Status</th><th>PR / issue</th><th>Result</th><th>Ran on</th><th>Updated</th></tr>{agents_done}</table></details>
<details><summary>Remote test runs</summary><table><tr><th>Host</th><th>For</th><th>Command</th><th>Status</th><th>Started</th></tr>{runs}</table></details>
<h2>GitHub Actions runners</h2>{runners_panel(s)}
<h2>Machines</h2><div><small>live leases/processes as of {esc(s.get("hosts_at"))}</small></div><div class=grid>{machines}</div>
<h2>Certification queue (one run per host)</h2><div class=grid>{cert_hosts}</div>
<details><summary>Recent certification results</summary><table><tr><th>PR</th><th>OS</th><th>SHA</th><th>Result</th><th>At</th></tr>{recent}</table></details>
<h2>Pull requests</h2>{pr_tables}
<h2>Answered</h2><ul>{dq}</ul><h2>Log</h2><ul>{log}</ul></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/state.json":
            self._send(
                200,
                json.dumps(
                    {"state": load(STATE, {}), "questions": load(QUESTIONS, [])}
                ),
                "application/json",
            )
        elif self.path == "/lifecycle":
            self._send(200, lifecycle_page.page())
        elif self.path == "/tests":
            self._send(200, tests_page.page())
        elif self.path == "/":
            self._send(200, page().replace("__REPO_WEB__", P.web))
        else:
            self._send(404, "not found")

    def do_POST(self):
        if self.path != "/answer" or READ_ONLY:
            return self._send(404, "not found")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 20000:
                return self._send(400, "invalid answer length")
            form = parse_qs(self.rfile.read(length).decode())
        except ValueError:
            return self._send(400, "invalid answer")
        if not compare_digest(
            form.get("token", [""])[0].encode(), ANSWER_TOKEN.encode()
        ):
            return self._send(403, "invalid answer token")
        qid, answer = form.get("id", [""])[0], form.get("answer", [""])[0].strip()
        if not answer:
            return self._send(400, "answer is required")

        def update(qs):
            for q in qs:
                if str(q["id"]) == qid and not q.get("answer"):
                    q["answer"] = answer
                    q["answered_at"] = time.strftime("%Y-%m-%d %H:%M")
            return qs

        questions(update)
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    import argparse

    global READ_ONLY
    parser = argparse.ArgumentParser(
        description="Serve all coordinator dashboard pages."
    )
    parser.add_argument("--bind", default=P.get("dashboard", "host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=P.get("dashboard", "port", 8787))
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=not P.get("dashboard", "allow_answers", False),
    )
    args = parser.parse_args()
    READ_ONLY = args.read_only
    with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
        print(f"Dashboard: http://{args.bind}:{server.server_port}", flush=True)
        server.serve_forever()
