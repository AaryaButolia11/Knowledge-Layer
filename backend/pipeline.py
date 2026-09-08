"""
pipeline.py
===========
Orchestrates one document end-to-end and keeps the knowledge layer incremental.

  ingest -> extract (heuristic + optional LLM) -> ground -> store facts
         -> re-reconcile ONLY the metric clusters this document touched

Because reconciliation is scoped to affected metric keys, adding the 20th PDF
does not rebuild the knowledge for the first 19 — it only recomputes edges for
the metrics that overlap, which is what makes the layer scale to many docs.
"""
from __future__ import annotations
import hashlib
import os
from collections import defaultdict
from typing import Dict, Any, List

from ingest import ingest, Doc
from extract import extract_heuristic
from ground import ground_facts
from reconcile import build_relationships
from db import Store

CHUNK_SIZE = 1200      # chars per RAG chunk
CHUNK_OVERLAP = 150


def _build_chunks(doc: Doc) -> List[Dict[str, Any]]:
    """Group each page's lines into overlapping text windows for lexical
    retrieval (rag.py). Kept separate from `facts` — chunks are for free-text
    Q&A, facts are for the structured reconciliation graph."""
    pages: Dict[int, List[str]] = defaultdict(list)
    for ln in doc.lines:
        pages[ln.page].append(ln.text)

    chunks = []
    for pno, texts in pages.items():
        joined = "\n".join(texts)
        if not joined.strip():
            continue
        start, idx = 0, 0
        while start < len(joined):
            end = min(len(joined), start + CHUNK_SIZE)
            text = joined[start:end]
            cid = hashlib.sha1(
                f"{doc.doc_id}|{pno}|{idx}".encode()).hexdigest()[:16]
            chunks.append({"id": f"c_{cid}", "doc_id": doc.doc_id,
                           "doc_name": doc.name, "page": pno,
                           "chunk_idx": idx, "text": text})
            idx += 1
            if end == len(joined):
                break
            start = end - CHUNK_OVERLAP
    return chunks


def process_document(path: str, store: Store, use_llm: bool = False,
                     drop_ungrounded: bool = False,
                     max_pages: int | None = None) -> Dict[str, Any]:
    doc = ingest(path, max_pages=max_pages)

    facts = extract_heuristic(doc)
    if use_llm or os.environ.get("EXTRACTOR") == "llm":
        try:
            from extract_llm import extract_llm
            facts += extract_llm(doc)
        except Exception:
            pass

    facts = ground_facts(facts, drop_ungrounded=drop_ungrounded)

    store.add_document(doc.doc_id, doc.name, doc.n_pages, doc.vintage, doc.path)
    store.upsert_facts(facts)
    store.add_chunks(_build_chunks(doc))

    # incremental reconciliation: only the metric keys this doc touched
    touched = sorted({f.metric_key for f in facts})
    affected_facts = []
    for k in touched:
        affected_facts.extend(store.facts(metric_key=k))
    rels = build_relationships(affected_facts)
    store.replace_relationships_for_keys(touched, rels)

    return {
        "doc_id": doc.doc_id,
        "name": doc.name,
        "pages": doc.n_pages,
        "vintage": doc.vintage,
        "facts_extracted": len(facts),
        "grounded": sum(1 for f in facts if f.grounded),
        "metric_clusters_touched": len(touched),
        "relationships_in_touched": len(rels),
        "total_pages": doc.n_pages,
        "pages_processed": doc.pages_processed,
        "is_truncated": doc.is_truncated,
        "truncation_note": doc.truncation_note,
    }


def reconcile_all(store: Store):
    """Full re-reconciliation across every metric key (used on demand)."""
    keys = store.metric_keys()
    all_facts = store.facts()
    rels = build_relationships(all_facts)
    store.replace_relationships_for_keys(keys, rels)
    return len(rels)