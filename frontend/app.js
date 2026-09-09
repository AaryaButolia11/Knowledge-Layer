// Fact Knowledge Layer — UI controller
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = (p) => fetch(p).then((r) => r.json());
const esc = (s) =>
  (s ?? "").toString().replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );

const LABEL = {
  corroborate: "Corroborated",
  contradict: "Contradiction",
  reconciled: "Reconciled",
};

let RELS = []; // enriched relationships
let FACTS = []; // all facts
let FILTER = "all";

/* -------------------------------------------------- normalise relationships */
// /api/relationships returns either enriched objects (with a/b) or, when the DB
// is empty, {source:"sample", items:[...]}. Normalise to a common shape.
function normRels(payload) {
  const items = Array.isArray(payload) ? payload : payload.items || [];
  return items
    .map((r) => ({
      id: r.id,
      type: r.type,
      dimension: r.dimension,
      explanation: r.explanation,
      cross: !!r.cross_document,
      metric_key: r.metric_key,
      a: r.a,
      b: r.b,
    }))
    .filter((r) => r.a && r.b);
}

/* --------------------------------------------------------------- theming */
// Theme is applied synchronously in <head> (before paint) to avoid a flash.
// This just keeps the toggle button + storage in sync afterwards.
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light"
    ? "light"
    : "dark";
}
function syncThemeButton() {
  const btn = $("#theme-toggle");
  if (btn) btn.setAttribute("aria-pressed", String(currentTheme() === "light"));
}
function toggleTheme() {
  const next = currentTheme() === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem("fkl-theme", next);
  } catch (e) {}
  syncThemeButton();
}
$("#theme-toggle")?.addEventListener("click", toggleTheme);
syncThemeButton();

/* ----------------------------------------------------------------- toasts */
function showToast({
  type = "success",
  title = "",
  message = "",
  actions = [],
} = {}) {
  const root = $("#toast-root");
  if (!root) return () => {};
  actions.forEach((a, i) => {
    a._i = i;
  });
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.innerHTML = `
    <div class="toast-icon" aria-hidden="true">${type === "success" ? "✓" : type === "error" ? "!" : "i"}</div>
    <div class="toast-body">
      ${title ? `<div class="toast-title">${esc(title)}</div>` : ""}
      ${message ? `<div class="toast-msg">${esc(message)}</div>` : ""}
      ${
        actions.length
          ? `<div class="toast-actions">${actions
              .map(
                (a, i) =>
                  `<button type="button" class="${i > 0 ? "ghost" : ""}" data-idx="${a._i}">${esc(a.label)}</button>`,
              )
              .join("")}</div>`
          : ""
      }
    </div>
    <button type="button" class="toast-close" aria-label="Dismiss notification">×</button>`;
  root.appendChild(el);
  // double rAF so the initial (pre-.show) state actually paints before transitioning
  requestAnimationFrame(() =>
    requestAnimationFrame(() => el.classList.add("show")),
  );

  const close = () => {
    el.classList.remove("show");
    el.classList.add("hide");
    setTimeout(() => el.remove(), 320);
  };
  el.querySelector(".toast-close").addEventListener("click", close);
  actions.forEach((a, i) => {
    el.querySelector(`[data-idx="${i}"]`)?.addEventListener("click", () => {
      a.onClick && a.onClick();
      if (a.closeOnClick !== false) close();
    });
  });
  if (type !== "error") setTimeout(close, 8000);
  return close;
}

/* -------------------------------------------------------------------- boot */
// On a cold start (e.g. Render free tier) the server seeds the 6 sample PDFs in
// a background thread. Wait for that to finish before loading, so we never show
// a half-built layer or crops whose facts aren't in the DB yet.
async function waitUntilReady() {
  const bar = $("#drop-status");
  const msg = $("#drop-msg");
  let shown = false;
  for (let i = 0; i < 120; i++) {
    const s = await api("/api/stats").catch(() => ({}));
    // Wait until the backend reports the initial seed is complete. Older builds
    // don't send `ready` at all — treat its absence as "ready" so they still load.
    if (s.ready === true || s.ready === undefined) {
      if (shown && bar) bar.hidden = true;
      return s;
    }
    if (bar && msg) {
      bar.hidden = false;
      shown = true;
      const done = s.seed_done ?? 0,
        total = s.seed_total ?? 6;
      msg.textContent = `Building the knowledge base from sample PDFs… (${done}/${total})`;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  return await api("/api/stats").catch(() => ({}));
}

async function boot() {
  const stats = await waitUntilReady();
  paintTally(stats);
  const engine = $("#engine-pill");
  if (stats.llm_enabled) {
    engine.textContent = "engine · heuristic + Groq LLM";
    engine.classList.add("llm-on");
  } else {
    engine.textContent = "engine · heuristic (offline)";
  }

  RELS = normRels(await api("/api/relationships").catch(() => []));
  FACTS = await api("/api/facts?limit=2000").catch(() => []);
  renderRels();
  renderFacts();
  loadDocList();
  moveTabIndicator();
  if ($(".tab[data-tab='graph']")?.classList.contains("is-active"))
    renderGraph();
}

function animateCount(el, to) {
  to = Number(to) || 0;
  const from = Number(el.dataset.val || 0);
  if (!el.isConnected) {
    el.textContent = to;
    return;
  }
  if (from === to) {
    el.textContent = to;
    el.dataset.val = to;
    return;
  }
  const dur = 650,
    t0 = performance.now();
  function step(t) {
    const p = Math.min(1, (t - t0) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(from + (to - from) * eased);
    if (p < 1) requestAnimationFrame(step);
    else {
      el.textContent = to;
      el.dataset.val = to;
    }
  }
  requestAnimationFrame(step);
}

function paintTally(s) {
  animateCount($("#n-corr"), s.corroborate ?? 0);
  animateCount($("#n-contra"), s.contradict ?? 0);
  animateCount($("#n-recon"), s.reconciled ?? 0);
  animateCount($("#n-facts"), s.grounded_facts ?? s.facts ?? 0);
  animateCount($("#n-docs"), s.documents ?? 0);
}

/* Tidy a raw value token for display. The extractor keeps a few characters
   after the number to catch a unit (e.g. "₹127Cr"), which sometimes drags in a
   trailing separator ("₹127Cr /") or a truncated magnitude word ("₹40,000.00
   mill"). Strip those; the unit chip already shows the canonical unit. */
function cleanVal(raw) {
  let s = (raw ?? "").toString().trim();
  s = s.replace(/[\s/|;:]+$/g, ""); // trailing separators / slashes
  s = s.replace(
    /\s+(mill|milli|millio|billi|billio|thousan?|thousa|lak)$/i,
    "",
  ); // truncated magnitudes
  s = s.replace(/[\s/|]+$/g, "");
  return s.trim() || (raw ?? "").toString().trim();
}

/* ------------------------------------------------------- relationship cards */
function factSide(f) {
  const crop = `<img class="crop" loading="lazy" src="/api/evidence/${encodeURIComponent(f.id)}"
                 onerror="this.remove()" alt="source evidence crop" />`;
  return `<div class="side">
    <div class="val">${esc(cleanVal(f.value_raw))}</div>
    <div class="meta">${esc(f.metric)} · ${esc(f.period || "period n/a")}${f.unit_canonical ? ` · ${esc(f.unit_canonical)}` : ""}</div>
    <div class="src">${esc(f.doc_name)} — p${(f.page ?? 0) + 1}</div>
    ${crop}
  </div>`;
}
function relCard(r) {
  return `<article class="rel ${r.type}">
    <div class="rel-head">
      <span class="tag ${r.type}">${LABEL[r.type]}</span>
      <span class="rel-metric"><b>${esc(r.a.metric_key || r.metric_key)}</b></span>
      ${r.cross ? `<span class="xdoc">cross-document</span>` : ""}
      ${r.dimension ? `<span class="dim">${esc(r.dimension)}</span>` : ""}
    </div>
    <div class="rel-body">${factSide(r.a)}${factSide(r.b)}</div>
    <div class="reason"><b>Why:</b> ${esc(r.explanation)}</div>
  </article>`;
}
function renderRels() {
  const list = FILTER === "all" ? RELS : RELS.filter((r) => r.type === FILTER);
  const box = $("#rel-list");
  if (!list.length) {
    box.innerHTML = `<p class="empty">No ${FILTER === "all" ? "" : LABEL[FILTER].toLowerCase() + " "}relationships yet. Add a PDF to grow the layer.</p>`;
    return;
  }
  // contradictions first, then reconciled, then corroborated
  const rank = { contradict: 0, reconciled: 1, corroborate: 2 };
  list.sort((a, b) => rank[a.type] - rank[b.type] || b.cross - a.cross);
  box.innerHTML = list.map(relCard).join("");
}

/* ----------------------------------------------------------------- facts */
function renderFacts(q = "") {
  q = q.trim().toLowerCase();
  const rows = FACTS.filter((f) => {
    if (!q) return true;
    return [f.metric, f.value_raw, f.period, f.doc_name, f.unit_canonical].some(
      (v) => (v || "").toString().toLowerCase().includes(q),
    );
  }).slice(0, 400);
  $("#fact-list").innerHTML = rows.length
    ? rows
        .map(
          (f) => `
    <div class="fact">
      <div class="fv">${esc(cleanVal(f.value_raw))}</div>
      <div class="fm">${esc(f.metric)}</div>
      <div class="fmeta">
        <span>${esc(f.period || "—")}</span>
        <span>${esc(f.unit_canonical || f.unit || "—")}</span>
        <span>${esc(f.doc_name)} p${(f.page ?? 0) + 1}</span>
        ${f.extractor === "llm" ? `<span class="badge-llm">Groq LLM</span>` : ""}
      </div>
    </div>`,
        )
        .join("")
    : `<p class="empty">No facts match “${esc(q)}”.</p>`;
}

/* ------------------------------------------------ annotated document viewer */
async function loadDocList() {
  const docs = await api("/api/documents").catch(() => []);
  const sel = $("#doc-select");
  if (!docs.length) {
    sel.innerHTML = `<option>No documents ingested</option>`;
    return;
  }
  sel.innerHTML = docs
    .map(
      (d) =>
        `<option value="${esc(d.doc_id)}">${esc(d.name)} · ${d.n_pages}pp</option>`,
    )
    .join("");
  sel.onchange = () => renderDoc(sel.value);
  renderDoc(sel.value);
}
function setDownloadLink(docId) {
  const a = $("#dl-pdf");
  if (a) {
    a.href = `/api/annotated-pdf/${encodeURIComponent(docId)}`;
  }
}
async function renderDoc(docId) {
  setDownloadLink(docId);
  const pane = $("#doc-pages");
  pane.innerHTML = `<p class="empty">Rendering annotated pages…</p>`;
  const data = await api(`/api/annotations/${encodeURIComponent(docId)}`).catch(
    () => null,
  );
  if (!data || !data.annotated_pages || !data.annotated_pages.length) {
    pane.innerHTML = `<p class="empty">No relationship-bearing facts on this document’s pages yet.</p>`;
    return;
  }
  pane.innerHTML = "";
  for (const pg of data.annotated_pages) {
    const wrap = document.createElement("div");
    wrap.className = "page-wrap";
    wrap.style.width = "min(760px, 100%)";
    const scaleToWidthPct = 100 / pg.width; // % of page width per PDF point
    const boxes = pg.boxes
      .map((b) => {
        const [x0, y0, x1, y1] = b.bbox;
        const st =
          `left:${x0 * scaleToWidthPct}%;top:${(y0 / pg.height) * 100}%;` +
          `width:${(x1 - x0) * scaleToWidthPct}%;height:${((y1 - y0) / pg.height) * 100}%;`;
        return `<div class="box ${b.type}" style="${st}"
                data-payload='${esc(JSON.stringify(b))}'></div>`;
      })
      .join("");
    wrap.innerHTML = `<span class="page-tag">page ${pg.page + 1} · ${pg.boxes.length} flagged</span>
       <img loading="lazy" src="/api/page/${encodeURIComponent(docId)}/${pg.page}" alt="page ${pg.page + 1}" />
       ${boxes}`;
    pane.appendChild(wrap);
  }
  wireTooltips();
}

/* tooltip on hover over annotation boxes */
function wireTooltips() {
  const tip = $("#tooltip");
  $$(".box").forEach((box) => {
    box.addEventListener("mousemove", (e) => {
      const b = JSON.parse(box.dataset.payload);
      tip.className = "tooltip " + b.type;
      const cp = b.counterpart;
      tip.innerHTML = `<div class="tt-h">${LABEL[b.type]} · <b>${esc(b.metric)}</b></div>
         <div class="tt-vs">${esc(b.value_raw)} (${esc(b.period || "—")})${cp ? ` ↔ ${esc(cp.value_raw)} (${esc(cp.period || "—")}, ${esc(cp.doc_name)})` : ""}</div>
         <div>${esc(b.reason)}</div>`;
      tip.hidden = false;
      const pad = 14,
        w = tip.offsetWidth,
        h = tip.offsetHeight;
      let x = e.clientX + pad,
        y = e.clientY + pad;
      if (x + w > innerWidth) x = e.clientX - w - pad;
      if (y + h > innerHeight) y = e.clientY - h - pad;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    });
    box.addEventListener("mouseleave", () => {
      tip.hidden = true;
    });
  });
}

/* ---------------------------------------------------------------- upload */
async function upload(file) {
  const label = $("#upload-label"),
    status = $("#drop-status"),
    msg = $("#drop-msg");
  label.classList.add("busy");
  status.hidden = false;
  msg.textContent = `Uploading ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    await fetch("/api/upload", { method: "POST", body: fd });
    let finalState = "timeout";
    for (let i = 0; i < 150; i++) {
      await new Promise((r) => setTimeout(r, 1200));
      const st = await api("/api/status").catch(() => ({}));
      if (st.detail) msg.textContent = st.detail;
      if (st.state === "done") {
        finalState = "done";
        break;
      }
      if (st.state === "error") {
        finalState = "error";
        msg.textContent = st.detail || "Extraction failed.";
        showToast({
          type: "error",
          title: "Ingestion failed",
          message: st.detail || `${file.name} could not be processed.`,
        });
        return;
      }
    }
    // refresh everything — the new PDF is merged into the same layer
    await boot();
    if (finalState === "done") {
      msg.textContent += "  Layer updated.";
      // find the doc we just ingested so we can link straight to its annotated PDF
      const docs = await api("/api/documents").catch(() => []);
      const match =
        docs.find((d) => d.name === file.name) || docs[docs.length - 1];
      showToast({
        type: "success",
        title: "Annotated PDF ready",
        message: `${file.name} finished processing — your annotated PDF is ready to download.`,
        actions: match
          ? [
              {
                label: "Download PDF",
                onClick: () => {
                  window.location.href = `/api/annotated-pdf/${encodeURIComponent(match.doc_id)}`;
                },
              },
              {
                label: "View evidence",
                onClick: () => {
                  setTab("documents");
                  const sel = $("#doc-select");
                  if (sel) {
                    sel.value = match.doc_id;
                    renderDoc(match.doc_id);
                  }
                  window.scrollTo({ top: 0, behavior: "smooth" });
                },
              },
            ]
          : [],
      });
    }
    setTimeout(() => {
      status.hidden = true;
    }, 3500);
  } finally {
    label.classList.remove("busy");
  }
}

/* ---------------------------------------------------------------- graph */
const EDGE_COLOR = {
  corroborate: "#38b98a",
  contradict: "#e75c53",
  reconciled: "#e2a53f",
};
let NETWORK = null;

function graphNodeLabel(f) {
  const v = (f.value_raw || "").toString();
  const m = (f.metric || "").toString();
  return v + (m ? `\n${m.length > 26 ? m.slice(0, 24) + "…" : m}` : "");
}
function renderGraph() {
  const el = $("#graph-canvas");
  if (!el || typeof vis === "undefined") return;
  if (!RELS.length) {
    el.innerHTML = `<p class="empty" style="padding:24px">No relationships yet — add a PDF to grow the graph.</p>`;
    return;
  }
  el.innerHTML = "";
  const nodes = new Map();
  const edges = [];
  RELS.forEach((r) => {
    [r.a, r.b].forEach((f) => {
      if (!nodes.has(f.id)) {
        nodes.set(f.id, {
          id: f.id,
          label: graphNodeLabel(f),
          shape: "box",
          margin: 8,
          color: {
            background: "#1c2333",
            border: "#3a4560",
            highlight: { background: "#232c48", border: "#5a72c4" },
          },
          font: {
            color: "#e8ecf5",
            size: 12,
            face: "Inter, sans-serif",
            multi: false,
          },
          title: `${f.metric || ""} · ${f.period || "period n/a"} · ${f.doc_name || ""} p${(f.page ?? 0) + 1}`,
        });
      }
    });
    edges.push({
      from: r.a.id,
      to: r.b.id,
      color: {
        color: EDGE_COLOR[r.type] || "#888",
        highlight: EDGE_COLOR[r.type] || "#aaa",
      },
      width: r.type === "contradict" ? 3 : 1.6,
      dashes: r.type === "reconciled",
      title: r.explanation || "",
      smooth: { type: "continuous" },
    });
  });
  const data = {
    nodes: new vis.DataSet([...nodes.values()]),
    edges: new vis.DataSet(edges),
  };
  const options = {
    physics: {
      solver: "barnesHut",
      barnesHut: {
        gravitationalConstant: -4200,
        springLength: 150,
        springConstant: 0.03,
      },
      stabilization: { iterations: 150 },
    },
    interaction: { hover: true, tooltipDelay: 100, navigationButtons: false },
    layout: { improvedLayout: true },
  };
  if (NETWORK) {
    NETWORK.destroy();
    NETWORK = null;
  }
  NETWORK = new vis.Network(el, data, options);
}

/* ------------------------------------------------------------------ ask */
function appendChat(role, text, pending = false) {
  const log = $("#ask-log");
  if (!log) return null;
  const el = document.createElement("div");
  el.className = `chat-msg chat-${role}${pending ? " pending" : ""}`;
  el.innerHTML = `<div class="chat-bubble">${esc(text)}</div>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}
function resolveChat(text, sources) {
  const log = $("#ask-log");
  const pending = log?.querySelector(".chat-msg.pending");
  if (!pending) return;
  pending.classList.remove("pending");
  const srcHtml =
    sources && sources.length
      ? `<div class="chat-sources">${sources
          .map(
            (s) =>
              `<span class="src-chip">${esc(s.doc_name)} · p${(s.page ?? 0) + 1}</span>`,
          )
          .join("")}</div>`
      : "";
  pending.querySelector(".chat-bubble").innerHTML =
    esc(text).replace(/\n/g, "<br>") + srcHtml;
  log.scrollTop = log.scrollHeight;
}
async function askQuestion(question) {
  appendChat("user", question);
  appendChat("assistant", "Thinking…", true);
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }).then((r) => r.json());
    resolveChat(res.answer || "No answer returned.", res.sources || []);
  } catch (e) {
    resolveChat("Something went wrong reaching the server — try again.", []);
  }
}
$("#ask-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#ask-input");
  const q = (input?.value || "").trim();
  if (!q) return;
  input.value = "";
  askQuestion(q);
});

/* ---------------------------------------------------------------- wiring */
function moveTabIndicator() {
  const active = $(".tab.is-active");
  const ind = $("#tab-indicator");
  if (!active || !ind) return;
  ind.style.width = active.offsetWidth + "px";
  ind.style.transform = `translateX(${active.offsetLeft}px)`;
}
function setTab(name) {
  $$(".tab").forEach((t) =>
    t.classList.toggle("is-active", t.dataset.tab === name),
  );
  $$(".panel").forEach((p) => {
    const show = p.dataset.view === name;
    p.hidden = !show;
    if (show) {
      p.classList.remove("panel-in");
      void p.offsetWidth; // force reflow so the animation restarts
      p.classList.add("panel-in");
    }
  });
  moveTabIndicator();
}
function setFilter(f) {
  FILTER = f;
  $$(".chip").forEach((c) =>
    c.classList.toggle("is-on", c.dataset.filter === f),
  );
  $$(".tile[data-filter]").forEach((t) =>
    t.classList.toggle("sel", t.dataset.filter === f && f !== "all"),
  );
  renderRels();
}

$$(".tab").forEach(
  (t) =>
    (t.onclick = () => {
      setTab(t.dataset.tab);
      if (t.dataset.tab === "graph") renderGraph();
    }),
);
$$(".chip").forEach((c) => (c.onclick = () => setFilter(c.dataset.filter)));
$$(".tile[data-filter]").forEach(
  (t) =>
    (t.onclick = () => {
      setTab("relationships");
      setFilter(t.dataset.filter);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }),
);
$("#fact-search").addEventListener("input", (e) => renderFacts(e.target.value));
$("#file").addEventListener("change", (e) => {
  if (e.target.files[0]) upload(e.target.files[0]);
  e.target.value = "";
});
window.addEventListener("resize", moveTabIndicator);

boot();
