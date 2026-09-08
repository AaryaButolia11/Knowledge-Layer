"""
evidence.py
===========
Grounding you can *see*. Every fact stores the page and bounding box it came
from, so we can re-open the source PDF and render exactly that region as a small
PNG. The UI shows this crop next to each relationship, which is how a reviewer
verifies — without trusting the model — that a claimed number really appears in
the document where we say it does.
"""
from __future__ import annotations
import json
from typing import Optional

import pymupdf


def _render(path: str, page: int, bbox, pad: int = 8, zoom: float = 2.0) -> bytes:
    d = pymupdf.open(path)
    pg = d[page]
    x0, y0, x1, y1 = bbox
    rect = pymupdf.Rect(max(0, x0 - pad), max(0, y0 - pad),
                        min(pg.rect.width, x1 + pad),
                        min(pg.rect.height, y1 + pad))
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect)
    return pix.tobytes("png")


def crop_for_fact(store, fact_id: str, zoom: float = 2.0) -> Optional[bytes]:
    """Return PNG bytes of the evidence region for a fact, or None."""
    f = store.fact(fact_id)
    if not f:
        return None
    doc = next((d for d in store.documents() if d["doc_id"] == f.doc_id), None)
    if not doc or not doc.get("path"):
        return None
    bbox = f.bbox if isinstance(f.bbox, list) else json.loads(f.bbox)
    try:
        return _render(doc["path"], f.page, bbox, zoom=zoom)
    except Exception:
        return None


def _doc_path(store, doc_id: str) -> Optional[str]:
    doc = next((d for d in store.documents() if d["doc_id"] == doc_id), None)
    return doc.get("path") if doc else None


def page_png(store, doc_id: str, page: int, zoom: float = 1.6) -> Optional[bytes]:
    """Render a whole PDF page as a PNG (used behind the annotation overlay)."""
    path = _doc_path(store, doc_id)
    if not path:
        return None
    try:
        d = pymupdf.open(path)
        pg = d[page]
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")
    except Exception:
        return None


def page_size(store, doc_id: str, page: int) -> Optional[tuple]:
    """Return (width, height) in PDF points for a page, so the frontend can
    scale bounding boxes onto the rendered image."""
    path = _doc_path(store, doc_id)
    if not path:
        return None
    try:
        d = pymupdf.open(path)
        r = d[page].rect
        return (r.width, r.height)
    except Exception:
        return None


# colour per relationship type (RGB 0..1) used for the real PDF highlights
_ANNOT_COLOR = {
    "contradict":  (0.905, 0.361, 0.325),   # red
    "reconciled":  (0.882, 0.647, 0.247),   # amber
    "corroborate": (0.220, 0.725, 0.541),   # green
}
_ANNOT_TITLE = {
    "contradict": "Contradiction", "reconciled": "Reconciled",
    "corroborate": "Corroborated",
}
_ANNOT_RANK = {"contradict": 3, "reconciled": 2, "corroborate": 1}


def annotated_pdf(store, doc_id: str) -> Optional[bytes]:
    """
    Return the *source PDF itself* with real highlight annotations drawn over
    every fact that takes part in a relationship, coloured by type, and with the
    system's reasoning embedded as each highlight's popup note — so hovering the
    highlight in any PDF viewer shows why it was flagged. Downloadable as a file.
    """
    path = _doc_path(store, doc_id)
    if not path:
        return None
    facts = {f.id: f for f in store.facts(doc_id=doc_id)}
    if not facts:
        return None

    # each fact is coloured by the strongest relationship it appears in
    best: dict = {}
    for r in store.relationships():
        for me, other in ((r["fact_a"], r["fact_b"]), (r["fact_b"], r["fact_a"])):
            if me not in facts:
                continue
            rank = _ANNOT_RANK.get(r["type"], 0)
            if me not in best or rank > best[me][0]:
                best[me] = (rank, r["type"], r["explanation"], store.fact(other))

    try:
        doc = pymupdf.open(path)
        for fid, (rank, typ, reason, other) in best.items():
            f = facts[fid]
            bbox = f.bbox if isinstance(f.bbox, list) else json.loads(f.bbox)
            page = doc[f.page]
            rect = pymupdf.Rect(*bbox)
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=_ANNOT_COLOR.get(typ, (1, 1, 0)))
            title = _ANNOT_TITLE.get(typ, "Flag")
            cp = ""
            if other:
                cp = (f"  vs  {other.value_raw} "
                      f"({other.period.get('label')}, {other.doc_name})")
            content = (f"[{title}] {f.metric} = {f.value_raw} "
                       f"({f.period.get('label')}){cp}\n\n{reason}")
            annot.set_info(title=title, content=content)
            annot.update(opacity=0.45)
        return doc.tobytes(deflate=True)
    except Exception:
        return None
