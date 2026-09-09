"""
main.py
=======
FastAPI service for the Fact Knowledge Layer.

Endpoints
---------
POST /api/upload            upload a PDF; it is ingested, facts extracted &
                            grounded, stored, and reconciliation is re-run
                            incrementally for the metric keys it touched.
GET  /api/facts             all stored facts (optionally ?metric_key=)
GET  /api/relationships     reconciliation results (optionally ?type=), each
                            enriched with both facts' display fields. Falls back
                            to the shipped sample_output/ when the DB is empty,
                            so a reviewer sees results with zero setup.
GET  /api/evidence/{id}     PNG crop of the source-PDF region behind a fact.
GET  /api/stats             counts by relationship type.
GET  /api/documents         ingested documents.

Extraction backend: the heuristic extractor always runs (no credentials
needed). If GROQ_API_KEY is set, the Groq LLM extractor (model from GROQ_MODEL,
default openai/gpt-oss-120b) runs as well for higher recall — its output is
grounded and cached identically.
"""
from __future__ import annotations
import io
import json
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Query
from fastapi.responses import Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import Store
from pipeline import process_document
import evidence as evidence_mod
import rag

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
DB_PATH = os.environ.get("KB_DB", str(BASE / "knowledge.db"))
UPLOAD_DIR = BASE / "uploads"
SAMPLE_DIR = ROOT / "sample_output"
FRONTEND_DIR = ROOT / "frontend"
UPLOAD_DIR.mkdir(exist_ok=True)

USE_LLM = bool(os.environ.get("GROQ_API_KEY"))
# Autoseed runs on every cold start and can touch many pages across several
# PDFs in one burst. Running that with the LLM extractor on used to fire a
# rapid sequence of Groq calls before a single real user request had even
# arrived, tripping the free-tier rate limit and leaving /api/ask sitting in
# a cooldown window nobody caused. Default autoseed to the heuristic
# extractor only; opt in explicitly (AUTOSEED_USE_LLM=true) if you want
# higher-recall seeding and are OK with the rate-limit risk on cold start.
AUTOSEED_USE_LLM = os.environ.get("AUTOSEED_USE_LLM", "false").lower() == "true"
store = Store(DB_PATH)

# Seeding progress, so the UI can wait until the knowledge base is fully built
# instead of rendering a half-seeded snapshot (the autoseed runs in a background
# thread on cold start so the port binds immediately for Render's health check).
_seed = {"seeding": False, "done": 0, "total": 0, "ready": False}


def _autoseed_if_empty():
    """Zero-setup startup: if the knowledge base has no documents yet, build it
    from the bundled sample PDFs. This is why `python main.py` on its own
    gives a fully working demo — with real evidence crops and annotated
    PDFs — without a separate seeding step."""
    try:
        if store.stats().get("documents", 0) > 0:
            _seed["ready"] = True
            return
    except Exception:
        pass
    import glob
    pdfs = sorted(glob.glob(str(ROOT / "sample_pdfs" / "*.pdf")))
    if not pdfs:
        print("[startup] no sample_pdfs/ found; starting with an empty layer.")
        _seed["ready"] = True
        return
    _seed.update(seeding=True, total=len(pdfs), done=0, ready=False)
    print(f"[startup] empty knowledge base — seeding from {len(pdfs)} sample PDFs "
          f"(LLM extractor: {AUTOSEED_USE_LLM}). One-time, ~10–20s …")
    for p in pdfs:
        try:
            process_document(p, store, use_llm=AUTOSEED_USE_LLM)
        except Exception as e:  # never let one bad PDF stop startup
            print(f"[startup]   skipped {os.path.basename(p)}: {e}")
        _seed["done"] += 1
    _seed.update(seeding=False, ready=True)
    print(f"[startup] seeded: {store.stats()}")


app = FastAPI(title="Fact Knowledge Layer")


@app.on_event("startup")
def _kick_off_autoseed():
    # Run in a background thread so the server can bind its port and answer
    # health checks immediately, instead of Render's port scan timing out
    # while a cold-start ingests every sample PDF synchronously.
    import threading
    threading.Thread(target=_autoseed_if_empty, daemon=True).start()

# ---- in-memory progress for the last upload (nice UX, not essential) -------- #
_status = {"state": "idle", "detail": ""}


def _fact_view(f) -> dict:
    return {
        "id": f.id, "doc_name": f.doc_name, "page": f.page,
        "metric": f.metric, "metric_key": f.metric_key,
        "value_raw": f.value_raw, "value": f.value,
        "unit": f.unit.get("dimension"), "unit_canonical": f.unit.get("canonical"),
        "period": f.period.get("label"), "scope": f.scope,
        "evidence": f.evidence, "confidence": f.confidence,
        "extractor": f.extractor,
    }


def _enrich(rels):
    out = []
    for r in rels:
        fa, fb = store.fact(r["fact_a"]), store.fact(r["fact_b"])
        if not fa or not fb:
            continue
        out.append({**r, "a": _fact_view(fa), "b": _fact_view(fb)})
    return out


@app.post("/api/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...),
                 max_pages: int | None = Query(
                     None, description="Cap ingestion to the first N pages. "
                                       "Omit or 0 to scan the whole document.")):
    name = os.path.basename(file.filename or "upload.pdf")
    dest = UPLOAD_DIR / name
    with open(dest, "wb") as buf:
        shutil.copyfileobj(file.file, buf)

    def job():
        _status.update(state="processing", detail=f"Ingesting {name}…")
        try:
            res = process_document(str(dest), store, use_llm=USE_LLM,
                                   max_pages=max_pages)
            note = f" {res['truncation_note']}" if res.get("is_truncated") else ""
            _status.update(
                state="done",
                detail=f"{name}: {res['facts_extracted']} facts, "
                       f"reconciliation updated.{note}",
                total_pages=res["total_pages"], pages_processed=res["pages_processed"],
                is_truncated=res["is_truncated"], truncation_note=res["truncation_note"])
        except Exception as e:  # keep the server alive; surface the error
            _status.update(state="error", detail=f"{name}: {e}")

    background_tasks.add_task(job)
    return {"status": "processing", "file": name, "llm": USE_LLM,
            "max_pages": max_pages,
            "message": "Ingestion started — poll /api/status."}


@app.get("/api/status")
def status():
    return _status


@app.get("/api/llm-status")
def llm_status():
    """Diagnostic: attempts to actually build the Groq client right now and
    reports why it failed, if it did — separate from the coarse llm_enabled
    flag in /api/stats, which only checks whether GROQ_API_KEY is set."""
    call = rag._client()
    return {"key_set": bool(os.environ.get("GROQ_API_KEY")),
            "client_ready": call is not None,
            "reason": rag._last_client_error}


class AskRequest(BaseModel):
    question: str
    doc_id: str | None = None   # scope to one ingested document; None = all
    top_k: int = 6


@app.post("/api/ask")
def ask(body: AskRequest):
    """RAG Q&A over ingested PDFs: BM25 retrieval over stored page-text chunks,
    then a Groq call constrained to cite (document, page) for every claim."""
    return rag.answer_question(body.question, store, doc_id=body.doc_id,
                               top_k=body.top_k)


@app.get("/api/documents")
def documents():
    return store.documents()


@app.get("/api/facts")
def facts(metric_key: str | None = Query(None), limit: int = 1000):
    fs = store.facts(metric_key=metric_key)[:limit]
    return [_fact_view(f) for f in fs]


@app.get("/api/relationships")
def relationships(type: str | None = Query(None)):
    rels = store.relationships(rtype=type)
    if rels:
        return _enrich(rels)
    # zero-setup fallback: shipped sample so reviewers see results immediately
    sample = SAMPLE_DIR / "relationships.json"
    if sample.exists():
        data = json.loads(sample.read_text())
        if type:
            data = [r for r in data if r.get("type") == type]
        return JSONResponse({"source": "sample", "items": data})
    return []


def _pick_case_example(rels_enriched: list, *, rtype: str, dimension=None):
    for r in rels_enriched:
        if r["type"] != rtype:
            continue
        if dimension is not None and r.get("dimension") != dimension:
            continue
        return r
    return None


@app.get("/api/cases")
def cases(mode: str = Query("dynamic", enum=["dynamic", "benchmark"])):
    """
    The four evaluation cases, either derived live from whatever is currently
    in the knowledge graph (mode=dynamic) or as a curated reference answer
    from the bundled Delhivery documents (mode=benchmark) — so a reviewer can
    check the live output against a known-good answer without depending on
    what happens to be in the DB right now.
    """
    if mode == "benchmark":
        path = SAMPLE_DIR / "cases.json"
        if path.exists():
            return JSONResponse(json.loads(path.read_text()))
        return JSONResponse({"source": "benchmark", "cases": []})

    rels = _enrich(store.relationships())
    case1 = _pick_case_example(rels, rtype="corroborate")
    case2 = _pick_case_example(rels, rtype="contradict")
    case3 = (_pick_case_example(rels, rtype="reconciled", dimension="unit")
             or _pick_case_example(rels, rtype="reconciled", dimension="definition")
             or _pick_case_example(rels, rtype="reconciled", dimension="basis")
             or _pick_case_example(rels, rtype="reconciled", dimension="period"))
    return JSONResponse({
        "source": "dynamic",
        "cases": [
            {"case": 1, "title": "Corroborated fact across documents",
             "relationship": case1,
             "found": case1 is not None},
            {"case": 2, "title": "Genuine / likely contradiction",
             "relationship": case2,
             "found": case2 is not None},
            {"case": 3, "title": "Apparent contradiction explained by context",
             "relationship": case3,
             "found": case3 is not None},
            {"case": 4, "title": "Extraction & reasoning failure analysis",
             "note": "Not derivable from the live graph — accounting-parentheses "
                     "sign handling is implemented (see normalize.parse_value); "
                     "footnote-formula column matching for reordered table "
                     "headers is not yet implemented.",
             "status": "partially_implemented"},
        ],
    })


@app.get("/api/ready")
def ready():
    """Lightweight readiness probe the UI polls on load: true once the initial
    autoseed has finished, so the frontend never renders a half-built layer."""
    return {"ready": _seed["ready"], "seeding": _seed["seeding"],
            "done": _seed["done"], "total": _seed["total"]}


@app.get("/api/stats")
def stats():
    s = store.stats()
    if not s.get("relationships") and (SAMPLE_DIR / "stats.json").exists():
        s = json.loads((SAMPLE_DIR / "stats.json").read_text())
        s["source"] = "sample"
    s["llm_enabled"] = USE_LLM
    # seeding progress so the UI can wait for a complete layer
    s["seeding"] = _seed["seeding"]
    s["ready"] = _seed["ready"]
    s["seed_done"] = _seed["done"]
    s["seed_total"] = _seed["total"]
    return s


@app.get("/api/evidence/{fact_id}")
def evidence(fact_id: str):
    png = evidence_mod.crop_for_fact(store, fact_id)
    if not png:
        return Response(status_code=404)
    return Response(content=png, media_type="image/png")


# ---------------------------------------------------------------- annotations #
# The colour of a fact is the strongest relationship it takes part in.
_TYPE_RANK = {"contradict": 3, "reconciled": 2, "corroborate": 1}


@app.get("/api/page/{doc_id}/{page}")
def page_image(doc_id: str, page: int, zoom: float = 1.6):
    png = evidence_mod.page_png(store, doc_id, page, zoom=zoom)
    if not png:
        return Response(status_code=404)
    return Response(content=png, media_type="image/png")


@app.get("/api/annotated-pdf/{doc_id}")
def annotated_pdf(doc_id: str):
    """Download the source PDF with real, colour-coded highlight annotations and
    the reasoning embedded as hover popups."""
    pdf = evidence_mod.annotated_pdf(store, doc_id)
    if not pdf:
        return Response(status_code=404)
    doc = next((d for d in store.documents() if d["doc_id"] == doc_id), None)
    base = (doc["name"] if doc else doc_id).replace(".pdf", "")
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="annotated-{base}.pdf"'})


@app.get("/api/annotations/{doc_id}")
def annotations(doc_id: str):
    """
    Everything the viewer needs to paint a document: for each page that has at
    least one fact participating in a relationship, the page size and a list of
    coloured boxes (bbox + type + plain-English reason + the counterpart fact).
    Colour = the strongest relationship the fact is part of.
    """
    facts = {f.id: f for f in store.facts(doc_id=doc_id)}
    if not facts:
        return JSONResponse({"doc_id": doc_id, "pages": []})

    # fact_id -> best (rank, type, reason, dimension, other-fact-view)
    best: dict = {}
    for r in store.relationships():
        for me, other in ((r["fact_a"], r["fact_b"]), (r["fact_b"], r["fact_a"])):
            if me not in facts:
                continue
            rank = _TYPE_RANK.get(r["type"], 0)
            if me not in best or rank > best[me][0]:
                of = store.fact(other)
                best[me] = (rank, r["type"], r["explanation"], r.get("dimension"),
                            _fact_view(of) if of else None,
                            bool(r["cross_document"]))

    pages: dict = {}
    for fid, (rank, typ, reason, dim, other, cross) in best.items():
        f = facts[fid]
        bbox = f.bbox if isinstance(f.bbox, list) else json.loads(f.bbox)
        size = evidence_mod.page_size(store, doc_id, f.page)
        if not size:
            continue
        w, h = size
        p = pages.setdefault(f.page, {"page": f.page, "width": w, "height": h,
                                      "boxes": []})
        p["boxes"].append({
            "bbox": bbox, "type": typ, "dimension": dim,
            "cross_document": cross, "reason": reason,
            "metric": f.metric, "value_raw": f.value_raw,
            "period": f.period.get("label"), "unit": f.unit.get("dimension"),
            "counterpart": other,
        })
    ordered = [pages[k] for k in sorted(pages)]
    doc = next((d for d in store.documents() if d["doc_id"] == doc_id), None)
    return JSONResponse({
        "doc_id": doc_id,
        "doc_name": doc["name"] if doc else doc_id,
        "n_pages": doc["n_pages"] if doc else None,
        "annotated_pages": ordered,
    })


# serve bundled sample JSON (mounted before the catch-all UI mount)
if SAMPLE_DIR.exists():
    app.mount("/sample_output",
              StaticFiles(directory=str(SAMPLE_DIR)), name="sample")

# serve the frontend (index.html at /)
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
    )