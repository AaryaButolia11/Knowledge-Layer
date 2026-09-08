"""
extract_llm.py
==============
Optional higher-recall extractor using Groq via LangChain. Same output schema
as the heuristic extractor, same grounding checks downstream.

Design notes
------------
* Pluggable & optional. If GROQ_API_KEY is unset or langchain-groq isn't
  installed, this returns [] and the pipeline silently uses the heuristic
  extractor — so the project runs with zero credentials.
* Every LLM response is cached to .llm_cache/ keyed by a hash of the page text
  and model. Re-runs are free and reproducible, and a shipped cache lets a
  reviewer reproduce LLM output without a key.
* The prompt fixes the *frame* (metric/value/unit/period/scope/qualifiers/
  evidence) but leaves the metric vocabulary open — the document decides what
  counts as a fact, satisfying "no hard-coded schema".
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
from typing import List

from ingest import Doc
from models import Fact
from normalize import (parse_value, parse_unit, parse_period, detect_scope,
                       canonical_metric, to_base)

CACHE_DIR = os.environ.get("LLM_CACHE", ".llm_cache")
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
COOLDOWN_SECONDS = int(os.environ.get("GROQ_COOLDOWN_SECONDS", "60"))

# ---------------------------------------------------------------- breaker --- #
# Groq free-tier keys hit HTTP 429 under load. Once we see one, stop calling
# the API for COOLDOWN_SECONDS and fall through to the heuristic extractor for
# every remaining page in this (and any concurrent) run, instead of hammering
# a rate-limited endpoint page after page.
_breaker = {"open_until": 0.0}


def _breaker_open() -> bool:
    return time.time() < _breaker["open_until"]


def _trip_breaker():
    _breaker["open_until"] = time.time() + COOLDOWN_SECONDS


def _is_rate_limit_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate limit" in s or "rate_limit" in s

_SYSTEM = """You extract QUANTITATIVE FACTS from a page of a financial or macroeconomic report.
Return ONLY a JSON array. Each element:
{
 "metric": "<what is measured, verbatim wording, e.g. 'Revenue from services'>",
 "value": "<the number exactly as written, e.g. '8,142' or '(452)' or '18%'>",
 "unit":  "<e.g. 'Rs Cr', '%', 'Mn Tons', 'days', 'count'>",
 "period":"<e.g. 'FY24', 'Q4 FY24', 'Mar 2024', 'FY2024-25', '' if none>",
 "qualifiers": ["consolidated"|"standalone"|"adjusted"|"YoY"|... , ...],
 "evidence": "<the exact sentence or cell text the number came from>"
}
Rules:
- Only extract numbers that are real measurements (money, %, tonnage, counts, days, ratios).
- Do NOT invent numbers. 'value' and 'evidence' must both come from the text.
- Skip page numbers, footnote markers, slide indices.
- Keep metric wording faithful to the source.
Return [] if the page has no facts."""


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, key + ".json")


def _cache_key(text: str) -> str:
    return hashlib.sha1(f"{MODEL}|{text}".encode()).hexdigest()[:20]


def _client():
    """Return a callable(prompt)->str or None if unavailable."""
    if not os.environ.get("GROQ_API_KEY"):
        return None
    try:
        from langchain_groq import ChatGroq
        from langchain_core.messages import SystemMessage, HumanMessage
        llm = ChatGroq(model=MODEL, temperature=0)

        def call(page_text: str) -> str:
            resp = llm.invoke([SystemMessage(content=_SYSTEM),
                               HumanMessage(content=page_text)])
            return resp.content
        return call
    except Exception:
        # fall back to the raw groq SDK if langchain isn't present
        try:
            from groq import Groq
            g = Groq()

            def call(page_text: str) -> str:
                r = g.chat.completions.create(
                    model=MODEL, temperature=0,
                    messages=[{"role": "system", "content": _SYSTEM},
                              {"role": "user", "content": page_text}])
                return r.choices[0].message.content
            return call
        except Exception:
            return None


def _parse_json(raw: str):
    raw = raw.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def extract_llm(doc: Doc) -> List[Fact]:
    call = _client()
    # group lines into per-page text blocks
    pages = {}
    line_bbox = {}
    for ln in doc.lines:
        pages.setdefault(ln.page, []).append(ln.text)
        line_bbox.setdefault(ln.page, ln.bbox)

    facts: List[Fact] = []
    for pno, texts in pages.items():
        page_text = "\n".join(texts)[:6000]
        key = _cache_key(page_text)
        cp = _cache_path(key)
        data = []
        if os.path.exists(cp):
            try:
                data = json.load(open(cp))
            except Exception:
                data = []  # corrupt cache entry for this page only; skip it
        elif call and not _breaker_open():
            try:
                data = _parse_json(call(page_text))
                json.dump(data, open(cp, "w"))
            except Exception as e:
                if _is_rate_limit_error(e):
                    _trip_breaker()
                    print(f"[extract_llm] Groq 429 on page {pno} — cooling down "
                          f"{COOLDOWN_SECONDS}s, remaining pages use the "
                          f"heuristic extractor.")
                else:
                    print(f"[extract_llm] page {pno} failed, skipping: {e}")
                data = []  # this page's failure never aborts the document
        else:
            continue  # breaker open, or no key/cache -> heuristic handles this page

        for item in data:
            try:
                value = parse_value(str(item.get("value", "")))
                if value is None:
                    continue
                unit = parse_unit(f"{item.get('value','')} {item.get('unit','')}")
                metric = str(item.get("metric", "")).strip()
                if not metric:
                    continue
                mkey, base = canonical_metric(metric)
                if not base:
                    continue
                period = parse_period(f"{item.get('period','')} {metric}")
                scope = detect_scope(
                    metric + " " + " ".join(map(str, item.get("qualifiers", []))),
                    doc.name, doc.vintage)
                ev = str(item.get("evidence", "")) or metric
                fid = Fact.make_id(doc.doc_id, pno, mkey, str(item.get("value")),
                                   period.key(), ev)
                facts.append(Fact(
                    id=fid, doc_id=doc.doc_id, doc_name=doc.name, page=pno,
                    bbox=line_bbox.get(pno, [0, 0, 0, 0]),
                    metric=metric[:120], metric_key=mkey, value=value,
                    value_base=to_base(value, unit), value_raw=str(item.get("value")),
                    unit=unit.__dict__, period=period.__dict__, scope=scope.__dict__,
                    qualifiers=[str(q) for q in item.get("qualifiers", [])],
                    evidence=ev, extractor="llm", confidence=0.7))
            except Exception:
                continue
    return facts