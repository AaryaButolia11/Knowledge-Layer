# Fact Knowledge Layer 🧠

A system that reads PDFs, extracts **qualified numerical facts**, ties every fact to the
exact place it came from in the source page, and then works out where those facts
**corroborate**, **contradict**, or **can be reconciled through context** (time, scope,
units, or definition) — across documents.

It runs **fully offline** on a deterministic extractor — **no API key, no paid service,
no internet needed**. A web UI lets you upload a PDF, watch it merge into the existing
knowledge layer, browse relationships with their **source evidence**, and view or download
the **original PDF with real colour-coded highlights** whose reasoning appears on hover.

### Deployment:
> Live Link: https://knowledge-layer-i014.onrender.com
> API Docs : https://knowledge-layer-i014.onrender.com/docs


### Made for:
> Built for the Superjoin VIT 2026 Engineering Intern assignment. The starter set is six
> Indian corporate/macro PDFs (Delhivery filings + RBI / IMF / Economic Survey excerpts).
> **Nothing in the extraction or reasoning is specific to them** — upload any PDF.

> **About the optional Groq key:** you do **not** need it. By default there is no key, so
> only the offline heuristic extractor runs — and it produces everything below. Groq is an
> _optional_ second pass that only _proposes extra candidate facts_ on messy prose; it
> never decides relationships, and it uses Groq's **free** tier. On startup the console
> prints `LLM extractor: False` to confirm it's off.

---

## The four required cases

Each case starts in plain English, then shows the evidence **with exact page references**
and how the engine decides. Every example is **produced live by the running engine** from
the sample PDFs (no curated or hard-coded answers) — a reviewer can click straight to the
cited page and the UI shows a source-evidence crop plus the reasoning.

Sample documents referenced below:
`DECK` = `03-delhivery-q4-fy24-earnings-presentation.pdf` ·
`AR` = `02-delhivery-annual-report-fy24-excerpt.pdf` ·
`PROSPECTUS` = `01-delhivery-prospectus-2022-excerpt.pdf`.

### Case 1 — A fact corroborated across sources, even when written differently

**In plain terms.** The same figure often appears in more than one place, written
differently — with a currency symbol in a headline, as a bare number in a table. A careful
reader treats them as one agreeing fact; so does the system.

**What the engine finds (exact locations).**

- **FY24 revenue = ₹8,142 Cr** — written as **“₹8,142 Cr · FY24 revenue from services”** on
  **`DECK` p6**, and as a bare **“8,142”** in the _Revenue for services (A)_ table on
  **`DECK` p17**.
- **EBITDA = ₹127 Cr** — **“₹127Cr”** on **`DECK` p6** vs **“127” (Reported EBITDA)** on
  **`DECK` p23**.

**How it decides.** Both normalise to the same `(metric = revenue, period = FY24, scope,
unit = crore)` and the same base value; only the representation differs (`₹…Cr` vs a plain
number), so they’re **comparable and equal → corroborate**, tagged _“expressed in different
units but equal after normalisation.”_

**Honest scope note.** These corroborations are **cross-page within one filing**. True
cross-_document_ corroboration (deck ↔ annual report) is currently limited by the
metric-ontology gap in **Limitations** — we don’t fake it.

### Case 2 — A genuine or likely contradiction

**In plain terms.** Sometimes two statements about the same thing genuinely disagree, with
no innocent explanation.

**What the engine finds (exact location).** On **`PROSPECTUS` p1**, the offer size is stated
as **₹40,000.00 million** and as **₹12,350.00 million** — both under the same note
**“\*Subject to finalization of the Basis of Allotment.”**

**How it decides.** Both facts share `(metric, period = May-2022, scope)` and the same
currency unit, but their base values (**₹4,000 Cr vs ₹1,235 Cr**) differ by ~69%, and **no**
period/unit/definition/scope difference explains it. Exactly two conflicting values under
identical qualifiers → **contradiction** (a real draft-vs-final artefact inside one
document).

### Case 3 — An apparent contradiction explained by context

**In plain terms.** Two numbers can look contradictory but aren’t — same quantity, different
period / unit / definition. The system names the one dimension that differs and reconciles
them.

**What the engine finds (exact locations).**

- **By definition** — EBITDA **₹127 Cr (reported)** vs **₹76 Cr (adjusted)**, both on
  **`DECK` p6** → _reconciled by DEFINITION._
- **By period** — EBITDA **₹127 Cr (FY24, `DECK` p6)** vs **₹46 Cr (Q4 FY24, `DECK` p7)** →
  _reconciled by PERIOD._
- **By unit (the classic)** — the normalizer proves **₹8,142 Cr** (`DECK` p6) ≡
  **₹81,415.38 million** (`AR` p22): ₹81,415.38 M ÷ 10 = ₹8,141.5 Cr ≈ ₹8,142 Cr, so an
  apparent order-of-magnitude clash collapses to a match (demonstrated by the normalization
  layer and the test suite).

**How it decides.** The two facts match on base metric but differ on exactly one qualifier —
`definition = reported vs adjusted`, `period = FY vs quarter`, or `unit = crore vs million` —
so the engine emits **reconciled** and records _which_ dimension explains the gap.

### Case 4 — An extraction or reasoning failure, and how it’s handled

**In plain terms.** No extractor is perfect; being honest about the failure modes — and
handling them safely — matters more than pretending they don’t exist.

**Handled correctly (a positive edge case):** accounting **parentheses are read as
negatives** — e.g. _Adjusted EBITDA_ **(404)** on **`DECK` p14** is stored as **−404 Cr**,
not +404, so loss-making periods reason correctly (verified in the UI and the test suite).

**Known failures, handled by staying silent rather than wrong:**

- **Cross-wording of the same entity.** The deck’s **“Partner centers (constellation/BAs)” =
  939 (`DECK` p8)** and the annual report’s **“Partner Delivery Centres” = 938 (`AR` p47)**
  are the same metric on the same date (31 Mar 2024), but different strings, so they don’t
  currently cluster into one contradiction. We deliberately **don’t** bridge this with
  permissive string rules (floods false matches) or a hard-coded synonym (defeats
  generalization) — it’s the top item in **Limitations** (a metric ontology).
- **Chart-only values** with no adjacent unit/period are withheld from reasoning so they
  can’t form false relationships.
- **Parsing-noise guard:** if 3+ distinct values collide on one `(metric, period, scope)`,
  it’s treated as an artefact and **skipped** rather than reported as a contradiction.

---

## How this submission answers the brief

### "Before You Submit" checklist

- ☑ **Runs from these instructions and accepts new PDFs through an API or UI** — one command
  (or `python main.py`); upload via the header button (**UI**) or `POST /api/upload` (**API**).
- ☑ **Results contain facts, source evidence, and cross-document relationships** — a
  searchable facts browser, an evidence crop behind every fact, colour-coded relationship
  cards, and an explicit `cross-document` flag.
- ☑ **Demonstrates the four required cases** — the section above, all live in the UI.
- ☑ **Approach documented + demo video ≤ 3 min** — this README + `DEMO_SCRIPT.md`; paste
  your video link under **Video demo**.
- ☑ **Credentials kept out of the repo; runs without a paid account** — `.env` is
  git-ignored and `sample_output/` holds real, keyless output.

### Brownie points — what actually works

- **Incremental, no rebuild** ✅ — uploading a PDF re-runs reconciliation **only for the
  metric keys it touched**; existing knowledge is untouched.
- **Many PDFs in one layer** ✅ — a single SQLite store keyed by `(metric, period, scope)`;
  re-uploading identical content is de-duplicated by content hash.
- **A schema that evolves dynamically** ✅ — metrics, periods, scopes and qualifiers are
  **open vocabularies read from each document**; a new kind of fact needs no schema change.
- **Large PDFs without falling over** ✅ (with a caveat) — streaming per-page ingest and
  per-fact error isolation mean one bad page never aborts a document; very large scanned
  PDFs are slower and need OCR (see Limitations).

---

## Setup and run instructions

**Prerequisites:** Python 3.10+ (tested on 3.12). No API key required.

### One command (macOS / Linux / Git Bash / WSL)

```bash
./run.sh
```

Creates a virtualenv, installs dependencies, builds the demo knowledge base from
`sample_pdfs/` (offline), and serves at **http://localhost:8000**.

### Windows (PowerShell / Command Prompt)

`run.sh` is a bash script; on plain Windows run the equivalent steps:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cd backend
python main.py
```

Then open **http://localhost:8000** (interactive API docs at **/docs**).

The **first launch auto-seeds** the knowledge base from `sample_pdfs/` if it's empty
(~10–20s; you'll see `[startup] seeding …` in the console), so `python main.py` alone gives
a fully working demo with real evidence crops — no separate step needed.

Notes:

- `--host 0.0.0.0` in the log is the _bind_ address — always open **localhost**, not
  `0.0.0.0`, in the browser.
- A console line like _"Consider using the pymupdf_layout package…"_ is a harmless optional
  notice from PyMuPDF, not an error.

> **Troubleshooting — evidence crops show as 404 / broken images:** your `knowledge.db` was
> created empty by an older run. Stop the server, delete `backend\knowledge.db`, and run
> `python main.py` again — it rebuilds the layer from the sample PDFs and the crops render.
> (`knowledge.db` is disposable and git-ignored; it's always rebuilt from the PDFs.)
>
> **Troubleshooting — `sqlite3: unable to open database file`:** the project is in a
> read-only/synced folder (OneDrive, Desktop, network drive). Move it to a plain local path
> like `C:\dev\fact-knowledge-layer` and rerun.

### Optional: enable the Groq LLM extractor

The heuristic extractor always runs. To add the optional LLM pass for higher recall:

```bash
cp .env.example .env
# set GROQ_API_KEY=...  (free key: https://console.groq.com/keys)
# GROQ_MODEL defaults to openai/gpt-oss-120b  (Groq free tier)
```

Restart the server. The LLM only **proposes** facts; it never classifies relationships, and
its output is grounded and cached exactly like the heuristic path.

---

## Video demo

**Demo video (≤ 3 min):** _[paste your Loom / YouTube link here]_

A click-by-click narration is in **`DEMO_SCRIPT.md`**: the classification tally → the
contradiction card with two evidence crops → the two EBITDA reconciliations (definition +
period) → the revenue corroboration → uploading a new PDF that merges in → the annotated
document viewer with hover-to-explain highlights and the downloadable annotated PDF.

---

## Approach and architecture

### What counts as a fact

A fact is a **qualified measurement**, not a loose number:

```
{ metric, value, unit, period, scope, qualifiers, evidence(doc, page, bbox, quote) }
```

Two facts are **comparable only when** their `(metric, period, scope, unit)` align after
normalization. That one rule is what makes the reasoning trustworthy: we never compare a
quarter to a full year, a margin to an absolute, or crore to million by accident.

### System flow

```
  ┌──────────┐   text+bbox, tables,      ┌───────────┐  candidate  ┌──────────┐
  │  ingest  │──  period-column grids ──▶│  extract  │──  facts  ──▶│  ground  │
  │ (PyMuPDF)│                           │ heuristic │             │ (verify  │
  └──────────┘                           │ +Groq LLM*│             │ number in│
                                         └───────────┘             │ evidence)│
                                                                   └────┬─────┘
   ┌───────────┐   convert units, map fiscal periods,                   │ grounded
   │ normalize │◀──resolve scope/definition, canonical metric───────────┘  facts
   └─────┬─────┘
         │                                    ┌────────────┐  corroborate / contradict /
         └───────────────────────────────────│ reconcile  │  reconcile — deterministic,
              only the touched metric keys    │ (rules)    │  per metric key (incremental)
                                              └─────┬──────┘
                                                    ▼
                       SQLite  ◀──────────────  facts + relationships
                          │
                          ▼
       FastAPI  +  web UI:  evidence crops · annotated pages · downloadable highlighted PDF
   * The LLM pass is optional and only proposes facts; it never classifies relationships.
```

### Component architecture

```
backend/
  ingest.py      PDF → lines+bbox, tables, and period-column grids
  extract.py     heuristic extractors: line/caption reader, table reader, grid reader
  extract_llm.py optional Groq extractor (grounded + disk-cached; returns [] with no key)
  normalize.py   units (crore/mn/lakh…), periods (Indian FY, quarter, snapshot), scope,
                 canonical metric names, monetary-currency inference
  ground.py      verify the claimed number actually appears in its cited evidence
  reconcile.py   deterministic corroborate / contradict / reconcile + incremental update
  models.py      Fact & Relationship dataclasses (stable schema, content-hashed ids)
  db.py          SQLite store; thread-safe; incremental reconciliation by metric key
  evidence.py    render evidence crops, page images, and the annotated highlighted PDF
  pipeline.py    ingest → extract → ground → store → reconcile
  main.py        FastAPI app + endpoints; auto-seeds from sample_pdfs/ on first run
  seed_demo.py   rebuild the KB + sample_output/ from sample_pdfs/
  test_system.py end-to-end verification suite (34 checks, offline, isolated DB)
frontend/        index.html · style.css · app.js   (no build step, vanilla)
```

### Key decisions and trade-offs

- **Reasoning is the product, not the graph.** The brief warns a graph DB or visualization
  "alone is not the solution," so the effort goes into _grounding, comparison and
  explanation_. The relationship engine is **deterministic and explainable**; storage is
  plain SQLite, which keeps the focus on the logic.
- **LLM proposes, rules decide.** LLMs are good at _finding_ candidate numbers in prose but
  unreliable at deciding whether two figures truly conflict. So the (optional) LLM only
  extracts; transparent rules classify — reproducible, and every edge is explainable.
- **Grounding you can see.** Every fact stores page + bounding box; the UI re-opens the PDF
  and renders that exact region, so a reviewer verifies a number without trusting the model.
- **Three extraction layers, one schema:** a geometry-aware line/caption reader (maps slide
  tiles like `₹127Cr / 1.6%` to `EBITDA / EBITDA margin`), PyMuPDF `find_tables`, and a
  custom **period-column grid reader** that recovers KPI/operating-metric values (pin-code
  reach, partner-centre series) where period headers sit far above their data columns.
- **Normalization is where contradictions are won or lost.** Crore/million/lakh, fiscal
  quarters vs calendar dates, reported/adjusted/standalone/consolidated — all converted to a
  common base with scope/definition recorded, so `₹8,142 Cr` vs `₹81,415 M` reconciles
  instead of firing a false contradiction.
- **Precision over recall in reasoning.** 405 facts, but only well-typed level measurements
  enter reasoning; growth deltas and un-anchored integers are stored yet excluded, and a
  genuine contradiction needs _exactly two_ distinct values — yielding a small, high-signal
  relationship set instead of thousands of spurious edges.

### AI tools used

Designed and implemented with an AI coding assistant (Anthropic Claude) for architecture,
implementation and precision-tuning. The **optional runtime LLM** is **Groq**
(`openai/gpt-oss-120b`, free tier) via LangChain, used only for candidate-fact extraction.

---

## Why it generalizes (no hard-coding)

- **No company names, filenames, or document-specific rules** in the extraction or
  reconciliation logic. The _frame_ of a fact is fixed; the _vocabulary_ is discovered per
  document.
- Any PDF at `POST /api/upload` flows through the same pipeline and merges into the same
  layer.
- Seeded facts exist only for a zero-setup demo; they're regenerated from the PDFs by the
  auto-seed / `seed_demo.py`, never committed as ground truth.

---

## Using the interface

- **Classification tally (hero):** live counts of corroborations / contradictions /
  reconciliations; click a tile to filter.
- **Relationships:** colour-coded cards (green / red / amber) with both facts, each with a
  **source evidence crop**, the reconciliation **dimension** chip, and a plain-English **Why**.
- **Annotated documents:** pick a document to see its pages with colour-coded highlight
  boxes over every relationship-bearing fact; **hover** to read the reasoning and the
  counterpart. **Download annotated PDF** exports the _real_ source PDF with genuine
  highlight annotations coloured by type and the reasoning embedded as each highlight's hover
  note (works in any PDF viewer).
- **Facts:** searchable browser over every grounded fact.
- **Add a PDF:** upload from the header; progress shows, the new document **merges** into the
  layer, and the whole view refreshes.

### REST API

| Method & path                     | Purpose                                                          |
| --------------------------------- | ---------------------------------------------------------------- |
| `POST /api/upload`                | Ingest a PDF; extract → ground → store → reconcile incrementally |
| `GET /api/status`                 | Progress of the most recent upload                               |
| `GET /api/facts?metric_key=`      | All grounded facts (optionally by metric)                        |
| `GET /api/relationships?type=`    | Reconciliation results, enriched with both facts                 |
| `GET /api/stats`                  | Counts by relationship type                                      |
| `GET /api/documents`              | Ingested documents                                               |
| `GET /api/evidence/{fact_id}`     | PNG crop of the source region behind a fact                      |
| `GET /api/page/{doc_id}/{page}`   | Full page render (behind the annotation overlay)                 |
| `GET /api/annotations/{doc_id}`   | Colour-coded boxes + reasons for a document's pages              |
| `GET /api/annotated-pdf/{doc_id}` | Download the source PDF with real highlights + hover reasons     |

---

## Verification test suite

Run the full end-to-end suite anytime (offline, ~15s):

```bash
cd backend
python test_system.py
```

**How it runs.** The script sets `KB_DB` to a throwaway path in your temp folder, imports
the app (which **auto-seeds that isolated database** from `sample_pdfs/` — your real
`knowledge.db` is never touched), then drives the live app with FastAPI's `TestClient` and
calls the normalization / reconciliation functions directly. It runs **34 checks across
five suites**, prints a per-check ✓/✗ report, and **exits non-zero on any failure** (so it
can gate CI).

**What passes, and why it matters:**

1. **REST API endpoints** — `/api/stats`, `/api/facts`, `/api/relationships`,
   `/api/documents`, `/api/evidence/{id}` (real → PNG, bogus → clean 404), `/api/page`,
   `/api/annotations`, `/api/annotated-pdf` (returns a real `%PDF`), and the static UI.
2. **Financial normalization** — crore / million→crore / billion / lakh / thousand /
   tonnes / percent, accounting-parentheses negatives `(4,157.43) → −4157.43`, and the
   headline unit reconciliation `₹8,142 Cr ≡ ₹81,415.38 M`.
3. **Reconciler engine** — asserts the four required cases are produced **live**: a
   corroboration; the ₹40,000M vs ₹12,350M contradiction; EBITDA reconcile-by-definition
   (127 vs 76) and reconcile-by-period (127 vs 46); plus the precision guard that keeps
   contradictions rare.
4. **Incremental / de-duplication** — re-ingesting identical content adds no duplicate
   document, facts, or relationships, and reconciliation is scoped to touched metric keys.
5. **Grounding & coverage** — every stored fact is grounded (its digits actually appear in
   the quoted evidence), all three relationship types are present, and reconciliations span
   multiple explanatory dimensions.

**Result (from a clean run):**

```text
[startup] seeded: {'documents': 6, 'facts': 405, 'grounded_facts': 405,
                   'relationships': 40, 'corroborate': 14, 'contradict': 1, 'reconciled': 25}

=== Suite 1 · REST API endpoints ===            ✓ 10/10
=== Suite 2 · Financial number normalization === ✓ 11/11
=== Suite 3 · Reconciler engine (four cases) === ✓  5/5
=== Suite 4 · Incremental & de-duplication ===   ✓  4/4
=== Suite 5 · Grounding & case coverage ===      ✓  4/4
============================================================
RESULT: 34/34 checks passed — ALL PASSED ✅
============================================================
```

Honest, per the brief — what doesn't work yet:

- **Cross-wording entity matching (metric ontology).** We reconcile numbers when metric
  _strings_ align after normalization, but not yet when two documents name the same
  real-world entity differently — e.g. the deck's _"Partner centers (constellation/BAs)"_ vs
  the annual report's _"Partner Delivery Centres."_ Bridging these needs a light metric
  ontology / embedding matcher; permissive string rules flood false matches and hard-coding
  the synonym would defeat the point. **This is the next thing to build**, and it would
  unlock more cross-document cases (e.g. the 939 vs 938 partner-centre count).
- **Chart/image OCR.** Values living only inside a chart image with no adjacent text aren't
  extracted (PyMuPDF reads the text layer). A Tesseract/vision pre-pass would help.
- **Textual, not semantic, grounding.** We verify the claimed number appears in the cited
  region; we don't yet verify the surrounding sentence _means_ what the metric label says.
- **Single-hop reasoning.** Reconciliation compares pairs; multi-hop deduction ("A implies X,
  B implies Y, so C is impossible") is future work.
- **Very large scanned PDFs** are slower and, without OCR, low-yield.

**Planned next:** the metric-ontology matcher above; embeddings to retrieve candidate matches
before comparison at scale; a confidence model for borderline contradictions; and in-browser
bbox highlighting on a zoomable PDF canvas.

---

## Additional notes

- **Deterministic and reproducible.** The relationship set is exactly what the rules produce
  from the facts — no benchmark answers, no curated ground truth. That honesty is what makes
  it trustworthy on documents it has never seen.
- **Runs without credentials.** The full experience — extraction, grounding, reconciliation,
  UI, annotated pages, and the downloadable highlighted PDF — works offline; the LLM is a
  bonus, not a dependency.
- **Repo hygiene.** `.env`, the built `knowledge.db`, uploads and caches are git-ignored;
  `sample_output/` holds real keyless output for evaluation.

### Suggested git flow

```bash
git init && git add .
git commit -m "Fact Knowledge Layer: extraction, grounding, cross-doc reconciliation, UI"
# create an empty GitHub repo, then:
git remote add origin <your-repo-url>
git branch -M main && git push -u origin main
```
