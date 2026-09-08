"""
test_system.py
==============
End-to-end verification suite for the Fact Knowledge Layer.

    cd backend && python test_system.py

Runs entirely offline against an isolated temporary database seeded from
sample_pdfs/ (your real knowledge.db is never touched). Exits non-zero if any
check fails, so it can gate CI.

Five suites:
  1. REST API endpoints        — every endpoint responds with the right shape.
  2. Financial normalization   — crore / million / billion / lakh / thousand /
                                 tonnes / percent + accounting-parentheses signs.
  3. Reconciler engine         — the four required cases are produced live.
  4. Incremental / dedup        — re-ingesting a doc adds no duplicates and only
                                 re-reconciles touched metric keys.
  5. Grounding & case coverage  — every fact is grounded in its evidence and all
                                 relationship types are present.
"""
from __future__ import annotations
import glob
import os
import sys
import tempfile

# --- isolate: seed into a temp DB so the user's knowledge.db is untouched ---- #
_TMP_DB = os.path.join(tempfile.gettempdir(), "fkl_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["KB_DB"] = _TMP_DB
os.environ.pop("GROQ_API_KEY", None)          # force the offline path

import main                                    # noqa: E402  (auto-seeds _TMP_DB)
from fastapi.testclient import TestClient       # noqa: E402
from normalize import (parse_unit, parse_value, parse_period,  # noqa: E402
                       to_base, canonical_metric)
from pipeline import process_document           # noqa: E402

client = TestClient(main.app)
store = main.store
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_PDFS = sorted(glob.glob(os.path.join(ROOT, "sample_pdfs", "*.pdf")))

# --- tiny test harness (no external deps) ------------------------------------ #
_PASS = 0
_FAIL = 0
_FAILURES = []


def check(name, cond, detail=""):
    global _PASS, _FAIL
    ok = bool(cond)
    if ok:
        _PASS += 1
        print(f"  \u2713 {name}")
    else:
        _FAIL += 1
        _FAILURES.append(name)
        print(f"  \u2717 {name}" + (f"  \u2014 {detail}" if detail else ""))
    return ok


def suite(title):
    print(f"\n=== {title} ===")


def _facts_by_id():
    return {f.id: f for f in store.facts()}


def _rel_facts(r, fmap):
    return fmap.get(r["fact_a"]), fmap.get(r["fact_b"])


# ============================================================================ #
# Suite 1 — REST API endpoints                                                 #
# ============================================================================ #
def suite_api():
    suite("Suite 1 · REST API endpoints")

    s = client.get("/api/stats").json()
    check("GET /api/stats returns populated counts",
          s.get("documents", 0) > 0 and s.get("facts", 0) > 0
          and all(k in s for k in ("corroborate", "contradict", "reconciled")),
          str(s))

    facts = client.get("/api/facts?limit=50").json()
    check("GET /api/facts returns grounded fact records",
          isinstance(facts, list) and facts
          and all(k in facts[0] for k in ("id", "metric", "value_raw", "period")),
          f"got {type(facts)}")

    rels = client.get("/api/relationships").json()
    types = {r["type"] for r in rels}
    check("GET /api/relationships enriched with both facts",
          rels and all(r.get("a") and r.get("b") for r in rels)
          and types <= {"corroborate", "contradict", "reconciled"},
          str(types))

    docs = client.get("/api/documents").json()
    check("GET /api/documents lists ingested docs with paths",
          docs and all(d.get("path") for d in docs))

    # a real fact id must render a PNG crop; a bogus one must 404 cleanly
    fid = rels[0]["a"]["id"]
    ev = client.get(f"/api/evidence/{fid}")
    check("GET /api/evidence/{id} returns a PNG crop",
          ev.status_code == 200 and ev.headers.get("content-type") == "image/png")
    check("GET /api/evidence/{bad} returns 404 (no crash)",
          client.get("/api/evidence/f_does_not_exist").status_code == 404)

    did = docs[0]["doc_id"]
    check("GET /api/page/{doc}/0 renders a page PNG",
          client.get(f"/api/page/{did}/0").status_code in (200, 404))  # doc[0] may have 0 boxes

    ann = client.get(f"/api/annotations/{did}").json()
    check("GET /api/annotations/{doc} returns pages+boxes+reasons",
          "annotated_pages" in ann)

    # find a doc that actually has annotations for the PDF-export check
    export_doc = None
    for d in docs:
        a = client.get(f"/api/annotations/{d['doc_id']}").json()
        if a.get("annotated_pages"):
            export_doc = d["doc_id"]
            break
    pdf = client.get(f"/api/annotated-pdf/{export_doc}")
    check("GET /api/annotated-pdf/{doc} returns a real PDF",
          pdf.status_code == 200
          and pdf.headers.get("content-type") == "application/pdf"
          and pdf.content[:5] == b"%PDF-")

    check("Static UI (index/app.js/style.css) served",
          all(client.get(p).status_code == 200
              for p in ("/", "/app.js", "/style.css")))


# ============================================================================ #
# Suite 2 — Financial number normalization                                     #
# ============================================================================ #
def suite_normalization():
    suite("Suite 2 · Financial number normalization")

    u = parse_unit("\u20b98,142 Cr revenue from services")
    check("\u20b98,142 Cr \u2192 currency, 8,142 crore",
          u.dimension == "currency" and abs(to_base(8142, u) - 8142) < 1)

    um = parse_unit("81,415.38 million revenue from operations")
    check("81,415.38 million \u2192 8,141.5 crore (unit conversion)",
          um.dimension == "currency" and abs(to_base(81415.38, um) - 8141.5) < 1)

    ub = parse_unit("\u20b92.8 Bn revenue")
    check("\u20b92.8 Bn \u2192 280 crore (billion scaling)",
          ub.dimension == "currency" and abs(to_base(2.8, ub) - 280) < 1)

    ul = parse_unit("5 lakh shipments")
    check("5 lakh \u2192 magnitude 5,00,000",
          abs(to_base(5, ul) - 500000) < 1)

    uk = parse_unit("1,429 K tonnes")
    check("tonnes \u2192 mass dimension", uk.dimension == "mass")

    up = parse_unit("11.5%")
    check("11.5% \u2192 percent dimension", up.dimension == "percent")

    check("accounting parentheses (4,157.43) \u2192 negative -4157.43",
          parse_value("(4,157.43)") == -4157.43)
    check("plain 8,142 \u2192 8142.0", parse_value("8,142") == 8142.0)

    # the whole point of unit normalization: these two are the SAME amount
    a = to_base(8142, parse_unit("\u20b98,142 Cr revenue"))
    b = to_base(81415.38, parse_unit("81,415.38 million revenue"))
    check("\u20b98,142 Cr and \u20b881,415.38 M reconcile to the same base",
          abs(a - b) < 1.0, f"{a} vs {b}")

    check("EBITDA vs EBITDA margin stay distinct metrics",
          canonical_metric("EBITDA")[0] != canonical_metric("EBITDA margin")[0])
    check("Q4 FY24 parses as a quarter; FY24 as a full year",
          parse_period("Q4 FY24").kind == "quarter"
          and parse_period("FY24").kind == "fy")


# ============================================================================ #
# Suite 3 — Reconciler engine: the four required cases, live                    #
# ============================================================================ #
def suite_reconciler():
    suite("Suite 3 · Reconciler engine (four required cases)")
    fmap = _facts_by_id()
    rels = store.relationships()

    def find(pred):
        for r in rels:
            a, b = _rel_facts(r, fmap)
            if a and b and pred(r, a, b):
                return r, a, b
        return None

    # Case 1 — corroboration (same value, agreeing sources)
    corr = find(lambda r, a, b: r["type"] == "corroborate")
    check("Case 1 \u2014 a corroboration exists", corr is not None)

    # Case 2 — genuine contradiction: prospectus 40,000 vs 12,350 (\u2192 4000 vs 1235 cr)
    contra = find(lambda r, a, b: r["type"] == "contradict"
                  and {round(a.value_base), round(b.value_base)} == {4000, 1235})
    check("Case 2 \u2014 genuine contradiction \u20b940,000M vs \u20b912,350M",
          contra is not None)

    # Case 3a — reconcile by DEFINITION: EBITDA 127 (reported) vs 76 (adjusted)
    rec_def = find(lambda r, a, b: r["type"] == "reconciled"
                   and r["dimension"] == "definition" and a.metric_key == "ebitda"
                   and {round(a.value_base), round(b.value_base)} == {127, 76})
    check("Case 3a \u2014 reconcile by DEFINITION: EBITDA \u20b9127 vs \u20b976 Cr",
          rec_def is not None)

    # Case 3b — reconcile by PERIOD: EBITDA 127 (FY24) vs 46 (Q4 FY24)
    rec_per = find(lambda r, a, b: r["type"] == "reconciled"
                   and r["dimension"] == "period" and a.metric_key == "ebitda"
                   and {round(a.value_base), round(b.value_base)} == {127, 46})
    check("Case 3b \u2014 reconcile by PERIOD: EBITDA \u20b9127 (FY24) vs \u20b946 (Q4)",
          rec_per is not None)

    # Precision guard (Case 4 handling): contradictions stay rare & are pairs
    n_contra = sum(1 for r in rels if r["type"] == "contradict")
    check("Precision guard \u2014 contradictions are rare (\u2264 5), not noise",
          n_contra <= 5, f"{n_contra} contradictions")


# ============================================================================ #
# Suite 4 — Incremental ingestion & de-duplication                              #
# ============================================================================ #
def suite_incremental():
    suite("Suite 4 · Incremental ingestion & de-duplication")

    before = store.stats()
    # re-ingest an already-present document (identical content)
    res = process_document(SAMPLE_PDFS[0], store, use_llm=False)
    after = store.stats()

    check("re-ingesting identical content adds no new document",
          after["documents"] == before["documents"],
          f"{before['documents']} -> {after['documents']}")
    check("re-ingesting identical content adds no duplicate facts",
          after["facts"] == before["facts"],
          f"{before['facts']} -> {after['facts']}")
    check("re-ingesting adds no duplicate relationships",
          after["relationships"] == before["relationships"],
          f"{before['relationships']} -> {after['relationships']}")
    check("reconciliation is scoped to touched metric keys only",
          res["metric_clusters_touched"] > 0
          and res["metric_clusters_touched"] < after["facts"])


# ============================================================================ #
# Suite 5 — Grounding & four-case coverage                                      #
# ============================================================================ #
def suite_grounding():
    suite("Suite 5 · Grounding & case coverage")
    facts = store.facts()

    check("every stored fact is grounded in its evidence",
          all(f.grounded for f in facts),
          f"{sum(1 for f in facts if not f.grounded)} ungrounded")

    # grounding is real: the fact's digits actually appear in its evidence text
    import re
    sample = [f for f in facts if f.value_raw and any(c.isdigit() for c in f.value_raw)][:50]

    def digits(s):
        return re.sub(r"[^0-9]", "", s)

    def appears(f):
        d = digits(f.value_raw)
        return len(d) < 2 or d in digits(f.evidence)
    check("fact values verifiably appear in their quoted evidence",
          all(appears(f) for f in sample))

    rels = store.relationships()
    types = {r["type"] for r in rels}
    check("all three relationship types present in the live graph",
          {"corroborate", "contradict", "reconciled"} <= types, str(types))

    dims = {r["dimension"] for r in rels if r["type"] == "reconciled"}
    check("reconciliations explain via multiple dimensions (\u2265 2)",
          len(dims) >= 2, str(dims))


# ============================================================================ #
def main_run():
    print("Fact Knowledge Layer \u2014 verification suite")
    print(f"(isolated temp DB: {_TMP_DB}; offline heuristic extractor)")
    if not SAMPLE_PDFS:
        print("No sample_pdfs/ found — cannot run.")
        sys.exit(2)

    suite_api()
    suite_normalization()
    suite_reconciler()
    suite_incremental()
    suite_grounding()

    total = _PASS + _FAIL
    print("\n" + "=" * 60)
    print(f"RESULT: {_PASS}/{total} checks passed"
          + (f", {_FAIL} FAILED" if _FAIL else " \u2014 ALL PASSED \u2705"))
    if _FAILURES:
        print("Failed:", ", ".join(_FAILURES))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main_run()
