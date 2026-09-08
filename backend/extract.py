"""
extract.py
==========
Turns ingested text (lines + table cells) into structured Facts.

Two extractors share one output schema:

  * heuristic  – deterministic, offline, zero-dependency-on-a-key. Mines numeric
                 mentions and associates each with a metric label + period + unit
                 by layout proximity. Runs everywhere, reproducibly.
  * llm        – (extract_llm.py) Groq via LangChain for higher-recall semantic
                 extraction. Same schema, same grounding checks. Opt-in.

The *frame* of a fact is fixed (metric/value/unit/period/scope/evidence); the
*vocabulary* of metrics and qualifiers is open and comes from the documents,
so nothing is hard-coded to any particular filing.
"""
from __future__ import annotations
import re
from typing import List, Optional

from ingest import Doc, Line, TableCell
from models import Fact
from normalize import (parse_value, parse_unit, parse_period, detect_scope,
                       canonical_metric, to_base, Period)

_NUM_TOKEN = re.compile(
    r"[₹]?\s*\(?[-+]?\d{1,3}(?:,\d{2,3})*(?:\.\d+)?\)?\+?%?|"
    r"[₹]?\s*\(?[-+]?\d+(?:\.\d+)?\)?\+?%?")
_PERIOD_HINT = re.compile(
    r"fy\s*'?\d{2,4}|q[1-4]\s*fy|year ended|quarter ended|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s'\-]*\d{2,4}|"
    r"mar\s*'?\d{2}|since inception", re.I)
_UNIT_IN_TOKEN = re.compile(r"[₹%]|\bcr\b|\bmn\b|\bbn\b|\btons?\b|\btonnes?\b|'?000|\bk\b|bps", re.I)
_ALPHA = re.compile(r"[A-Za-z]{3,}")
_STOP_LABELS = re.compile(r"^\s*(note|source|figure|table|slide|page|\d+)\s*$", re.I)


def _label_like(text: str) -> bool:
    if _STOP_LABELS.match(text):
        return False
    alpha = len(_ALPHA.findall(text))
    digits = len(re.findall(r"\d", text))
    return alpha >= 1 and (digits == 0 or alpha >= digits)


# countable-entity nouns: a bare number next to one of these is a real "how many"
# fact even without a unit or magnitude (e.g. "938 Partner Delivery Centres").
_COUNT_NOUN = re.compile(
    r"\b(shipments?|parcels?|orders?|consignments?|centres?|centers?|"
    r"employees?|workers?|customers?|clients?|stores?|outlets?|facilities|"
    r"warehouses?|hubs?|gateways?|vehicles?|trucks?|trailers?|pin[\s-]?codes?|"
    r"stations?|branches?|partners?|dealers?|franchisees?)\b", re.I)
_PURE_PERIOD = re.compile(
    r"^(q[1-4]\s*fy\s*'?\d{2,4}|fy\s*'?\d{2,4}([-/]\d{2,4})?)$", re.I)
_PURE_NUM = re.compile(r"^[₹]?\s*\(?[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?\s*%?$")


def _xc(bbox):
    return (bbox[0] + bbox[2]) / 2.0


def extract_from_grids(doc: Doc) -> List[Fact]:
    """
    Read period-column grids that PyMuPDF's table finder mangles: a header row of
    pure period labels ("Q4 FY22 … Q4 FY24") sitting above data rows, with each
    data number aligned by x under its period column. Generic to any deck that
    lays metrics out this way (operating-metrics tables, KPI grids).
    """
    facts: List[Fact] = []
    by_page: dict = {}
    for ln in doc.lines:
        by_page.setdefault(ln.page, []).append(ln)

    for pno, plines in by_page.items():
        # --- find the period-header row: >=2 pure-period labels sharing a y ---
        rows: dict = {}
        for ln in plines:
            rows.setdefault(round(ln.y / 3) * 3, []).append(ln)
        header = None
        for y, group in rows.items():
            periods = [g for g in group if _PURE_PERIOD.match(g.text.strip())]
            if len(periods) >= 2 and (header is None or len(periods) > len(header[1])):
                header = (y, periods)
        if not header:
            continue
        header_y, periods = header
        columns = sorted(((_xc(p.bbox), p.text.strip()) for p in periods),
                         key=lambda c: c[0])
        col_x = [c[0] for c in columns]
        first_col_x = min(col_x)

        # --- read each data row below the header -------------------------- #
        for y, group in rows.items():
            if y <= header_y:
                continue
            group = sorted(group, key=lambda l: l.bbox[0])
            labels = [g for g in group
                      if g.bbox[0] < first_col_x - 20 and _label_like(g.text)]
            if not labels:
                continue
            row_label = _clean_metric(" ".join(l.text for l in labels))
            if not row_label or len(_ALPHA.findall(row_label)) < 2:
                continue
            for g in group:
                t = g.text.strip()
                if not _PURE_NUM.match(t):
                    continue
                xc = _xc(g.bbox)
                # nearest period column within tolerance
                best = min(col_x, key=lambda cx: abs(cx - xc))
                if abs(best - xc) > 55:
                    continue
                period_label = dict(zip(col_x, [c[1] for c in columns]))[best]
                period = parse_period(period_label)
                if period.kind == "unknown":
                    continue
                unit = parse_unit(t + " " + row_label)
                value = parse_value(t)
                if value is None or _YEAR_RE.match(t.strip("()+%")):
                    continue
                # keep counts only when the metric is a real countable entity
                if unit.dimension == "count" and not (
                        _COUNT_NOUN.search(row_label) or
                        _UNIT_IN_TOKEN.search(t)):
                    continue
                if unit.dimension == "percent" and not _PCT_METRIC.search(row_label):
                    continue
                mkey, base = canonical_metric(row_label)
                if not base:
                    continue
                scope = detect_scope(row_label, doc.name, doc.vintage)
                fid = Fact.make_id(doc.doc_id, pno, mkey, t, period.key(), row_label)
                facts.append(Fact(
                    id=fid, doc_id=doc.doc_id, doc_name=doc.name, page=pno,
                    bbox=g.bbox, metric=row_label[:120], metric_key=mkey,
                    value=value, value_base=to_base(value, unit),
                    value_raw=t, unit=unit.__dict__, period=period.__dict__,
                    scope=scope.__dict__, qualifiers=["grid"], evidence=(
                        f"{row_label}: {t} ({period_label})"),
                    extractor="heuristic", confidence=0.8))
    return facts


def _page_date_period(lines: List[Line]) -> Optional[str]:
    """A snapshot date that captions a whole page, e.g. 'As on March 31, 2024'.
    Returns the matched phrase so parse_period can turn it into a point period."""
    pat = re.compile(
        r"(as on|as at|as of|ended)\s+"
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+(20\d{2})",
        re.I)
    for ln in lines:
        m = pat.search(ln.text)
        if m:
            return m.group(0)
    return None


def _page_dominant_period(lines: List[Line]) -> Optional[str]:
    """The period that captions a whole page/slide (e.g. a corner 'Q4 FY24').
    We take the most frequent short pure-period label on the page."""
    from collections import Counter
    cnt = Counter()
    pure = re.compile(r"^(q[1-4]\s*fy\s*'?\d{2,4}|fy\s*'?\d{2,4}([-/]\d{2,4})?)$", re.I)
    for ln in lines:
        t = ln.text.strip()
        if len(t) <= 12 and pure.match(t):
            cnt[t] += 1
    if cnt:
        return cnt.most_common(1)[0][0]
    for ln in lines[:8]:
        m = _PERIOD_HINT.search(ln.text)
        if m and len(ln.text) < 40:
            return ln.text
    return None


def _iter_num_tokens(text: str):
    for m in re.finditer(r"\(?[-+]?\d{1,3}(?:,\d{2,3})*(?:\.\d+)?\)?|\(?[-+]?\d+(?:\.\d+)?\)?", text):
        yield m.start(), m.end(), m.group(0)


def _find_label(idx: int, page_lines: List[Line]) -> str:
    """The caption for a value tile is normally the line directly BELOW it that
    horizontally overlaps it (slide tiles: big number on top, caption under it).
    Fall back to the nearest label-like line otherwise."""
    cur = page_lines[idx]
    cx0, cx1 = cur.bbox[0], cur.bbox[2]

    def overlap(o: Line) -> bool:
        ox0, ox1 = o.bbox[0], o.bbox[2]
        return min(cx1, ox1) - max(cx0, ox0) > -20  # allow small offset

    # 1) closest label-like line below, overlapping in x, within 40px
    best, best_d = "", 1e9
    for o in page_lines:
        if o is cur or o.y <= cur.y:
            continue
        if not _label_like(o.text) or not overlap(o):
            continue
        d = o.y - cur.y
        if d < best_d and d < 45:
            best, best_d = o.text, d
    if best:
        return best
    # 2) otherwise nearest label-like line in either direction (index neighbours)
    best, best_d = "", 1e9
    for j in range(max(0, idx - 3), min(len(page_lines), idx + 4)):
        if j == idx:
            continue
        o = page_lines[j]
        if not _label_like(o.text):
            continue
        d = abs(o.y - cur.y) + (0 if o.y > cur.y else 6)
        if d < best_d and d < 60:
            best, best_d = o.text, d
    return best


_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# a percent value is only a real fact if its label is something that is measured
# in percent — a margin, rate, ratio, growth, yield, share, etc. This stops an
# inline "YoY: 12.7%" from being recorded as the metric "revenue".
_PCT_METRIC = re.compile(
    r"\b(margin|ratio|rate|growth|yield|inflation|gdp|cagr|roe|roce|roa|"
    r"utilisation|utilization|occupancy|penetration|contribution|churn|"
    r"deficit|surplus|coverage|npa|casa)\b", re.I)
# prose comparison lines ("fell from 6.8% to 4%") carry two numbers for
# different periods — not a same-period contradiction.
_CMP_LINE = re.compile(r"\b(from|to|vs|versus|compared|between)\b", re.I)


def _clean_metric(candidate: str) -> str:
    """A metric label must read like a label, not a sentence."""
    c = candidate.strip()
    words = _ALPHA.findall(c)
    if not words or len(words) > 9 or len(c) > 70:
        return ""
    return c


def _pick_metric(text: str, idx: int, plines: List[Line]) -> str:
    """Choose a label whose canonicalisation is non-empty (not unit-only)."""
    for cand in (text, _find_label(idx, plines)):
        cand = _clean_metric(cand)
        if not cand:
            continue
        _, base = canonical_metric(cand)
        if base and len(base) > 1:
            return cand
    return ""


def _page_dominant_segment(lines: List[Line]) -> Optional[str]:
    """The business segment a whole page/slide is about (e.g. an 'Express Parcel'
    or 'PTL' section header). Used as a fallback so per-segment rows like
    'Service EBITDA margin' don't collide across segments."""
    from collections import Counter
    cnt = Counter()
    for ln in lines[:6]:                       # segment titles sit at the top
        t = ln.text.strip()
        if len(t) > 40 or any(ch.isdigit() for ch in t):
            continue
        sc = detect_scope(t)
        if sc.segment:
            cnt[sc.segment] += 1
    return cnt.most_common(1)[0][0] if cnt else None


def extract_from_lines(doc: Doc) -> List[Fact]:
    """
    Accept a line-level fact ONLY when it is high-signal: a real unit
    (currency / % / mass / days) OR an explicit magnitude count, together with a
    known period and a short label. This deliberately ignores dense prose, where
    number extraction is unreliable, and keeps slide highlights and captioned
    figures, which are clean.
    """
    facts: List[Fact] = []
    by_page = {}
    for ln in doc.lines:
        by_page.setdefault(ln.page, []).append(ln)

    for pno, plines in by_page.items():
        plines.sort(key=lambda l: (round(l.y), l.bbox[0]))
        dom = _page_dominant_period(plines)
        datep = _page_date_period(plines)
        seg = _page_dominant_segment(plines)
        for i, ln in enumerate(plines):
            text = ln.text
            if len(_ALPHA.findall(text)) > 6:
                continue
            if "\n" in text or text.lower().startswith("see "):
                continue
            if _CMP_LINE.search(text) and len(list(_iter_num_tokens(text))) >= 2:
                continue
            spans = list(_iter_num_tokens(text))
            if not spans:
                continue

            # Work out one metric label per number on the line.
            # * single number -> same-line words, else the nearest caption line
            # * N numbers on a "A / B" tile -> split the caption on '/', pair by
            #   index (so "₹127Cr / 1.6%" + "EBITDA / EBITDA margin" maps
            #   127->EBITDA and 1.6%->EBITDA margin). Otherwise skip (ambiguous).
            if len(spans) == 1:
                labels = [_pick_metric(text, i, plines)]
            else:
                caption = _clean_metric(_find_label(i, plines)) or \
                          _clean_metric(text)
                parts = [p.strip() for p in re.split(r"\s*/\s*", caption) if p.strip()]
                if len(parts) == len(spans):
                    labels = parts
                else:
                    continue

            for (s, e, tok), metric_src in zip(spans, labels):
                if not metric_src or _YEAR_RE.match(tok.strip("()+")):
                    continue
                value_raw = text[max(0, s - 1):e + 5]
                nxt = None
                # bound unit context at the next number on the line
                later = [sp for sp in spans if sp[0] > s]
                nxt = later[0][0] if later else min(len(text), e + 12)
                ctx = text[max(0, s - 2):nxt]
                tight = text[max(0, s - 2):e + 4]
                if not _UNIT_IN_TOKEN.search(tight):
                    continue

                unit = parse_unit(ctx)
                if unit.dimension == "count" and not _UNIT_IN_TOKEN.search(ctx):
                    unit = parse_unit(ctx + " " + metric_src)
                value = parse_value(value_raw)
                if value is None:
                    continue

                period = parse_period(" ".join([metric_src, text, dom or ""]))
                has_real_unit = unit.dimension in ("currency", "mass", "percent", "days")
                has_magnitude = bool(_UNIT_IN_TOKEN.search(ctx))
                has_period = period.kind != "unknown"
                if not (has_real_unit or has_magnitude) or not has_period:
                    continue
                if unit.dimension == "percent" and abs(value) > 1000:
                    continue
                if unit.dimension == "percent" and not _PCT_METRIC.search(metric_src):
                    continue

                mkey, base = canonical_metric(metric_src)
                if not base:
                    continue
                scope = detect_scope(metric_src + " " + text, doc.name, doc.vintage)
                if not scope.segment and seg:
                    scope.segment = seg
                quals = []
                if re.search(r"\b(yoy|qoq|y-o-y|q-o-q)\b", metric_src + " " + text, re.I):
                    quals.append("delta")
                conf = 0.5 + (0.2 if has_real_unit else 0.1) + \
                       (0.2 if len(base.split()) >= 2 else 0.0)
                fid = Fact.make_id(doc.doc_id, pno, mkey, value_raw.strip(),
                                   period.key(), text)
                facts.append(Fact(
                    id=fid, doc_id=doc.doc_id, doc_name=doc.name, page=pno,
                    bbox=ln.bbox, metric=metric_src[:120], metric_key=mkey,
                    value=value, value_base=to_base(value, unit),
                    value_raw=value_raw.strip(),
                    unit=unit.__dict__, period=period.__dict__, scope=scope.__dict__,
                    qualifiers=quals, evidence=text, extractor="heuristic",
                    confidence=round(min(conf, 0.95), 2)))
    return facts


def extract_from_tables(doc: Doc) -> List[Fact]:
    facts: List[Fact] = []
    for c in doc.tables:
        if not re.search(r"\d", c.text):
            continue
        if _YEAR_RE.match(c.text.strip("()+")):
            continue
        # unit hint from row/col header (e.g. "₹ Cr", "% margin", "(Days)")
        unit_ctx = f"{c.text} {c.row_header} {c.col_header}"
        unit = parse_unit(unit_ctx)
        value = parse_value(c.text)
        if value is None:
            continue
        metric_disp = _clean_metric(c.row_header.strip() or c.col_header.strip())
        if not metric_disp or len(_ALPHA.findall(metric_disp)) < 2:
            continue
        mkey, base = canonical_metric(metric_disp)
        if not base or len(base.split()) < 1:
            continue
        period = parse_period(f"{c.col_header} {c.row_header}")
        if period.kind == "unknown":
            period = parse_period(c.text)
        has_period = period.kind != "unknown"
        has_real_unit = unit.dimension in ("currency", "mass", "percent", "days")
        has_magnitude = bool(_UNIT_IN_TOKEN.search(unit_ctx))
        # a table cell must carry a real unit or an explicit magnitude, AND a
        # period — a bare integer under a period column is usually noise.
        if not (has_real_unit or has_magnitude):
            continue
        if not has_period:
            continue
        scope = detect_scope(metric_disp + " " + c.col_header + " " +
                             getattr(c, "table_title", ""), doc.name, doc.vintage)
        conf = 0.55 + (0.2 if has_period else 0) + (0.15 if unit.dimension != "count" else 0)
        fid = Fact.make_id(doc.doc_id, c.page, mkey, c.text,
                           period.key(), f"{c.row_header}|{c.col_header}")
        facts.append(Fact(
            id=fid, doc_id=doc.doc_id, doc_name=doc.name, page=c.page,
            bbox=c.bbox, metric=metric_disp[:120], metric_key=mkey,
            value=value, value_base=to_base(value, unit), value_raw=c.text,
            unit=unit.__dict__, period=period.__dict__, scope=scope.__dict__,
            qualifiers=[c.col_header] if c.col_header else [],
            evidence=f"{c.row_header} — {c.col_header}: {c.text}".strip(" —:"),
            extractor="heuristic-table", confidence=round(min(conf, 0.95), 2)))
    return facts


def extract_heuristic(doc: Doc) -> List[Fact]:
    facts = (extract_from_lines(doc) + extract_from_tables(doc)
             + extract_from_grids(doc))
    # de-duplicate identical (metric_key, value, period, page)
    seen, out = set(), []
    for f in facts:
        k = (f.metric_key, round(f.value, 4), f.period["label"], f.doc_id, f.page)
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out
