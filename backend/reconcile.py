"""
reconcile.py
============
The reasoning core. Comparison is deterministic and explainable, never an LLM
guess. Two facts are only compared when they share a base metric AND a unit
dimension. Then we mechanically inspect their dimensions:

  same metric + same period + same scope + same unit-dim
       values agree  -> corroborate   (strongest when cross-page / cross-doc)
       values differ -> contradict    (genuine, if nothing explains the gap)

  same metric, exactly one qualifying dimension differs
       -> reconciled, naming the dimension:
          * definition   (EBITDA vs Adjusted EBITDA; rev-from-services vs -customers)
          * basis        (consolidated vs standalone)
          * period       (a full year vs one of its quarters)
          * vintage      (same period from different publishers / data vintages)

Unit *expression* differences collapse under normalisation, so "1.4 Mn Tons"
and "1,429 '000 Tons" corroborate.

Precision guards keep the layer readable: facts without a known period are not
compared; ultra-generic metric names are skipped; and a metric/period/scope
group exposing many distinct values is treated as a parsing artefact rather than
a storm of contradictions.
"""
from __future__ import annotations
import re
from itertools import combinations
from typing import List, Dict, Tuple

from models import Fact, Relationship

REL_TOL = 0.03          # relative tolerance for "values agree"
ABS_TOL = 0.6           # absolute tolerance for tiny crore figures
MAX_DISTINCT = 3        # >this many distinct values in one cell-group => artefact
CONFUSABLE = 3.0        # reconcile only when values are within this factor

GENERIC = {
    "total", "cost", "income", "expense", "expenses", "value", "number",
    "growth", "rate", "margin", "share", "price", "amount", "balance",
    "change", "net", "gross", "others", "other", "sub total", "as", "at",
    "cent", "percent", "load", "saving", "savings", "index", "ratio", "level",
    "and", "nine", "months", "quarter", "year", "period", "term", "date",
}


_COUNT_NOUN = re.compile(
    r"\b(shipments?|parcels?|orders?|consignments?|centres?|centers?|"
    r"employees?|workers?|customers?|clients?|stores?|outlets?|facilities|"
    r"warehouses?|hubs?|gateways?|vehicles?|trucks?|trailers?|pin[\s-]?codes?|"
    r"stations?|branches?|partners?|dealers?|franchisees?|agents?)\b", re.I)


def _reconcilable(f: Fact) -> bool:
    """Only well-typed level measurements enter the reasoning layer. YoY/QoQ
    deltas are growth rates, not levels, and are excluded. Currency / mass /
    percent / days always qualify. A count qualifies only when its metric names
    a real countable entity (shipments, centres, employees …) — this keeps
    'partner delivery centres' in and stray table integers out."""
    if "delta" in (f.qualifiers or []):
        return False
    d = f.dim()
    if d in ("currency", "mass", "percent", "days"):
        return True
    if d == "count" and len(f.metric_key.split()) >= 2 and _COUNT_NOUN.search(f.metric_key):
        return True
    return False


def _metric_ok(mkey: str, dim: str) -> bool:
    if _too_generic(mkey):
        return False
    # single-word metrics collide easily; allow them only for currency figures
    # (ebitda, pat, revenue), where they are almost always real line items.
    if len(mkey.split()) < 2 and dim != "currency":
        return False
    return True


MACRO = re.compile(r"\b(gdp|gva|inflation|cpi|wpi|deficit|cad|fiscal|repo|"
                   r"forex|reserves|current account|growth)\b", re.I)


def _is_macro(mkey: str) -> bool:
    return bool(MACRO.search(mkey))


def _agree(a: float, b: float) -> bool:
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-9)
    if abs(a - b) <= ABS_TOL and denom < 5:
        return True
    return abs(a - b) / denom <= REL_TOL


def _period_key(f: Fact) -> str:
    p = f.period
    k = p.get("kind")
    if k == "quarter":
        return f"Q{p.get('quarter')}-FY{p.get('fy')}"
    if k == "fy":
        return f"FY{p.get('fy')}"
    if k in ("month", "point"):
        # a *snapshot* on a fiscal quarter-end IS that quarter, so
        # "as on March 31, 2024" compares against "Q4 FY24". A bare calendar
        # year (start != end) is annual, not a quarter.
        start, end = p.get("start"), p.get("end")
        if end and start == end and len(end) >= 7:
            yr, mo = int(end[:4]), int(end[5:7])
            qmap = {3: (4, yr), 6: (1, yr + 1), 9: (2, yr + 1), 12: (3, yr + 1)}
            if mo in qmap:
                q, fy = qmap[mo]
                return f"Q{q}-FY{fy}"
        return f"AT-{end}"
    if k == "inception":
        return "INCEPTION"
    return p.get("label") or "UNKNOWN"


def _scope_key(f: Fact) -> str:
    s = f.scope
    return f"{s.get('basis') or '-'}|{s.get('segment') or '-'}|{s.get('definition')}"


def _cmp_value(f: Fact) -> float:
    return f.value if f.dim() == "percent" else f.value_base


def _u(f: Fact) -> str:
    return {"currency": " Cr", "mass": " t", "percent": "%",
            "days": " d", "count": ""}.get(f.dim(), "")


def _gap(a: Fact, b: Fact) -> str:
    va, vb = _cmp_value(a), _cmp_value(b)
    denom = max(abs(va), abs(vb), 1e-9)
    return f"{va:g} vs {vb:g}{_u(a)}, {abs(va-vb)/denom*100:.0f}% apart"


def _mk(a: Fact, b: Fact, typ: str, dim, expl: str, conf: float) -> Relationship:
    return Relationship(
        id=Relationship.make_id(a.id, b.id, typ), type=typ,
        fact_a=a.id, fact_b=b.id, metric_key=a.metric_key,
        explanation=expl, dimension=dim, confidence=round(min(conf, 0.98), 2),
        cross_document=(a.doc_id != b.doc_id))


def _too_generic(mkey: str) -> bool:
    return (not mkey) or len(mkey) < 3 or mkey in GENERIC


def build_relationships(facts: List[Fact]) -> List[Relationship]:
    facts = [f for f in facts
             if f.period.get("kind") != "unknown" and _reconcilable(f)]
    by_metric: Dict[str, List[Fact]] = {}
    for f in facts:
        by_metric.setdefault(f.metric_key, []).append(f)

    rels: List[Relationship] = []
    seen = set()

    def add(r: Relationship):
        if r.id in seen:
            return
        seen.add(r.id)
        rels.append(r)

    for mkey, group in by_metric.items():
        dim0 = group[0].dim()
        if not _metric_ok(mkey, dim0) or len(group) < 2:
            continue

        # ---------- exact-cell comparisons: (period, scope, dimension) ------- #
        cells: Dict[Tuple[str, str, str], List[Fact]] = {}
        for f in group:
            cells.setdefault((_period_key(f), _scope_key(f), f.dim()), []).append(f)

        for (_pk, _sk, _dim), items in cells.items():
            if len(items) < 2:
                continue
            reps: Dict[float, Fact] = {}
            for f in sorted(items, key=lambda x: -x.confidence):
                reps.setdefault(round(_cmp_value(f), 3), f)
            distinct = list(reps.items())
            if len(distinct) > MAX_DISTINCT:
                continue  # artefact: one metric name catching many cells

            if len(distinct) == 1:
                fs = items
                pair = None
                for a, b in combinations(fs, 2):
                    if a.doc_id != b.doc_id:
                        pair = (a, b); break
                if not pair:
                    for a, b in combinations(fs, 2):
                        if a.page != b.page:
                            pair = (a, b); break
                if pair:
                    a, b = pair
                    unit_note = ""
                    if a.unit.get("raw") != b.unit.get("raw") and a.dim() != "percent":
                        unit_note = (f" Expressed in different units "
                                     f"({a.value_raw} vs {b.value_raw}) but equal after "
                                     f"normalisation.")
                    expl = ("Same metric, period and scope; values agree." + unit_note)
                    conf = 0.8 + (0.15 if a.doc_id != b.doc_id else 0.05)
                    add(_mk(a, b, "corroborate",
                            "unit" if unit_note else None, expl, conf))
            else:
                # A genuine contradiction is a PAIRWISE disagreement (two
                # sources, two values). If one metric/period/scope cell shows
                # three or more different values, the metric key is too coarse
                # (e.g. per-segment rows sharing a name) — an artefact, not a
                # contradiction. Skip those.
                if len(distinct) != 2:
                    continue
                for (va, fa), (vb, fb) in combinations(distinct, 2):
                    if _agree(va, vb):
                        continue
                    if "\n" in fa.evidence or "\n" in fb.evidence:
                        continue  # merged block -> unreliable
                    cross = fa.doc_id != fb.doc_id
                    vint_diff = (fa.scope.get("source_vintage") !=
                                 fb.scope.get("source_vintage"))
                    macro = _is_macro(mkey) or fa.scope.get("forecast") or fb.scope.get("forecast")
                    if cross and vint_diff and macro:
                        expl = ("Same metric and period reported by different "
                                "sources/vintages (" +
                                f"{fa.doc_name[:22]} vs {fb.doc_name[:22]}); the gap "
                                f"({_gap(fa, fb)}) is explained by publisher and data "
                                "vintage rather than a true conflict.")
                        add(_mk(fa, fb, "reconciled", "vintage", expl, 0.72))
                    else:
                        where = ("across documents" if cross else
                                 "within the same document (possible source error)")
                        expl = (f"Same metric, period and scope, but values disagree "
                                f"({_gap(fa, fb)}). No period, unit, definition or "
                                f"scope difference explains it — likely a genuine "
                                f"contradiction {where}.")
                        add(_mk(fa, fb, "contradict", None, expl,
                                0.72 if cross else 0.62))

        # ---------- cross-dimension reconciles within the metric ------------- #
        emitted = set()   # (period_key, reconcile_dimension) -> one pair only
        for a, b in combinations(group, 2):
            if a.dim() != b.dim():
                continue
            va, vb = _cmp_value(a), _cmp_value(b)
            if (va >= 0) != (vb >= 0):
                continue  # a positive vs a negative value is not a reconciliation
            ratio = max(abs(va), abs(vb), 1e-9) / max(min(abs(va), abs(vb)), 1e-9)
            same_period = _period_key(a) == _period_key(b)
            seg_same = a.scope.get("segment") == b.scope.get("segment")
            def_same = a.scope.get("definition") == b.scope.get("definition")
            basis_same = (a.scope.get("basis") or "") == (b.scope.get("basis") or "")

            if same_period and seg_same and basis_same and not def_same \
                    and ratio <= CONFUSABLE:
                tag = (_period_key(a), a.dim(), "definition")
                if tag in emitted:
                    continue
                emitted.add(tag)
                da, db = a.scope.get("definition"), b.scope.get("definition")
                expl = (f"Same base metric under different definitions "
                        f"('{da}' vs '{db}') for {a.period.get('label')} "
                        f"({va:g} vs {vb:g}{_u(a)}). Apparent contradiction reconciled "
                        f"by DEFINITION (e.g. reported vs adjusted, or "
                        f"services vs customers).")
                add(_mk(a, b, "reconciled", "definition", expl, 0.78))

            elif same_period and seg_same and def_same and not basis_same \
                    and ratio <= CONFUSABLE:
                tag = (_period_key(a), a.dim(), "basis")
                if tag in emitted:
                    continue
                emitted.add(tag)
                expl = (f"Same metric on different reporting bases "
                        f"({a.scope.get('basis')} vs {b.scope.get('basis')}) for "
                        f"{a.period.get('label')}. Reconciled by BASIS.")
                add(_mk(a, b, "reconciled", "basis", expl, 0.75))

            elif seg_same and def_same and basis_same and not same_period:
                ka, kb = a.period, b.period
                fy_match = ka.get("fy") == kb.get("fy") and ka.get("fy") is not None
                kinds = {ka.get("kind"), kb.get("kind")}
                if fy_match and kinds == {"fy", "quarter"} and ratio <= CONFUSABLE:
                    tag = (f"FY{ka.get('fy')}", a.dim(), "period")
                    if tag in emitted:
                        continue
                    emitted.add(tag)
                    expl = (f"Same metric and scope but different periods "
                            f"({a.period.get('label')} vs {b.period.get('label')}): a "
                            f"full year versus one of its quarters "
                            f"({va:g} vs {vb:g}{_u(a)}). Apparent contradiction "
                            f"reconciled by PERIOD.")
                    add(_mk(a, b, "reconciled", "period", expl, 0.76))

    order = {"contradict": 0, "reconciled": 1, "corroborate": 2}
    rels.sort(key=lambda r: (order.get(r.type, 3), not r.cross_document, -r.confidence))

    # Corroborations of the same metric across many pages are correct but
    # repetitive; keep the most informative few per metric (cross-document and
    # high-confidence first) so the view stays readable. Contradictions and
    # reconciliations are never capped.
    CORROB_CAP = 6
    RECON_CAP = 5   # per (metric_key, dimension), keeps variety without a wall
    kept, seen_corr, seen_recon = [], {}, {}
    for r in rels:
        if r.type == "corroborate":
            n = seen_corr.get(r.metric_key, 0)
            if n >= CORROB_CAP:
                continue
            seen_corr[r.metric_key] = n + 1
        elif r.type == "reconciled":
            key = (r.metric_key, r.dimension)
            n = seen_recon.get(key, 0)
            if n >= RECON_CAP:
                continue
            seen_recon[key] = n + 1
        kept.append(r)
    return kept
