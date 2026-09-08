"""
ground.py
=========
The closed-loop hallucination check. Every extracted fact claims a value and a
piece of supporting evidence. Here we verify the claimed number actually appears
in that evidence span. If it doesn't, the fact is marked ungrounded (and can be
dropped or shown with a warning). This is what stops an LLM extractor from
inventing figures, and it also catches heuristic mis-associations.
"""
from __future__ import annotations
import re
from typing import List
from models import Fact


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s)


def verify(fact: Fact) -> bool:
    raw = _digits(fact.value_raw)
    ev = _digits(fact.evidence)
    if not raw:
        return False
    if raw in ev:
        return True
    # tolerate a leading/trailing rounding digit mismatch on long numbers
    if len(raw) >= 3 and raw[:-1] in ev:
        return True
    # value formatted without thousands separators
    if str(int(abs(fact.value))) in ev:
        return True
    return False


def ground_facts(facts: List[Fact], drop_ungrounded: bool = False) -> List[Fact]:
    out = []
    for f in facts:
        f.grounded = verify(f)
        if f.grounded:
            f.confidence = min(0.99, f.confidence + 0.05)
        else:
            f.confidence = max(0.1, f.confidence - 0.25)
        if drop_ungrounded and not f.grounded:
            continue
        out.append(f)
    return out
