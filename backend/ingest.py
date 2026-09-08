"""
ingest.py
=========
Layout-aware ingestion. Every unit of text keeps its page number and bounding
box so any fact extracted from it can be grounded back to an exact spot on the
page. We surface two views of each page:

  * lines  – individual text lines with bbox (good for slide-style "value then
             label" layouts and prose)
  * tables – detected tables as row/col cells with bbox (good for financial
             statements and macro appendix tables, where most numbers live)

No document-specific rules — this works on any digitally-authored PDF.
"""
from __future__ import annotations
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

import pymupdf


@dataclass
class Line:
    page: int
    bbox: List[float]
    text: str
    y: float


@dataclass
class TableCell:
    page: int
    bbox: List[float]
    text: str
    row: int
    col: int
    row_header: str
    col_header: str


@dataclass
class Doc:
    doc_id: str
    name: str
    n_pages: int
    lines: List[Line]
    tables: List[TableCell]
    vintage: str          # best-effort publication date (YYYY-MM-DD) or ""
    path: str


_DATE_RE = re.compile(
    r"(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})"
    r"|(?:date[:\s]+)(\w+)\s+(\d{1,2}),?\s+(\d{4})", re.I)
_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _guess_vintage(pages_text: List[str]) -> str:
    """Best-effort publication date from the first couple of pages."""
    blob = "\n".join(pages_text[:3]).lower()
    m = _DATE_RE.search(blob)
    if m:
        if m.group(2):
            d, mon, y = int(m.group(1)), _MON[m.group(2)[:3]], int(m.group(3))
        else:
            mon, d, y = _MON.get(m.group(4)[:3].lower(), 1), int(m.group(5)), int(m.group(6))
        return f"{y}-{mon:02d}-{d:02d}"
    # fall back to a bare year (report titles like "2024-25")
    m = re.search(r"(20\d{2})", blob)
    return f"{m.group(1)}-01-01" if m else ""


def _doc_id(path: str) -> str:
    with open(path, "rb") as f:
        h = hashlib.sha1(f.read()).hexdigest()[:12]
    return f"d_{h}"


def ingest(path: str) -> Doc:
    name = os.path.basename(path)
    d = pymupdf.open(path)
    lines: List[Line] = []
    tables: List[TableCell] = []
    pages_text: List[str] = []

    for pno in range(d.page_count):
        page = d[pno]
        pd = page.get_text("dict")
        page_txt = []
        for block in pd.get("blocks", []):
            for ln in block.get("lines", []):
                txt = "".join(s["text"] for s in ln["spans"]).strip()
                if not txt:
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                lines.append(Line(pno, [x0, y0, x1, y1], txt, y0))
                page_txt.append(txt)
        pages_text.append("\n".join(page_txt))

        # tables (best-effort; skipped silently if the finder fails)
        try:
            found = page.find_tables()
            for t in found.tables:
                cells = t.extract()
                if not cells or len(cells) < 2:
                    continue
                header = [(c or "").strip() for c in cells[0]]
                row_header_col = 0
                for ri, row in enumerate(cells[1:], start=1):
                    rh = (row[row_header_col] or "").strip()
                    for ci, val in enumerate(row):
                        val = (val or "").strip()
                        if not val:
                            continue
                        ch = header[ci] if ci < len(header) else ""
                        tables.append(TableCell(
                            pno, list(t.bbox), val, ri, ci, rh, ch))
        except Exception:
            pass

    vintage = _guess_vintage(pages_text)
    return Doc(_doc_id(path), name, d.page_count, lines, tables, vintage, path)


def render_evidence_crop(path: str, page: int, bbox: List[float],
                         out_path: str, pad: int = 8, zoom: float = 2.0) -> str:
    """Render a cropped PNG of the evidence region for the UI."""
    d = pymupdf.open(path)
    pg = d[page]
    x0, y0, x1, y1 = bbox
    rect = pymupdf.Rect(max(0, x0 - pad), max(0, y0 - pad),
                        min(pg.rect.width, x1 + pad),
                        min(pg.rect.height, y1 + pad))
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect)
    pix.save(out_path)
    return out_path
