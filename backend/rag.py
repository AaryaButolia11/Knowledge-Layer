"""
rag.py
======
Question answering over the ingested PDFs.

Retrieval is plain BM25 over the `chunks` table (db.py) — no embeddings API,
no vector DB, no extra credentials. Chunks are built once at ingest time
(pipeline._build_chunks) from the same page text the fact extractors see, so
"ask a question" and "extract a fact" are reading the same source of truth.

Generation is Groq (same model/env vars as extract_llm.py), and it shares that
module's 429 circuit breaker — a rate limit hit by either extraction or Q&A
cools both down, since they're hitting the same key.

The answer is grounded the same way facts are: the prompt requires the model
to cite (document, page) for every claim, and we return the retrieved chunks
as `sources` regardless of what the model says, so the UI can show the reader
exactly what was retrieved even if the answer is wrong.
"""
from __future__ import annotations
import math
import os
import re
from collections import Counter
from typing import Dict, Any, List, Optional

from extract_llm import _breaker_open, _trip_breaker, _is_rate_limit_error, _throttle

MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
TOKEN_RE = re.compile(r"[a-z0-9]+")

_SYSTEM = """You answer questions about financial / macroeconomic PDF reports
using ONLY the excerpts provided below. Rules:
- Every factual claim must cite its source as (DocumentName, p.N), using the
  document names and page numbers exactly as given in the excerpts.
- If the excerpts don't contain enough to answer, say so plainly — do not
  guess or use outside knowledge.
- Keep the answer concise (a few sentences, or a short list for multi-part
  questions)."""

# Set by _client() every time it's called, so /api/llm-status (main.py) can
# report *why* the Groq client isn't ready — not just that it isn't.
_last_client_error: Optional[str] = None


def _tokenize(text: str) -> List[str]:
    return TOKEN_RE.findall(text.lower())


class _BM25:
    def __init__(self, docs: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.avgdl = (sum(len(d) for d in docs) / self.N) if self.N else 0.0
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
                   for t, n in df.items()}
        self._freqs = [Counter(d) for d in docs]

    def top(self, query: str, n: int = 6) -> List[int]:
        qt = _tokenize(query)
        scores = []
        for i, freqs in enumerate(self._freqs):
            dl = len(self.docs[i]) or 1
            s = 0.0
            for t in qt:
                if t not in freqs:
                    continue
                f = freqs[t]
                idf = self.idf.get(t, 0.0)
                s += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [i for _, i in scores[:n]]


def _client():
    """Return callable(system, user) -> str, or None if no Groq key/SDK.

    Also records the reason for failure in module-level `_last_client_error`
    so /api/llm-status can surface it (e.g. "GROQ_API_KEY not set" vs.
    "langchain-groq not installed" vs. an actual SDK/auth error), rather than
    just reporting a bare True/False.
    """
    global _last_client_error
    _last_client_error = None

    if not os.environ.get("GROQ_API_KEY"):
        _last_client_error = "GROQ_API_KEY not set"
        return None

    try:
        from langchain_groq import ChatGroq
        from langchain_core.messages import SystemMessage, HumanMessage
        llm = ChatGroq(model=MODEL, temperature=0.2)

        def call(system: str, user: str) -> str:
            resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            return resp.content
        return call
    except Exception as e_langchain:
        try:
            from groq import Groq
            g = Groq()

            def call(system: str, user: str) -> str:
                r = g.chat.completions.create(
                    model=MODEL, temperature=0.2,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
                return r.choices[0].message.content
            return call
        except Exception as e_groq:
            _last_client_error = (
                f"langchain-groq unavailable ({e_langchain}); "
                f"groq SDK fallback also failed ({e_groq})")
            return None


def answer_question(question: str, store, doc_id: Optional[str] = None,
                    top_k: int = 6) -> Dict[str, Any]:
    question = (question or "").strip()
    if not question:
        return {"answer": "Ask me something about the ingested PDFs.", "sources": []}

    chunks = store.chunks(doc_id=doc_id)
    if not chunks:
        return {"answer": "No documents have been ingested yet — upload a PDF first.",
                "sources": []}

    tokenized = [_tokenize(c["text"]) for c in chunks]
    idxs = _BM25(tokenized).top(question, n=top_k)
    if not idxs:
        return {"answer": "I couldn't find anything in the ingested documents "
                          "relevant to that question.", "sources": []}

    picked = [chunks[i] for i in idxs]
    context = "\n\n---\n\n".join(
        f"[{c['doc_name']}, p.{c['page'] + 1}]\n{c['text']}" for c in picked)
    sources = [{"doc_id": c["doc_id"], "doc_name": c["doc_name"], "page": c["page"]}
              for c in picked]

    call = _client()
    if not call:
        return {"answer": "Set GROQ_API_KEY to enable question answering "
                          "(retrieval still ran — see sources).",
                "sources": sources}
    if _breaker_open():
        return {"answer": "Groq is cooling down after a rate limit — try again "
                          "in about a minute.", "sources": sources}

    try:
        _throttle()
        text = call(_SYSTEM, f"Question: {question}\n\nExcerpts:\n{context}")
    except Exception as e:
        if _is_rate_limit_error(e):
            _trip_breaker()
            return {"answer": "Hit a Groq rate limit — cooling down 60s, try "
                              "again shortly.", "sources": sources}
        return {"answer": f"Question answering failed: {e}", "sources": sources}

    return {"answer": (text or "").strip(), "sources": sources}