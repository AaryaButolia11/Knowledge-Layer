"""
db.py
=====
SQLite persistence. Facts and relationships are stored with full provenance and
normalised fields (JSON blobs for the nested unit/period/scope). Chosen over a
graph DB deliberately: the assignment notes a graph alone is not the solution —
the *reasoning* is the substance, so we keep storage simple and queryable, and
treat the graph as an optional view rendered client-side.

Incrementality: adding a document only re-runs reconciliation for metric keys it
touches, so existing knowledge is not rebuilt from scratch.

Thread-safety: FastAPI serves requests from a thread pool, and one page can fire
dozens of parallel /api/evidence calls. A single connection shared across threads
(check_same_thread=False) will raise "bad parameter or other API misuse" if two
threads touch it at once, so every database access below is serialised with one
lock. It keeps the code simple and is more than fast enough for this workload.
"""
from __future__ import annotations
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional

from models import Fact, Relationship

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY, name TEXT, n_pages INT, vintage TEXT, path TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY, doc_id TEXT, doc_name TEXT, page INT, bbox TEXT,
    metric TEXT, metric_key TEXT, value REAL, value_base REAL, value_raw TEXT,
    unit TEXT, period TEXT, scope TEXT, qualifiers TEXT, evidence TEXT,
    extractor TEXT, confidence REAL, grounded INT
);
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY, type TEXT, fact_a TEXT, fact_b TEXT, metric_key TEXT,
    explanation TEXT, dimension TEXT, confidence REAL, cross_document INT
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(metric_key);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(type);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, doc_id TEXT, doc_name TEXT, page INT,
    chunk_idx INT, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


class Store:
    def __init__(self, path: str = "knowledge.db"):
        self.path = path
        # Make sure the parent directory exists. sqlite3.connect fails with an
        # opaque "unable to open database file" if it doesn't (e.g. KB_DB set
        # to a path under a directory that hasn't been created on this host).
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        # One connection, shared across FastAPI's worker threads. Safe because
        # every access is wrapped in self._lock below.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            # WAL lets /api/evidence & /api/facts readers proceed while an
            # upload is writing facts — readers no longer block on the writer's
            # transaction the way the default rollback-journal mode would.
            try:
                self.conn.execute("PRAGMA journal_mode=WAL;")
                self.conn.execute("PRAGMA synchronous=NORMAL;")
            except sqlite3.OperationalError:
                pass  # e.g. path on a filesystem that doesn't support WAL
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------ docs #
    def add_document(self, doc_id, name, n_pages, vintage, path):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)",
                (doc_id, name, n_pages, vintage, path))
            self.conn.commit()

    def has_document(self, doc_id) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone() is not None

    def documents(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM documents")]

    # ----------------------------------------------------------------- facts #
    def upsert_facts(self, facts: List[Fact]):
        with self._lock:
            for f in facts:
                self.conn.execute(
                    "INSERT OR REPLACE INTO facts VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f.id, f.doc_id, f.doc_name, f.page, json.dumps(f.bbox),
                     f.metric, f.metric_key, f.value, f.value_base, f.value_raw,
                     json.dumps(f.unit), json.dumps(f.period), json.dumps(f.scope),
                     json.dumps(f.qualifiers), f.evidence, f.extractor,
                     f.confidence, int(f.grounded)))
            self.conn.commit()

    def _row_to_fact(self, r) -> Fact:
        return Fact(
            id=r["id"], doc_id=r["doc_id"], doc_name=r["doc_name"], page=r["page"],
            bbox=json.loads(r["bbox"]), metric=r["metric"], metric_key=r["metric_key"],
            value=r["value"], value_base=r["value_base"], value_raw=r["value_raw"],
            unit=json.loads(r["unit"]), period=json.loads(r["period"]),
            scope=json.loads(r["scope"]), qualifiers=json.loads(r["qualifiers"]),
            evidence=r["evidence"], extractor=r["extractor"],
            confidence=r["confidence"], grounded=bool(r["grounded"]))

    def facts(self, metric_key: Optional[str] = None,
              doc_id: Optional[str] = None) -> List[Fact]:
        q, args, conds = "SELECT * FROM facts", [], []
        if metric_key:
            conds.append("metric_key=?"); args.append(metric_key)
        if doc_id:
            conds.append("doc_id=?"); args.append(doc_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def fact(self, fid: str) -> Optional[Fact]:
        if not fid:
            return None
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM facts WHERE id=?", (str(fid),)).fetchone()
        return self._row_to_fact(r) if r else None

    def metric_keys(self) -> List[str]:
        with self._lock:
            return [r["metric_key"] for r in self.conn.execute(
                "SELECT DISTINCT metric_key FROM facts")]

    # ---------------------------------------------------------------- chunks #
    def add_chunks(self, chunks: List[Dict[str, Any]]):
        """chunks: dicts with id, doc_id, doc_name, page, chunk_idx, text."""
        if not chunks:
            return
        with self._lock:
            for c in chunks:
                self.conn.execute(
                    "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?)",
                    (c["id"], c["doc_id"], c["doc_name"], c["page"],
                     c["chunk_idx"], c["text"]))
            self.conn.commit()

    def chunks(self, doc_id: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM chunks", []
        if doc_id:
            q += " WHERE doc_id=?"; args.append(doc_id)
        with self._lock:
            return [dict(r) for r in self.conn.execute(q, args)]

    # --------------------------------------------------------- relationships #
    def replace_relationships_for_keys(self, keys, rels: List[Relationship]):
        with self._lock:
            if keys:
                qmarks = ",".join("?" * len(keys))
                self.conn.execute(
                    f"DELETE FROM relationships WHERE metric_key IN ({qmarks})",
                    list(keys))
            for r in rels:
                self.conn.execute(
                    "INSERT OR REPLACE INTO relationships VALUES (?,?,?,?,?,?,?,?,?)",
                    (r.id, r.type, r.fact_a, r.fact_b, r.metric_key, r.explanation,
                     r.dimension, r.confidence, int(r.cross_document)))
            self.conn.commit()

    def relationships(self, rtype: Optional[str] = None) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM relationships", []
        if rtype:
            q += " WHERE type=?"; args.append(rtype)
        with self._lock:
            rows = [dict(r) for r in self.conn.execute(q, args)]
        order = {"contradict": 0, "reconciled": 1, "corroborate": 2}
        rows.sort(key=lambda r: (order.get(r["type"], 3),
                                 0 if r["cross_document"] else 1,
                                 -r["confidence"]))
        return rows

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            c = self.conn.execute
            return {
                "documents": c("SELECT COUNT(*) FROM documents").fetchone()[0],
                "facts": c("SELECT COUNT(*) FROM facts").fetchone()[0],
                "grounded_facts": c("SELECT COUNT(*) FROM facts WHERE grounded=1").fetchone()[0],
                "relationships": c("SELECT COUNT(*) FROM relationships").fetchone()[0],
                "corroborate": c("SELECT COUNT(*) FROM relationships WHERE type='corroborate'").fetchone()[0],
                "contradict": c("SELECT COUNT(*) FROM relationships WHERE type='contradict'").fetchone()[0],
                "reconciled": c("SELECT COUNT(*) FROM relationships WHERE type='reconciled'").fetchone()[0],
            }