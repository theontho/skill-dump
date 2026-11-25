"""Markup, styles, and interaction for the lifecycle overview."""

CSS = """
.flow-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.flow-totals{display:grid;grid-template-columns:1fr 1fr;margin:10px 0 14px}
.flow-total{padding:11px 15px}.flow-total+.flow-total{border-left:1px solid var(--line)}
.flow-total strong{font-size:20px;font-weight:650}.flow-total span{color:var(--muted);font-size:12px;margin-left:8px}
.flow-total p{font-size:12px;margin-top:2px}.flow-total h3{margin:0;font-size:13px}
.flow-scroll{overflow:auto;scrollbar-color:#555 #222;padding-bottom:3px}.flow-canvas{min-width:1225px}
.flow-stages{list-style:none;display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:13px;padding:0;margin:0}
.flow-stages li{position:relative;display:flex;flex-direction:column;min-width:0}
.flow-card{flex:1;border:1px solid #404040;border-top:2px solid var(--stage);border-radius:6px;background:var(--panel);padding:7px 8px}
.flow-card h3{margin:0 0 6px;font-weight:650;font-size:13px;line-height:1.2;min-height:2.4em}
.flow-card dl{margin:0;font-size:12px;display:grid;grid-template-columns:auto auto;gap:0 3px}
.flow-card dt{color:var(--muted)}.flow-card dd{text-align:right;margin:0;font-weight:650;white-space:nowrap}
.flow-card p{border-top:1px solid var(--line);padding-top:4px;margin-top:4px;color:#ccc;font-size:11px}
.flow-arrow{position:absolute;width:13px;height:14px;top:43px;right:-13px;stroke:#a7a7a7;fill:none}
.flow-explain{font-size:11px;color:var(--muted);line-height:1.35;margin:5px 2px 0;min-height:4.1em}
.flow-returns{position:relative;height:65px;margin-top:1px;color:#f4be93}.flow-returns svg{display:block;width:100%;height:100%}
.flow-returns .belt{fill:none;stroke:#ba825c;stroke-width:1.2;stroke-dasharray:4 3;vector-effect:non-scaling-stroke}
.flow-return{position:absolute;transform:translateX(-50%);margin:0;background:var(--bg);border:1px solid #58453a;border-radius:4px;
padding:2px 7px;white-space:nowrap;font-size:11px;line-height:1.4;color:#f1c8a9}
.flow-return strong{color:#fff;font-weight:670}
#lifecycleFlow{margin-bottom:20px}.flow-notes{margin-top:5px;max-width:none}
#stageStrip{margin:0 0 18px}.strip-head{margin-bottom:7px}.strip-viewport{max-height:360px;overflow:auto;scrollbar-color:#555 #222}
.strip-row{display:grid;grid-template-columns:minmax(160px,22%) minmax(105px,13%) minmax(230px,1fr) minmax(64px,7%);gap:10px;
align-items:center;padding:5px 10px;min-width:640px;border-bottom:1px solid #303030;min-height:31px;font-size:12px}
.strip-row:last-child{border-bottom:0}.strip-row:hover:not(.strip-column){background:#202020}
.strip-column{position:sticky;top:0;z-index:1;background:#232323;color:var(--muted);font-size:11px}
.strip-now{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#c6c6c6}.strip-title{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.strip-title a{text-decoration:none;font-weight:650;margin-right:4px}.strip-title a:hover{text-decoration:underline}
.strip-track{position:relative;display:flex;align-items:stretch;min-width:0;height:17px;background:#323232;border-radius:3px}
.strip-piece{min-width:0;overflow:hidden;display:grid;place-items:center;height:100%;padding:0;border:0;border-radius:0;background:var(--stage);
color:#121212;font-size:10px;font-weight:750;white-space:nowrap;cursor:help;container-type:inline-size;flex-shrink:0}
.strip-piece:hover{background:var(--stage);filter:brightness(1.14)}.strip-piece:focus-visible{z-index:1}
.strip-piece span{display:none}@container (min-width:47px){.strip-piece span{display:block}}
.strip-piece.unrecorded{background:repeating-linear-gradient(135deg,#646772 0 1px,#393b42 1px 4px);color:#fff}
.strip-mark{position:absolute;z-index:1;width:6px;left:clamp(3px,var(--position),calc(100% - 3px));top:50%;height:6px;
transform:translate(-50%,-50%) rotate(45deg);border-radius:0;padding:0;background:var(--stage);border:1px solid #151515;cursor:help}
.strip-mark:hover{background:var(--stage);filter:brightness(1.2)}
.strip-row> :last-child{text-align:right}.trace-disclosure{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.trace-disclosure>summary{cursor:pointer;font-size:16px;font-weight:650;margin-bottom:12px}
@media(max-width:640px){.flow-totals{grid-template-columns:1fr}.flow-total+.flow-total{border-left:0;border-top:1px solid var(--line)}}
"""

MARKUP = """
<section id="lifecycleFlow" aria-labelledby="flowTitle">
  <div class="flow-head"><h2 id="flowTitle">Typical path to merge</h2><span class="muted" id="flowCohort"></span></div>
  <div id="flowTotals" class="flow-totals surface"></div>
  <div class="flow-scroll" tabindex="0" role="region" aria-label="Lifecycle stages and rework paths">
    <div class="flow-canvas">
      <ol class="flow-stages" id="flowStages"></ol>
      <div class="flow-returns" id="flowReturns">
        <svg viewBox="0 0 1000 65" preserveAspectRatio="none" aria-hidden="true">
          <defs><marker id="backArrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
            <path d="M0 0l5 3-5 3" fill="none" stroke="#ba825c"/></marker></defs>
          <path class="belt" d="M687 0v12h42" marker-end="url(#backArrow)"/>
          <path class="belt" d="M250 0v27H150V4" marker-end="url(#backArrow)"/>
          <path class="belt" d="M477 0v27h-55V4" marker-end="url(#backArrow)"/>
          <path class="belt" d="M850 0v46H650V4" marker-end="url(#backArrow)"/>
        </svg>
      </div>
    </div>
  </div>
  <details class="note flow-notes"><summary>What the timings and loop counts mean</summary>
    Stage boxes add all visits to that stage and count parallel jobs within it once. Stages can overlap, so the boxes
    do not add up to the end-to-end total. The PR strips assign each second to just one stage. “Mark ready” and
    “Merged” are instant events. Missing coordinator history
    can undercount lane waits, refreshes and conflicts. “Review fixes” counts a different head certified after a
    Copilot review, not proof the changes addressed it. Pushes use the force-push time or, when unavailable, the first
    observed workflow for a new head; the count includes only old runs with observed running time at that point.
    Queue removals show all non-merge exits, not only those followed by a refresh.
  </details>
</section>
<section id="stageStrip" aria-labelledby="stripTitle">
  <div class="flow-head strip-head"><h2 id="stripTitle">Each PR, one clock</h2><span class="muted" id="stripCount"></span></div>
  <p class="muted strip-head">Read left to right. Each bar fills its own elapsed time; the total at right shows how long the PR took.</p>
  <div class="surface strip-viewport" role="region" aria-label="PR stage strips" tabindex="0" id="stripViewport">
    <div class="strip-row strip-column"><span>Pull request</span><span>Current stage</span>
      <span>Start to finish · hatched time not recorded</span><span>Total</span></div>
    <div id="stripRows"></div>
  </div>
</section>
"""

JS = r"""
const FLOW=D?.flow;
const FLOW_STAGES={
  dev:["Opened / dev","Draft work and edits before GitHub marks the PR ready."],
  review:["Copilot review","Wait for Copilot to read the changes and post feedback."],
  cert:["Local certification","Run tests on local machines; available hosts vary."],
  ready:["Mark ready","GitHub switches the PR out of draft; this is a milestone."],
  hosted:["Hosted ready checks","GitHub allocates runners and tests the ready PR."],
  lane:["Wait for lane slot","Wait until the PR can land without conflicting with others."],
  refresh:["Train refresh","Merge main, regenerate files, and run a quick check."],
  checks:["PR check gate","Wait for GitHub checks to report on the refreshed commit."],
  queue:["Merge queue","Wait for a slot and test the PR with queued changes."],
  merged:["Merged","GitHub lands the change on main; this is a milestone."],
  unrecorded:["Unrecorded","No timed stage was captured here after the PR was marked ready."]
};
PHASE.ready=["Mark ready","#e9e9e9"];
PHASE.unrecorded=["Unrecorded","#62636d"];
function flowColor(stage){return PHASE[stage][1]}
function formatFlow(value){return value==null?"—":short(value)}
function drawFlow(){
  const flow=FLOW;
  $("flowCohort").textContent=flow?`${flow.full.n} tracked PRs merged in the last 48h`:"Waiting for lifecycle data";
  const totals=[
    ["First push to merge",flow?.full,flow?
      `${flow.full.n-flow.push_only.n} of ${flow.full.n} use PR opening because GitHub did not show the first push.`:"No merged timing data yet."],
    ["Ready to land to merge",flow?.ready,flow?
      `From first marked-ready event, PR opened ready, or first recorded approval (${flow.ready.n} PRs).`:"No readiness timing data yet."]
  ];
  for(const [title,value,note] of totals){
    const box=el("div","flow-total");
    const median=el("strong","",formatFlow(value?.median));
    box.append(el("h3","",title),median,el("span","","median"),el("span","",`${formatFlow(value?.p90)} p90`),el("p","muted",note));
    $("flowTotals").append(box);
  }
  const stages=Object.entries(FLOW_STAGES).filter(([stage])=>stage!=="unrecorded");
  for(const [index,[stage,[title,hint]]] of stages.entries()){
    const li=el("li"),box=el("div","flow-card"),dl=el("dl"),value=flow?.stages[stage];
    box.style.setProperty("--stage",flowColor(stage));
    for(const [label,number] of [["Median",value?.median],["p90",value?.p90]]){
      dl.append(el("dt","",label),el("dd","",formatFlow(number)));
    }
    box.append(el("h3","",title),dl,el("p","",`${value?.n||0} PRs`));
    li.append(box,el("p","flow-explain",hint));
    if(index<stages.length-1){
      const arrow=svg("svg",{viewBox:"0 0 13 14",class:"flow-arrow","aria-hidden":"true"});
      arrow.append(svg("path",{d:"M1 7h10m-4-4 4 4-4 4"}));li.append(arrow);
    }
    $("flowStages").append(li);
  }
  const loops=[
    ["recert","Review fixes → re-certify",20,17],
    ["superseded","Push while CI runs",45,17],
    ["conflicts","Conflicts → agent resolution",77,0],
    ["removed","Queue removals → refresh",77,37],
  ];
  for(const [k,label,left,top] of loops){
    const item=el("p","flow-return");
    item.style.left=`${left}%`;item.style.top=`${top}px`;
    item.append(document.createTextNode(label+" · "),el("strong","",flow?.loops[k]??(flow?0:"—")));
    $("flowReturns").append(item);
  }
}
function setupStripPiece(control,row,stage,start,end){
  const s={phase:stage,label:FLOW_STAGES[stage][0],start,end,status:"running",activity:"running",
    stateText:start===end?"Milestone":"Elapsed time",note:FLOW_STAGES[stage][1],url:row.url};
  control.type="button";control.dataset.stage=stage;
  control.style.setProperty("--stage",flowColor(stage));
  interact(control,s);
}
function drawStrips(){
  const rows=FLOW?.rows||[];
  const merged=rows.filter(row=>row.merged).length;
  $("stripCount").textContent=`${rows.length-merged} open · ${merged} merged`;
  if(!rows.length){$("stripRows").append(el("p","strip-row muted","No PRs in the snapshot."));return}
  const fragment=document.createDocumentFragment();
  for(const row of rows){
    const tr=el("div","strip-row pr-strip"),title=el("div","strip-title"),track=el("div","strip-track");
    tr.dataset.prStrip=row.number;
    title.title=row.title;title.append(link(row.url,`#${row.number}`),document.createTextNode(row.title));
    const duration=row.end-row.first;
    for(const part of row.strip){
      const sec=part.end-part.start,control=el("button",`strip-piece ${part.stage}`);
      control.style.width=`${100*sec/duration}%`;control.append(el("span","",short(sec)));
      setupStripPiece(control,row,part.stage,part.start,part.end);track.append(control);
    }
    const markers=row.ready.map(at=>[at,"ready"]);
    if(row.merged)markers.push([row.merged,"merged"]);
    for(const [at,phase] of markers){
      const control=el("button","strip-mark");
      control.style.setProperty("--position",`${duration?100*(at-row.first)/duration:0}%`);
      setupStripPiece(control,row,phase,at,at);track.append(control);
    }
    const active=el("span","strip-now",FLOW_STAGES[row.current][0]);
    active.title=active.textContent;
    const total=el("strong","",short(duration));total.title=`${exact(duration)} · ${row.first_source}`;
    tr.append(title,active,track,total);fragment.append(tr);
  }
  $("stripRows").append(fragment);
}
drawFlow();
drawStrips();
$("stripViewport").addEventListener("scroll",()=>hide(true),{passive:true});
"""
