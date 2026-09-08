"""
models.py
=========
The two objects the whole system revolves around.

A Fact is a *qualified measurement*: a metric + value + unit + period + scope,
tied to exact evidence (doc, page, bounding box, quote). A Relationship is a
typed, explained edge between two facts (corroborate / contradict / reconciled).
"""
from __future__ import annotations
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

from normalize import Unit, Period, Scope


@dataclass
class Fact:
    id: str
    doc_id: str
    doc_name: str
    page: int
    bbox: List[float]                 # [x0, y0, x1, y1] on the page

    metric: str                       # display metric, e.g. "PTL freight tonnage"
    metric_key: str                   # clustering key (canonicalised)
    value: float
    value_base: float                 # value in the unit's canonical base
    value_raw: str

    unit: Dict[str, Any]              # Unit as dict
    period: Dict[str, Any]            # Period as dict
    scope: Dict[str, Any]             # Scope as dict

    qualifiers: List[str] = field(default_factory=list)
    evidence: str = ""                # exact supporting quote
    extractor: str = "heuristic"      # heuristic | llm
    confidence: float = 0.5
    grounded: bool = False            # did value_raw verify back into evidence?

    @staticmethod
    def make_id(doc_id: str, page: int, metric_key: str, value_raw: str,
                period_key: str, evidence: str) -> str:
        h = hashlib.sha1(
            f"{doc_id}|{page}|{metric_key}|{value_raw}|{period_key}|{evidence[:60]}"
            .encode()).hexdigest()[:16]
        return f"f_{h}"

    def dim(self) -> str:
        return self.unit.get("dimension", "count")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Relationship:
    id: str
    type: str                         # corroborate | contradict | reconciled
    fact_a: str
    fact_b: str
    metric_key: str
    explanation: str
    dimension: Optional[str] = None   # which dimension reconciles / conflicts
    confidence: float = 0.5
    cross_document: bool = False

    @staticmethod
    def make_id(a: str, b: str, typ: str) -> str:
        lo, hi = sorted([a, b])
        return f"r_{hashlib.sha1(f'{lo}|{hi}|{typ}'.encode()).hexdigest()[:16]}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
