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

from db import Store
from pipeline import process_document
import evidence as evidence_mod

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
DB_PATH = os.environ.get("KB_DB", str(BASE / "knowledge.db"))
UPLOAD_DIR = BASE / "uploads"
SAMPLE_DIR = ROOT / "sample_output"
FRONTEND_DIR = ROOT / "frontend"
UPLOAD_DIR.mkdir(exist_ok=True)

USE_LLM = bool(os.environ.get("GROQ_API_KEY"))
store = Store(DB_PATH)


def _autoseed_if_empty():
    """Zero-setup startup: if the knowledge base has no documents yet, build it
    from the bundled sample PDFs (offline heuristic extractor). This is why
    `python main.py` on its own gives a fully working demo — with real evidence
    crops and annotated PDFs — without a separate seeding step."""
    try:
        if store.stats().get("documents", 0) > 0:
            return
    except Exception:
        pass
    import glob
    pdfs = sorted(glob.glob(str(ROOT / "sample_pdfs" / "*.pdf")))
    if not pdfs:
        print("[startup] no sample_pdfs/ found; starting with an empty layer.")
        return
    print(f"[startup] empty knowledge base — seeding from {len(pdfs)} sample PDFs "
          f"(LLM extractor: {USE_LLM}). One-time, ~10–20s …")
    for p in pdfs:
        try:
            process_document(p, store, use_llm=USE_LLM)
        except Exception as e:  # never let one bad PDF stop startup
            print(f"[startup]   skipped {os.path.basename(p)}: {e}")
    print(f"[startup] seeded: {store.stats()}")


_autoseed_if_empty()

app = FastAPI(title="Fact Knowledge Layer")

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
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    name = os.path.basename(file.filename or "upload.pdf")
    dest = UPLOAD_DIR / name
    with open(dest, "wb") as buf:
        shutil.copyfileobj(file.file, buf)

    def job():
        _status.update(state="processing", detail=f"Ingesting {name}…")
        try:
            res = process_document(str(dest), store, use_llm=USE_LLM)
            _status.update(state="done",
                           detail=f"{name}: {res['facts_extracted']} facts, "
                                  f"reconciliation updated.")
        except Exception as e:  # keep the server alive; surface the error
            _status.update(state="error", detail=f"{name}: {e}")

    background_tasks.add_task(job)
    return {"status": "processing", "file": name,
            "llm": USE_LLM, "message": "Ingestion started — poll /api/status."}


@app.get("/api/status")
def status():
    return _status


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


@app.get("/api/stats")
def stats():
    s = store.stats()
    if not s.get("relationships") and (SAMPLE_DIR / "stats.json").exists():
        s = json.loads((SAMPLE_DIR / "stats.json").read_text())
        s["source"] = "sample"
    s["llm_enabled"] = USE_LLM
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
