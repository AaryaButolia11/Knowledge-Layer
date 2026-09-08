// Fact Knowledge Layer — UI controller
const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
const api = (p) => fetch(p).then(r => r.json());
const esc = (s) => (s ?? "").toString().replace(/[&<>"']/g,
  c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));

const LABEL = { corroborate:"Corroborated", contradict:"Contradiction", reconciled:"Reconciled" };

let RELS = [];          // enriched relationships
let FACTS = [];         // all facts
let FILTER = "all";

/* -------------------------------------------------- normalise relationships */
// /api/relationships returns either enriched objects (with a/b) or, when the DB
// is empty, {source:"sample", items:[...]}. Normalise to a common shape.
function normRels(payload){
  const items = Array.isArray(payload) ? payload : (payload.items || []);
  return items.map(r => ({
    id:r.id, type:r.type, dimension:r.dimension, explanation:r.explanation,
    cross:!!r.cross_document, metric_key:r.metric_key,
    a:r.a, b:r.b,
  })).filter(r => r.a && r.b);
}

/* -------------------------------------------------------------------- boot */
async function boot(){
  const stats = await api("/api/stats").catch(()=>({}));
  paintTally(stats);
  const engine = $("#engine-pill");
  if (stats.llm_enabled){ engine.textContent = "engine · heuristic + Groq LLM"; engine.classList.add("llm-on"); }
  else { engine.textContent = "engine · heuristic (offline)"; }

  RELS  = normRels(await api("/api/relationships").catch(()=>[]));
  FACTS = await api("/api/facts?limit=2000").catch(()=>[]);
  renderRels(); renderFacts(); loadDocList();
}

function paintTally(s){
  $("#n-corr").textContent   = s.corroborate ?? 0;
  $("#n-contra").textContent = s.contradict ?? 0;
  $("#n-recon").textContent  = s.reconciled ?? 0;
  $("#n-facts").textContent  = s.grounded_facts ?? s.facts ?? 0;
  $("#n-docs").textContent   = s.documents ?? 0;
}

/* ------------------------------------------------------- relationship cards */
function factSide(f){
  const crop = `<img class="crop" loading="lazy" src="/api/evidence/${encodeURIComponent(f.id)}"
                 onerror="this.remove()" alt="source evidence crop" />`;
  return `<div class="side">
    <div class="val">${esc(f.value_raw)}</div>
    <div class="meta">${esc(f.metric)} · ${esc(f.period || "period n/a")}${f.unit_canonical?` · ${esc(f.unit_canonical)}`:""}</div>
    <div class="src">${esc(f.doc_name)} — p${(f.page ?? 0)+1}</div>
    ${crop}
  </div>`;
}
function relCard(r){
  return `<article class="rel ${r.type}">
    <div class="rel-head">
      <span class="tag ${r.type}">${LABEL[r.type]}</span>
      <span class="rel-metric"><b>${esc(r.a.metric_key || r.metric_key)}</b></span>
      ${r.cross ? `<span class="xdoc">cross-document</span>`:""}
      ${r.dimension ? `<span class="dim">${esc(r.dimension)}</span>`:""}
    </div>
    <div class="rel-body">${factSide(r.a)}${factSide(r.b)}</div>
    <div class="reason"><b>Why:</b> ${esc(r.explanation)}</div>
  </article>`;
}
function renderRels(){
  const list = FILTER==="all" ? RELS : RELS.filter(r=>r.type===FILTER);
  const box = $("#rel-list");
  if(!list.length){ box.innerHTML = `<p class="empty">No ${FILTER==="all"?"":LABEL[FILTER].toLowerCase()+" "}relationships yet. Add a PDF to grow the layer.</p>`; return; }
  // contradictions first, then reconciled, then corroborated
  const rank = {contradict:0, reconciled:1, corroborate:2};
  list.sort((a,b)=> (rank[a.type]-rank[b.type]) || (b.cross-a.cross));
  box.innerHTML = list.map(relCard).join("");
}

/* ----------------------------------------------------------------- facts */
function renderFacts(q=""){
  q = q.trim().toLowerCase();
  const rows = FACTS.filter(f=>{
    if(!q) return true;
    return [f.metric,f.value_raw,f.period,f.doc_name,f.unit_canonical]
      .some(v => (v||"").toString().toLowerCase().includes(q));
  }).slice(0, 400);
  $("#fact-list").innerHTML = rows.length ? rows.map(f=>`
    <div class="fact">
      <div class="fv">${esc(f.value_raw)}</div>
      <div class="fm">${esc(f.metric)}</div>
      <div class="fmeta">
        <span>${esc(f.period||"—")}</span>
        <span>${esc(f.unit_canonical||f.unit||"—")}</span>
        <span>${esc(f.doc_name)} p${(f.page??0)+1}</span>
        ${f.extractor==="llm"?`<span class="badge-llm">Groq LLM</span>`:""}
      </div>
    </div>`).join("") : `<p class="empty">No facts match “${esc(q)}”.</p>`;
}

/* ------------------------------------------------ annotated document viewer */
async function loadDocList(){
  const docs = await api("/api/documents").catch(()=>[]);
  const sel = $("#doc-select");
  if(!docs.length){ sel.innerHTML = `<option>No documents ingested</option>`; return; }
  sel.innerHTML = docs.map(d=>`<option value="${esc(d.doc_id)}">${esc(d.name)} · ${d.n_pages}pp</option>`).join("");
  sel.onchange = () => renderDoc(sel.value);
  renderDoc(sel.value);
}
function setDownloadLink(docId){
  const a = $("#dl-pdf");
  if(a){ a.href = `/api/annotated-pdf/${encodeURIComponent(docId)}`; }
}
async function renderDoc(docId){
  setDownloadLink(docId);
  const pane = $("#doc-pages");
  pane.innerHTML = `<p class="empty">Rendering annotated pages…</p>`;
  const data = await api(`/api/annotations/${encodeURIComponent(docId)}`).catch(()=>null);
  if(!data || !data.annotated_pages || !data.annotated_pages.length){
    pane.innerHTML = `<p class="empty">No relationship-bearing facts on this document’s pages yet.</p>`;
    return;
  }
  pane.innerHTML = "";
  for(const pg of data.annotated_pages){
    const wrap = document.createElement("div");
    wrap.className = "page-wrap";
    wrap.style.width = "min(760px, 100%)";
    const scaleToWidthPct = 100 / pg.width;   // % of page width per PDF point
    const boxes = pg.boxes.map(b=>{
      const [x0,y0,x1,y1] = b.bbox;
      const st = `left:${x0*scaleToWidthPct}%;top:${y0/pg.height*100}%;`
               + `width:${(x1-x0)*scaleToWidthPct}%;height:${(y1-y0)/pg.height*100}%;`;
      return `<div class="box ${b.type}" style="${st}"
                data-payload='${esc(JSON.stringify(b))}'></div>`;
    }).join("");
    wrap.innerHTML =
      `<span class="page-tag">page ${pg.page+1} · ${pg.boxes.length} flagged</span>
       <img loading="lazy" src="/api/page/${encodeURIComponent(docId)}/${pg.page}" alt="page ${pg.page+1}" />
       ${boxes}`;
    pane.appendChild(wrap);
  }
  wireTooltips();
}

/* tooltip on hover over annotation boxes */
function wireTooltips(){
  const tip = $("#tooltip");
  $$(".box").forEach(box=>{
    box.addEventListener("mousemove", e=>{
      const b = JSON.parse(box.dataset.payload);
      tip.className = "tooltip " + b.type;
      const cp = b.counterpart;
      tip.innerHTML =
        `<div class="tt-h">${LABEL[b.type]} · <b>${esc(b.metric)}</b></div>
         <div class="tt-vs">${esc(b.value_raw)} (${esc(b.period||"—")})${cp?` ↔ ${esc(cp.value_raw)} (${esc(cp.period||"—")}, ${esc(cp.doc_name)})`:""}</div>
         <div>${esc(b.reason)}</div>`;
      tip.hidden = false;
      const pad=14, w=tip.offsetWidth, h=tip.offsetHeight;
      let x=e.clientX+pad, y=e.clientY+pad;
      if(x+w>innerWidth) x=e.clientX-w-pad;
      if(y+h>innerHeight) y=e.clientY-h-pad;
      tip.style.left=x+"px"; tip.style.top=y+"px";
    });
    box.addEventListener("mouseleave", ()=>{ tip.hidden=true; });
  });
}

/* ---------------------------------------------------------------- upload */
async function upload(file){
  const label = $("#upload-label"), status = $("#drop-status"), msg = $("#drop-msg");
  label.classList.add("busy"); status.hidden=false; msg.textContent = `Uploading ${file.name}…`;
  const fd = new FormData(); fd.append("file", file);
  try{
    await fetch("/api/upload",{method:"POST",body:fd});
    // poll status
    for(let i=0;i<150;i++){
      await new Promise(r=>setTimeout(r,1200));
      const st = await api("/api/status").catch(()=>({}));
      if(st.detail) msg.textContent = st.detail;
      if(st.state==="done"){ break; }
      if(st.state==="error"){ msg.textContent = st.detail || "Extraction failed."; label.classList.remove("busy"); return; }
    }
    // refresh everything — the new PDF is merged into the same layer
    await boot();
    msg.textContent += "  Layer updated.";
    setTimeout(()=>{ status.hidden=true; }, 3500);
  } finally { label.classList.remove("busy"); }
}

/* ---------------------------------------------------------------- wiring */
function setTab(name){
  $$(".tab").forEach(t=>t.classList.toggle("is-active", t.dataset.tab===name));
  $$(".panel").forEach(p=>p.hidden = p.dataset.view!==name);
}
function setFilter(f){
  FILTER=f;
  $$(".chip").forEach(c=>c.classList.toggle("is-on", c.dataset.filter===f));
  $$(".tile[data-filter]").forEach(t=>t.classList.toggle("sel", t.dataset.filter===f && f!=="all"));
  renderRels();
}

$$(".tab").forEach(t=>t.onclick=()=>setTab(t.dataset.tab));
$$(".chip").forEach(c=>c.onclick=()=>setFilter(c.dataset.filter));
$$(".tile[data-filter]").forEach(t=>t.onclick=()=>{ setTab("relationships"); setFilter(t.dataset.filter); window.scrollTo({top:0,behavior:"smooth"}); });
$("#fact-search").addEventListener("input", e=>renderFacts(e.target.value));
$("#file").addEventListener("change", e=>{ if(e.target.files[0]) upload(e.target.files[0]); e.target.value=""; });

boot();
