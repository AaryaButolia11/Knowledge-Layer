"""
seed_demo.py
============
Build a fresh knowledge base from the bundled sample PDFs (../sample_pdfs), then
export sample_output/ JSON. Run this once for a fully-populated, click-through
demo with working evidence crops:

    cd backend && python seed_demo.py

Uses the heuristic extractor by default (no credentials). Set GROQ_API_KEY (and
optionally GROQ_MODEL, default openai/gpt-oss-120b) to also run the Groq LLM
extractor for higher recall.
"""
from __future__ import annotations
import glob
import json
import os
from pathlib import Path

from db import Store
from pipeline import process_document

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
PDFS = sorted(glob.glob(str(ROOT / "sample_pdfs" / "*.pdf")))
DB = str(BASE / "knowledge.db")
OUT = ROOT / "sample_output"
OUT.mkdir(exist_ok=True)


def _fact_view(f):
    return {"id": f.id, "doc_name": f.doc_name, "page": f.page, "metric": f.metric,
            "metric_key": f.metric_key, "value_raw": f.value_raw, "value": f.value,
            "unit": f.unit.get("dimension"), "unit_canonical": f.unit.get("canonical"),
            "period": f.period.get("label"), "scope": f.scope, "evidence": f.evidence,
            "confidence": f.confidence, "extractor": f.extractor}


def main():
    if os.path.exists(DB):
        os.remove(DB)
    store = Store(DB)
    use_llm = bool(os.environ.get("GROQ_API_KEY"))
    print(f"Seeding from {len(PDFS)} PDFs (LLM extractor: {use_llm}) …")
    for p in PDFS:
        r = process_document(p, store, use_llm=use_llm)
        print(f"  {r['name'][:46]:46s} {r['facts_extracted']:4d} facts")

    facts = [_fact_view(f) for f in store.facts()]
    fmap = {f.id: f for f in store.facts()}
    rels = []
    for r in store.relationships():
        a, b = fmap.get(r["fact_a"]), fmap.get(r["fact_b"])
        if a and b:
            rels.append({**r, "a": _fact_view(a), "b": _fact_view(b)})

    json.dump(facts, open(OUT / "facts.json", "w"), indent=2)
    json.dump(rels, open(OUT / "relationships.json", "w"), indent=2)
    json.dump(store.stats(), open(OUT / "stats.json", "w"), indent=2)
    s = store.stats()
    print(f"\nDone. {s['facts']} facts, {s['relationships']} relationships "
          f"(corroborate={s['corroborate']}, contradict={s['contradict']}, "
          f"reconciled={s['reconciled']}).")
    print(f"DB: {DB}\nSample JSON: {OUT}")


if __name__ == "__main__":
    main()
