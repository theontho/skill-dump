"""Self-contained dark dashboard extension for the lifecycle snapshot."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from lifecycle_flow import overview
from lifecycle_flow_view import CSS, JS, MARKUP
from runtime import P


def file_version(name: str) -> tuple[Path, int]:
    path = P.data / name
    try:
        return path, path.stat().st_mtime_ns
    except OSError:
        return path, 0


def read(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=1)
def render_page(
    snapshot: tuple[Path, int], cache: tuple[Path, int], history: tuple[Path, int]
) -> str:
    data = read(snapshot[0])
    if data:
        data["flow"] = overview(data, read(cache[0]) or {}, read(history[0]) or {})
    return TEMPLATE.replace("__REPO_PATH__", P.repo.lower()).replace(
        "__SNAPSHOT__",
        json.dumps(data, separators=(",", ":")).replace("<", "\\u003c"),
        1,
    )


def page() -> str:
    return render_page(
        file_version("lifecycle.json"),
        file_version("lifecycle-cache.json"),
        file_version("lifecycle-coord-history.json"),
    )


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PR lifecycle · Coordinator</title>
<style>
:root{color-scheme:dark;--bg:#111;--panel:#191919;--line:#353535;--muted:#aaa;--text:#eee;--link:#9fc5ff;--red:#ff7775}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1680px;margin:auto;padding:20px clamp(14px,3vw,44px) 44px}a{color:var(--link);text-underline-offset:3px}
button{font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:5px;cursor:pointer}
button:hover{background:#292929}button:focus-visible,a:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--link);outline-offset:2px}
h1,h2,p{margin:0}h1{font-size:31px;letter-spacing:-.025em;font-weight:660;margin-top:9px}h2{font-size:19px;font-weight:650}
.muted,.subtitle{color:var(--muted)}.subtitle{margin:3px 0 17px;max-width:73ch}
.heading,.sectionhead,.legend,.switch,.waste{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.heading,.sectionhead{justify-content:space-between}.heading{align-items:baseline}#fresh{font-size:12px}
.surface{border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.summary{display:grid;grid-template-columns:minmax(0,1fr) minmax(370px,.8fr);margin-bottom:25px}.summary>div{padding:15px 18px}
.summary>div+div{border-left:1px solid var(--line)}.summary h2{margin-bottom:3px}
.stack{display:flex;height:17px;margin:15px 0 10px;background:#282828;border-radius:3px;overflow:hidden}
.stack span{height:100%}.legend{gap:5px 14px;font-size:12px;color:#d5d5d5}.legend span{display:inline-flex;gap:5px;align-items:center}
.key{width:10px;height:10px;display:inline-block;background:var(--phase);border-radius:2px;flex:none}
.key.wait,.stack .waiting{background:repeating-linear-gradient(135deg,var(--phase) 0 1px,#252525 1px 4px)}
.key.unknown,.stack .unknown{background:var(--phase);opacity:.75}.key.marker{background:var(--red);transform:rotate(45deg);border-radius:0;width:7px;height:7px;margin:0 3px}
.waste{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:9px;border-top:1px solid var(--line);margin-top:15px;padding-top:10px}
.waste span{color:var(--muted);font-size:12px}.waste strong{display:block;color:var(--text);font-size:17px}
.note{color:var(--muted);font-size:12px;margin-top:9px;max-width:76ch}.note summary{cursor:pointer}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:3px 0;border-bottom:1px solid #303030;text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:550}td:first-child,th:first-child{text-align:left}tr:last-child td{border-bottom:0}
.sectionhead{margin-bottom:5px}.switch{gap:4px}.switch button{padding:5px 10px}.switch [aria-pressed="true"]{background:#2b3b50;border-color:#6387b8}
.timelineLegend{margin:6px 0 10px}.timeline{overflow:auto;max-height:74vh;scrollbar-color:#555 #222}
.shell{min-width:760px}.line{display:grid;grid-template-columns: minmax(230px,25%) minmax(0,1fr);border-bottom:1px solid #303030}
.line:last-child{border-bottom:0}.line.axis{position:sticky;top:0;z-index:2;background:#232323}.line:hover:not(.axis){background:#202020}
.label{display:flex;align-items:center;gap:7px;min-width:0;min-height:35px;padding:4px 8px 4px 10px;
position:sticky;left:0;z-index:1;background:var(--panel);box-shadow:1px 0 0 #363636}
.axis .label{background:#232323}.line:hover:not(.axis) .label{background:#202020}
.label button{border-color:transparent;display:flex;align-items:center;min-width:0;gap:6px;text-align:left;padding:4px 3px;flex:1}
.label button svg{width:12px;height:12px;flex:none;fill:none;stroke:currentColor;stroke-width:1.7}.label button[aria-expanded="true"] svg{transform:rotate(90deg)}
.label .title,.name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.label b{font-weight:650;color:#f2f2f2}
.status{font-size:10px;text-transform:uppercase;letter-spacing:.045em;color:var(--muted);flex:none}.status.merged{color:#a2deb3}
.sub .label{padding-left:30px;font-size:11px;color:#bdbdbd;min-height:24px}.sub.group .label{color:#e7e7e7;font-weight:600;background:#222}
.slot{width:100%;display:block;align-self:center;overflow:hidden}.tick{stroke:#383838;stroke-width:1}
.ticklabel{fill:#c8c8c8;font:11px system-ui}.bar,.mark{cursor:help}.bar:hover,.bar:focus-visible,.mark:hover,.mark:focus-visible{stroke:#fff;stroke-width:1.5;outline:none}
.hit{fill:transparent;cursor:help}.mark.failure{fill:var(--red)}.mark.cancelled{fill:#ffae94}.mark.merged{fill:#7bd392}
.openpr{flex:none;text-decoration:none;font-size:12px}.openpr:hover{text-decoration:underline}
#tip{position:fixed;z-index:4;max-width:min(360px,calc(100vw - 16px));background:#252525;border:1px solid #575757;border-radius:6px;
box-shadow:0 5px 18px #0006;padding:9px 12px;pointer-events:none;font-size:12px;overflow-wrap:anywhere}
#tip strong,#tip span{display:block}#tip span{color:#c5c5c5}#tip a{pointer-events:auto}#tip.pinned{pointer-events:auto}
#empty,#errors{padding:12px 15px;color:var(--muted)}#errors{margin:10px 0;background:#261e18;border:1px solid #644935;border-radius:6px}
#errors summary{cursor:pointer;color:#f7d0a8}#errors ul{margin:6px 0;padding-left:24px;overflow-wrap:anywhere}#errors li{margin-bottom:3px}
.wasteline{grid-column:1/-1;padding:8px 12px 10px 30px;font-size:12px}.wasteline ul{margin:4px 0;padding-left:18px;columns:2;column-gap:25px}.wasteline li{break-inside:avoid;margin-bottom:3px}
@media(max-width:850px){.summary{grid-template-columns:1fr}.summary>div+div{border-left:0;border-top:1px solid var(--line)}.timeline{max-height:70vh}}
__FLOW_CSS__
</style></head>
<body><main>
<nav><a href="/">Coordinator dashboard</a> / Lifecycle</nav>
<div class="heading"><h1>PR lifecycle</h1><span id="fresh" class="muted"></span></div>
<p class="subtitle">See what ran, what waited, and what had to be repeated before a pull request merged.</p>
<details id="errors" hidden><summary id="errorTitle"></summary><ul></ul></details>
__FLOW_MARKUP__
<details class="trace-disclosure" id="detailedTraces"><summary>Detailed traces</summary>
<section class="summary surface" aria-labelledby="where">
  <div>
    <h2 id="where">Where the time goes</h2>
    <p id="scope" class="muted"></p>
    <div id="stack" class="stack" role="img" aria-label="Total pull-request hours by phase"></div>
    <div id="stackLegend" class="legend"></div>
    <div id="waste" class="waste"></div>
    <details class="note"><summary>How to read these numbers</summary>
      Totals use whole lifecycles for PRs merged in the last 48 hours. Each second is counted once per PR: merge queue,
      hosted checks, train refresh, certification, Copilot, conflict-free slot, PR checks, then unclassified time.
      A running job takes precedence over a waiting job in the same phase. Unclassified time is not measured developer work.
      Medians and p90s use only PRs with attributed time in that phase. Waste counts can overlap; a superseded run may have
      passed, and a rerun can be a partial retry. Older jobs and coordinator events may be unavailable.
    </details>
  </div>
  <div><h2>Time by phase</h2><table>
    <thead><tr><th scope="col">Phase</th><th scope="col">PRs</th><th scope="col">Median</th><th scope="col">p90</th></tr></thead>
    <tbody id="stats"></tbody>
  </table></div>
</section>
<section aria-labelledby="timelineTitle">
  <div class="sectionhead"><h2 id="timelineTitle">Timeline</h2>
    <div class="switch" role="group" aria-label="Timeline range">
      <button type="button" data-hours="6" aria-pressed="false">Last 6h</button>
      <button type="button" data-hours="24" aria-pressed="false">Last 24h</button>
      <button type="button" data-hours="48" aria-pressed="true">Last 48h</button>
    </div>
  </div>
  <p class="muted" id="range"></p>
  <div class="legend timelineLegend" id="timelineLegend"></div>
  <div class="surface timeline" id="timeline" role="region" aria-label="Scrollable pull request timeline" tabindex="0"><div class="shell" id="shell"></div></div>
  <p class="note">Click a PR to see individual jobs and run links. Select a bar or marker to keep its details open.
    Older starts extend off the left edge. GitHub often omits the first push; in that case the clock starts when the PR opened.</p>
</section>
</details>
</main>
<div id="tip" role="tooltip" hidden></div>
<script id="snapshot" type="application/json">__SNAPSHOT__</script>
<script>
"use strict";
const D=JSON.parse(document.getElementById("snapshot").textContent);
const $=id=>document.getElementById(id);
const NS="http://www.w3.org/2000/svg";
const PHASE={
  dev:["Dev / unclassified","#727782"],review:["Copilot","#be9bf2"],cert:["Local certification","#55c7be"],
  lane:["Conflict-free slot","#e8b364"],refresh:["Train refresh","#e89562"],checks:["PR check gate","#e2c982"],
  hosted:["Hosted checks","#79aaf4"],queue:["Merge queue","#c997db"],merged:["Merged","#7bd392"]
};
const COUNTS={cancelled_runs:"Cancelled runs",superseded_runs:"Superseded runs",superseded_certs:"Superseded certs",
              queue_removals:"Queue removals",reruns:"Rerun attempts"};
const H=3600, opened=new Set();
const now=D?.generated||Math.floor(Date.now()/1000);
let hours=48, left=now-hours*H, width=900, serial=0, pinned=null;
const fullTime=t=>new Intl.DateTimeFormat(undefined,{dateStyle:"medium",timeStyle:"long"}).format(t*1000);
const shortTime=t=>new Intl.DateTimeFormat(undefined,{weekday:"short",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(t*1000);
function el(tag, cls, text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!=null)node.textContent=text;return node}
function svg(tag, attrs={}){const node=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))node.setAttribute(k,v);return node}
function exact(seconds){
  seconds=Math.max(0,Math.round(seconds));
  const h=Math.floor(seconds/H), m=Math.floor(seconds%H/60), s=seconds%60;
  return [h?`${h}h`:"",m?`${m}m`:"",s||!seconds?`${s}s`:""].filter(Boolean).join(" ");
}
function short(seconds){return seconds>=H?`${(seconds/H).toFixed(1)}h`:seconds>=60?`${Math.round(seconds/60)}m`:`${Math.round(seconds)}s`}
function safe(url){
  try{const u=new URL(url);if(u.origin==="https://github.com"&&u.pathname.toLowerCase().startsWith("/__REPO_PATH__/"))return u.href}
  catch{}return "";
}
function link(url,text){
  const a=el("a","",text);a.href=safe(url)||"/";a.target="_blank";a.rel="noopener noreferrer";return a;
}
function key(text,color,type=""){
  const s=el("span"),g=el("i","key "+type);g.style.setProperty("--phase",color);s.append(g,document.createTextNode(text));return s;
}
function renderSummary(){
  const s=D?.summary;
  const stale=Date.now()/1000-now>15*60;
  $("fresh").textContent=D?`Updated ${fullTime(now)}${stale?" · snapshot may be stale":""}`:"No snapshot yet";
  if(D?.warnings?.length){
    $("errors").hidden=false;
    $("errorTitle").textContent=`Some GitHub timing data is missing or cached (${D.warnings.length} requests)`;
    for(const notice of D.warnings.slice(0,20))$("errors").querySelector("ul").append(el("li","",notice));
  }
  const total=s?.phases.reduce((v,p)=>v+p.running+p.waiting+p.unknown,0)||0;
  $("scope").textContent=s?.merged?`${s.merged} merged PRs · ${short(total)} total across full lifecycles`:"No merged PRs in the last 48h.";
  const labels=[];
  for(const row of s?.phases||[]){
    const [name,color]=PHASE[row.phase];
    const duration=row.running+row.waiting+row.unknown;
    labels.push(`${name}: ${short(duration)}`);
    for(const mode of ["running","waiting","unknown"]){
      if(!row[mode])continue;
      const b=el("span",mode);b.style.width=`${100*row[mode]/total}%`;
      b.style.setProperty("--phase",color);if(mode==="running")b.style.background=color;
      b.title=`${name} · ${mode} · ${exact(row[mode])}`;$("stack").append(b);
    }
    $("stackLegend").append(key(`${name} ${short(duration)}`,color));
    const tr=el("tr");
    for(const value of [name,row.prs,short(row.median),short(row.p90)])tr.append(el("td","",value));
    $("stats").append(tr);
  }
  $("stack").setAttribute("aria-label",`${short(total)} total PR-hours. ${labels.join(", ")}`);
  if(s?.merged)for(const [kind,label] of Object.entries(COUNTS)){
    const cell=el("span");cell.append(el("strong","",s.waste[kind]||0),document.createTextNode(label));$("waste").append(cell);
  }else $("waste").hidden=true;
  for(const name of Object.keys(PHASE).filter(k=>!["ready","unrecorded"].includes(k)))$("timelineLegend").append(key(...PHASE[name]));
  $("timelineLegend").append(key("Waiting","#c2c2c2","wait"),key("Failure / removal","#ff7775","marker"));
}
function tooltip(segment,anchor,hold=false){
  if(pinned&&!hold&&pinned!==anchor)return;
  const tip=$("tip");
  if(hold&&pinned===anchor){hide(true);return}
  if(hold)pinned=anchor;
  tip.classList.toggle("pinned",!!pinned);
  const end=segment.end??now, duration=exact(end-segment.start);
  const state=segment.stateText||(segment.status==="failed"?"Failed":segment.status==="cancelled"?"Cancelled":
    segment.activity==="unknown"?"Unclassified":segment.end==null?"Still "+segment.activity:
    segment.activity==="waiting"?"Waited":"Ran");
  tip.replaceChildren(el("strong","",segment.label),
    el("span","",`${PHASE[segment.phase][0]} · ${segment.phase==="merged"?"Merged":state} · ${duration}`),
    el("span","",fullTime(segment.start)));
  if(end!==segment.start||segment.end==null)tip.append(el("span","",(segment.end==null?"As of ":"Until ")+fullTime(end)));
  if(segment.note)tip.append(el("span","",segment.note));
  if(safe(segment.url))tip.append(link(segment.url,segment.job?"Open GitHub job":segment.run?"Open GitHub run":"Open GitHub details"));
  tip.hidden=false;
  const r=anchor.getBoundingClientRect();
  tip.style.left=`${Math.max(8,Math.min(r.left,innerWidth-tip.offsetWidth-8))}px`;
  tip.style.top=`${r.bottom+7+tip.offsetHeight>innerHeight?Math.max(8,r.top-7-tip.offsetHeight):r.bottom+7}px`;
}
function hide(force=false){
  if(force)pinned=null;
  if(!pinned)$("tip").hidden=true;
}
function interact(mark,segment){
  mark.classList.add("segment");
  mark.setAttribute("tabindex","0");mark.setAttribute("role","button");
  mark.setAttribute("aria-label",`${segment.label}, ${exact((segment.end??now)-segment.start)}`);
  mark.addEventListener("pointerenter",()=>tooltip(segment,mark));
  mark.addEventListener("pointerleave",()=>hide());
  mark.addEventListener("focus",()=>tooltip(segment,mark));
  mark.addEventListener("blur",()=>hide());
  mark.addEventListener("click",event=>{event.stopPropagation();tooltip(segment,mark,true)});
  mark.addEventListener("keydown",event=>{
    if(event.key==="Enter"||event.key===" "){event.preventDefault();tooltip(segment,mark,true)}
  });
}
function x(time){return (time-left)/(now-left)*width}
function ticks(){
  const delta=(hours===6?1:hours===24?3:6)*H, values=[];
  for(let t=Math.ceil(left/delta)*delta;t<=now;t+=delta)values.push(t);
  return values;
}
function visible(s){return s.start<=now&&(s.end??now)>=left}
function draw(items,{axis=false,tiny=false}={}){
  const h=axis?28:tiny?24:47, g=svg("svg",{class:"slot",width,height:h,viewBox:`0 0 ${width} ${h}`});
  const values=ticks();
  for(const t of values){
    const offset=x(t);g.append(svg("line",{class:"tick",x1:offset,y1:0,x2:offset,y2:h}));
    if(axis&&offset>=33&&offset<width-40){
      const label=svg("text",{class:"ticklabel",x:offset,y:18,"text-anchor":"middle"});label.textContent=shortTime(t);g.append(label);
    }
  }
  if(axis)return g;
  const defs=svg("defs"), patterns={};
  g.append(defs);
  for(const s of items.filter(visible).sort((a,b)=>Number(a.start===a.end)-Number(b.start===b.end))){
    const a=Math.max(0,Math.min(width,x(s.start))),b=Math.max(0,Math.min(width,x(s.end??now)));
    let fill=PHASE[s.phase][1];
    const positions={dev:7,review:12,cert:16,lane:21,refresh:21,checks:21,hosted:26,queue:40,merged:40};
    const lanes={"Linux x64":0,"Linux ARM64":2.6,"macOS":5.2,"Windows":7.8,"Other":10.4};
    const y=tiny?8:positions[s.phase]+(s.phase==="hosted"?(lanes[s.lane]||0):0);
    let mark;
    if(s.start===s.end){
      const type=s.phase==="merged"?"merged":s.status==="failed"?"failure":s.status==="cancelled"?"cancelled":"";
      const py=tiny?12:type==="failure"||type==="cancelled"?5:y+3;
      const cx=Math.min(width-5,Math.max(5,a)),size=type?4:2.8;
      mark=svg("path",{d:`M${cx},${py-size} l${size},${size} -${size},${size} -${size},-${size} Z`,class:`mark ${type}`,fill});
    }else{
      if(s.activity==="waiting"){
        if(!patterns[s.phase]){
          const id=`hatch${serial++}`;
          const p=svg("pattern",{id,width:5,height:5,patternUnits:"userSpaceOnUse",patternTransform:"rotate(35)"});
          p.append(svg("rect",{width:5,height:5,fill,opacity:.22}),svg("path",{d:"M0 0V5",stroke:fill,"stroke-width":2}));
          defs.append(p);patterns[s.phase]=id;
        }
        fill=`url(#${patterns[s.phase]})`;
      }
      mark=svg("rect",{class:"bar",x:Math.min(a,width-2),y,width:Math.min(width,Math.max(2,b-a)),
                       height:tiny?9:s.phase==="hosted"?2.5:4,rx:1,fill,opacity:s.activity==="unknown"?.8:1});
      if(s.status==="failed"||s.status==="cancelled"){
        mark.setAttribute("stroke",s.status==="failed"?"#ff7775":"#ffae94");mark.setAttribute("stroke-width","1");
      }
    }
    interact(mark,s);
    g.append(mark);
  }
  return g;
}
function row(label,graphic,cls=""){
  const line=el("div","line "+cls);line.append(label,graphic);return line;
}
function expand(pr,fragment){
  const buckets=new Map();
  for(const job of pr.jobs.filter(visible)){
    const key=(job.phase==="queue"?"Merge group":"Hosted")+" · "+job.lane;
    if(!buckets.has(key))buckets.set(key,new Map());
    const jobs=buckets.get(key);
    if(!jobs.has(job.job))jobs.set(job.job,[]);
    jobs.get(job.job).push(job);
  }
  for(const [name,group] of [...buckets].sort(([a],[b])=>a.localeCompare(b))){
    fragment.append(row(el("div","label",`${name} · ${group.size} jobs`),draw([],{tiny:true}),"sub group"));
    for(const spans of [...group.values()].sort((a,b)=>a[0].start-b[0].start)){
      const label=el("div","label"),job=spans[0],a=link(job.url,job.label);
      a.className="name";a.title=job.label;label.append(a);
      fragment.append(row(label,draw(spans,{tiny:true}),"sub jobrow"));
    }
  }
  const other=pr.segments.filter(s=>s.lane&&s.phase==="cert"&&visible(s));
  for(const lane of [...new Set(other.map(s=>s.lane))].sort()){
    fragment.append(row(el("div","label",`Local certification · ${lane}`),
                        draw(other.filter(s=>s.lane===lane),{tiny:true}),"sub"));
  }
  const block=el("div","wasteline");
  if(pr.waste.length){
    block.append(el("strong","",`Rework and queue exits (${pr.waste.reduce((n,e)=>n+e.count,0)})`));
    const list=el("ul");
    for(const event of [...pr.waste].sort((a,b)=>a.at-b.at)){
      const li=el("li"),label=COUNTS[event.kind].replace(/s$/,"");
      li.append(link(event.url,event.label),document.createTextNode(` · ${label}${event.count>1?` ×${event.count}`:""} · ${shortTime(event.at)}`));
      if(event.note)li.append(document.createTextNode(` · ${event.note}`));list.append(li);
    }
    block.append(list);
  }else block.append(el("span","muted",buckets.size||other.length?"No rework recorded.":"No individual jobs recorded in this window."));
  fragment.append(block);
}
function render(){
  hide(true);
  const shell=$("shell");
  width=Math.round(shell.clientWidth-Math.max(230,shell.clientWidth*.25));
  const prs=(D?.prs||[]).filter(pr=>pr.first<=now&&(pr.merged??now)>=left);
  $("range").textContent=`${prs.length} PRs · ${shortTime(left)} to ${shortTime(now)} · your local time`;
  const fragment=document.createDocumentFragment();
  fragment.append(row(el("div","label muted","Pull request"),draw([],{axis:true}),"axis"));
  if(!prs.length){
    const empty=el("p","",D?"No tracked PRs in this time range. Try a wider range.":"Waiting for the first collection. Reload this page in a few minutes.");
    empty.id="empty";fragment.append(empty);
  }
  for(const pr of prs){
    const expanded=opened.has(pr.number), label=el("div","label");
    const btn=el("button");btn.type="button";btn.dataset.pr=pr.number;btn.setAttribute("aria-expanded",expanded);
    const arrow=svg("svg",{viewBox:"0 0 12 12","aria-hidden":"true"});arrow.append(svg("path",{d:"M4 2l4 4-4 4"}));
    const title=el("span","title");title.append(el("b","",`#${pr.number} `),document.createTextNode(pr.title));
    btn.title=pr.title;btn.append(arrow,title);
    btn.addEventListener("click",()=>{
      if(opened.has(pr.number))opened.delete(pr.number);else opened.add(pr.number);
      render();document.querySelector(`[data-pr="${pr.number}"]`)?.focus({preventScroll:true});
    });
    const gh=link(pr.url,"GitHub");gh.className="openpr";
    label.append(btn,el("span",`status ${pr.state}`,pr.state),gh);
    fragment.append(row(label,draw(pr.segments),"prrow"));
    if(expanded)expand(pr,fragment);
  }
  shell.replaceChildren(fragment);
}
for(const btn of document.querySelectorAll("[data-hours]"))btn.addEventListener("click",()=>{
  hours=Number(btn.dataset.hours);left=now-hours*H;
  for(const other of document.querySelectorAll("[data-hours]"))other.setAttribute("aria-pressed",other===btn);
  render();
});
document.addEventListener("click",e=>{if(!$("tip").contains(e.target))hide(true)});
document.addEventListener("keydown",e=>{if(e.key==="Escape")hide(true)});
$("timeline").addEventListener("scroll",()=>hide(true),{passive:true});
addEventListener("scroll",()=>hide(true),{passive:true});
renderSummary();
__FLOW_JS__
$("detailedTraces").addEventListener("toggle",()=>{if($("detailedTraces").open)render();else hide(true)});
let sized=$("shell").clientWidth;
new ResizeObserver(()=>{const w=$("shell").clientWidth;if(w!==sized&&$("detailedTraces").open){sized=w;render()}}).observe($("shell"));
</script></body></html>"""

TEMPLATE = (
    HTML.replace("__FLOW_CSS__", CSS)
    .replace("__FLOW_MARKUP__", MARKUP)
    .replace("__FLOW_JS__", JS)
)
