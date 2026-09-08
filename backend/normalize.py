"""
normalize.py
============
The canonicalisation layer. This is where a raw string like "₹(452) Cr" or
"1,429 '000 Tons" or "FY2024-25" becomes a structured, comparable object.

Nothing here is Delhivery- or document-specific. It only knows about generic
financial / macro vocabulary (crore, million, FY, Q, consolidated, adjusted ...),
so it generalises to any Indian corporate or macro PDF.

Two facts are only ever compared *after* they pass through here.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# --------------------------------------------------------------------------- #
#  VALUES
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(
    r"""
    (?P<paren>\()?                 # optional opening paren  -> negative
    \s*
    (?P<sign>[-+])?
    (?P<num>\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<plus>\+)?                  # trailing + (e.g. "2.8 Bn+")
    (?(paren)\s*\))                # closing paren if we opened one
    """,
    re.VERBOSE,
)


def parse_value(raw: str) -> Optional[float]:
    """Parse a single numeric token. Parentheses => negative (accounting)."""
    m = _NUM_RE.search(raw)
    if not m:
        return None
    num = float(m.group("num").replace(",", ""))
    if m.group("paren") or m.group("sign") == "-":
        num = -num
    return num


# --------------------------------------------------------------------------- #
#  UNITS  (dimension + factor to a canonical base)
# --------------------------------------------------------------------------- #
# canonical bases:  currency -> crore | mass -> tonne | count -> unit
_MAG = {  # magnitude word -> multiplier in *base units of that magnitude*
    "cr": 1e7, "crore": 1e7, "crores": 1e7,
    "lakh": 1e5, "lac": 1e5, "lakhs": 1e5,
    "bn": 1e9, "billion": 1e9,
    "mn": 1e6, "million": 1e6, "mln": 1e6,
    "k": 1e3, "'000": 1e3, "000": 1e3, "thousand": 1e3, "thousands": 1e3,
}

_CURRENCY_HINT = re.compile(r"(₹|\brs\.?\b|\binr\b|\brupees?\b)", re.I)
# monetary line items are currency even when the ₹/Cr sits in a table title we
# didn't capture. Indian filings/decks quote these in crore by default, so a
# bare "8,142" under "Revenue" means ₹8,142 Cr, and "81,415.38 million" converts.
_MONETARY = re.compile(
    r"\b(revenue|turnover|ebitda|ebit|pbt|pat|patmi|profit|networth|"
    r"net worth|capex|gross profit|operating profit|contribution margin)\b", re.I)
# a metric that is really a ratio / macro share, so NOT a currency amount even
# though it may contain a monetary word ("short-term debt ratio", "% of GDP").
_RATIOISH = re.compile(r"\b(ratio|gdp|gva|share|proportion|per cent|percent)\b", re.I)
_MASS_HINT = re.compile(r"\b(tons?|tonnes?|mt)\b", re.I)
_PCT_HINT = re.compile(r"%|percent|per cent|bps|basis points?", re.I)
_DAYS_HINT = re.compile(r"\bdays?\b", re.I)


@dataclass
class Unit:
    dimension: str = "count"     # currency | mass | percent | count | days | ratio
    canonical: str = "unit"      # crore | tonne | percent | unit | day
    to_base: float = 1.0         # multiply raw value by this -> value in canonical base
    raw: str = ""

    def key(self) -> str:
        return self.dimension


def parse_unit(context: str) -> Unit:
    """
    Infer the unit from a short context string (the value token + surrounding
    words, e.g. "₹8,142 Cr" or "1.4 Mn Tons" or "18%").
    """
    c = context.lower()
    mag = 1.0
    mag_word = ""
    # detect magnitude word
    for w in ("crore", "crores", "cr", "lakhs", "lakh", "lac",
              "billion", "bn", "million", "mln", "mn",
              "thousand", "thousands", "'000", "000", "k"):
        if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", c):
            mag = _MAG[w]
            mag_word = w
            break

    if _PCT_HINT.search(c):
        return Unit("percent", "percent", 1.0, context.strip())
    if _MASS_HINT.search(c):
        return Unit("mass", "tonne", mag, context.strip())      # base = tonnes
    if _DAYS_HINT.search(c):
        return Unit("days", "day", 1.0, context.strip())
    if _CURRENCY_HINT.search(c):
        # base = crore ; convert absolute-rupee magnitudes down to crore
        return Unit("currency", "crore", mag / 1e7, context.strip())
    # monetary line item without an explicit symbol -> crore by convention,
    # unless a magnitude word says otherwise (million/billion/lakh/thousand).
    if _MONETARY.search(c) and not _RATIOISH.search(c):
        if mag_word in ("million", "mn", "mln"):
            tb = 0.1
        elif mag_word in ("billion", "bn"):
            tb = 100.0
        elif mag_word in ("lakh", "lakhs", "lac"):
            tb = 0.01
        elif mag_word in ("thousand", "thousands", "k", "'000", "000"):
            tb = 1e-4
        else:                       # crore or unspecified
            tb = 1.0
        return Unit("currency", "crore", tb, context.strip())
    # bare number with a magnitude word -> a count (shipments, customers, pincodes)
    if mag_word:
        return Unit("count", "unit", mag, context.strip())
    return Unit("count", "unit", 1.0, context.strip())


def to_base(value: float, unit: Unit) -> float:
    """Value expressed in the unit's canonical base."""
    return value * unit.to_base


# --------------------------------------------------------------------------- #
#  PERIODS  (Indian fiscal-year aware)
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_QMONTH = {1: (4, 6), 2: (7, 9), 3: (10, 12), 4: (1, 3)}  # Indian fiscal quarters


@dataclass
class Period:
    label: str = ""              # human label, e.g. "FY24", "Q4 FY24", "Mar-2024"
    kind: str = "unknown"        # fy | quarter | month | point | inception | unknown
    fy: Optional[int] = None     # the *ending* fiscal year, e.g. FY24 -> 2024
    quarter: Optional[int] = None
    start: Optional[str] = None  # "YYYY-MM"
    end: Optional[str] = None    # "YYYY-MM"

    def key(self) -> str:
        if self.kind == "quarter":
            return f"Q{self.quarter}-FY{self.fy}"
        if self.kind == "fy":
            return f"FY{self.fy}"
        if self.kind in ("month", "point"):
            return f"AT-{self.end}"
        if self.kind == "inception":
            return "INCEPTION"
        return self.label or "UNKNOWN"


def _fy_bounds(fy_end: int, quarter: Optional[int]):
    if quarter:
        m0, m1 = _QMONTH[quarter]
        yr = fy_end if quarter == 4 else fy_end - 1
        return f"{yr}-{m0:02d}", f"{yr}-{m1:02d}"
    return f"{fy_end-1}-04", f"{fy_end}-03"


def _fy_from_2digit(d: int) -> int:
    return 2000 + d if d < 90 else 1900 + d


def parse_period(text: str) -> Period:
    """Extract a normalised period from arbitrary text. Best-effort, generic."""
    t = text.strip()
    low = t.lower()

    if "inception" in low or "since inception" in low:
        return Period("since inception", "inception")

    # Q<n> FY<yy>   /   Q<n>FY<yyyy>
    m = re.search(r"q([1-4])\s*fy\s*'?(\d{2,4})", low)
    if m:
        q = int(m.group(1)); yy = int(m.group(2))
        fy = _fy_from_2digit(yy) if yy < 100 else yy
        s, e = _fy_bounds(fy, q)
        return Period(f"Q{q} FY{str(fy)[-2:]}", "quarter", fy, q, s, e)

    # FY<yyyy>-<yy>  (macro convention, e.g. FY2024-25 -> ends 2025)
    m = re.search(r"fy\s*'?(\d{4})\s*[-/]\s*(\d{2})", low)
    if m:
        fy = int(m.group(1)) + 1
        s, e = _fy_bounds(fy, None)
        return Period(f"FY{str(fy)[-2:]}", "fy", fy, None, s, e)

    # FY<yy>-<yy>    (e.g. FY23-24 -> ends 2024)
    m = re.search(r"fy\s*'?(\d{2})\s*[-/]\s*(\d{2})", low)
    if m:
        fy = _fy_from_2digit(int(m.group(2)))
        s, e = _fy_bounds(fy, None)
        return Period(f"FY{str(fy)[-2:]}", "fy", fy, None, s, e)

    # FY<yyyy> or FY<yy>
    m = re.search(r"fy\s*'?(\d{2,4})", low)
    if m:
        yy = int(m.group(1))
        fy = _fy_from_2digit(yy) if yy < 100 else yy
        s, e = _fy_bounds(fy, None)
        return Period(f"FY{str(fy)[-2:]}", "fy", fy, None, s, e)

    # "year ended March 31, 2024"  /  "quarter ended March 31, 2024"
    m = re.search(r"(year|quarter)\s+ended\s+(\w+)\s+\d{1,2},?\s+(\d{4})", low)
    if m:
        kind_word, mon, yr = m.group(1), m.group(2)[:3], int(m.group(3))
        if mon in _MONTHS:
            mo = _MONTHS[mon]
            fy = yr if mo <= 3 else yr + 1
            if kind_word == "year":
                s, e = _fy_bounds(fy, None)
                return Period(f"FY{str(fy)[-2:]}", "fy", fy, None, s, e)
            q = {(1, 3): 4, (4, 6): 1, (7, 9): 2, (10, 12): 3}
            qq = next((v for (a, b), v in q.items() if a <= mo <= b), None)
            s, e = _fy_bounds(fy, qq)
            return Period(f"Q{qq} FY{str(fy)[-2:]}", "quarter", fy, qq, s, e)

    # "March 31, 2024" / "as on March 31 2024"  -> point-in-time (day dropped)
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
                  r"\d{1,2},?\s+((?:19|20)\d{2})", low)
    if m:
        mo = _MONTHS[m.group(1)]
        yr = int(m.group(2))
        if 1990 <= yr <= 2035:
            return Period(f"{m.group(1).title()}-{yr}", "point", None, None,
                          f"{yr}-{mo:02d}", f"{yr}-{mo:02d}")

    # Mar '24 / Mar-24 / March 2024  (year must be a 4-digit year or 'YY —
    # never a day number, so "March 31, 2013" is not read as year 2031)
    m = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*['\-/]?\s*"
        r"((?:19|20)\d{2}|'\d{2})", low)
    if m:
        mo = _MONTHS[m.group(1)]
        ytok = m.group(2).lstrip("'")
        yr = int(ytok) if len(ytok) == 4 else _fy_from_2digit(int(ytok))
        if 1990 <= yr <= 2035:
            return Period(f"{m.group(1).title()}-{yr}", "point", None, None,
                          f"{yr}-{mo:02d}", f"{yr}-{mo:02d}")

    # bare calendar year
    m = re.search(r"(?<!\d)(19|20)(\d{2})(?!\d)", low)
    if m:
        yr = int(m.group(1) + m.group(2))
        if 1990 <= yr <= 2035:
            return Period(str(yr), "point", None, None, f"{yr}-01", f"{yr}-12")

    return Period("", "unknown")


# --------------------------------------------------------------------------- #
#  SCOPE  (basis / segment / definition / forecast)  &  METRIC canonicalisation
# --------------------------------------------------------------------------- #
_SEGMENTS = {
    "express parcel": "express_parcel", "express": "express_parcel",
    "part truckload": "ptl", "ptl": "ptl",
    "truckload": "tl", "tl ": "tl",
    "supply chain": "scs", "scs": "scs",
    "cross border": "cross_border", "cross-border": "cross_border",
}
_BASIS = {
    "consolidated": "consolidated", "standalone": "standalone",
    "pro forma": "proforma", "proforma": "proforma", "pro-forma": "proforma",
}
# definition-changing qualifiers that are stripped from the metric name and
# instead recorded as scope.definition (so "EBITDA" and "Adjusted EBITDA"
# cluster on the same base metric but reconcile via a definition difference)
_DEFINITIONS = [
    "adjusted", "adj.", "adj", "reported", "service", "gross", "net",
    "total", "underlying", "normalised", "normalized",
]
_FORECAST = re.compile(r"\b(projected|forecast|estimate|expected|outlook|target|"
                       r"budget|revised estimate|advance estimate|f\d|p\d|e\b)\b", re.I)
_UNIT_WORDS = re.compile(
    r"(₹|rs\.?|inr|crores?|cr|lakhs?|million|mn|billion|bn|thousand|'?000|tons?|"
    r"tonnes?|per cent|percent|%|days?|bps|yoy|qoq)", re.I)


@dataclass
class Scope:
    basis: Optional[str] = None       # consolidated | standalone | proforma
    segment: Optional[str] = None     # express_parcel | ptl | tl | scs | cross_border
    definition: str = "reported"      # adjusted | service | net | reported | ...
    forecast: bool = False
    source_doc: str = ""
    source_vintage: str = ""          # publication date of the doc (YYYY-MM-DD)

    def key(self) -> str:
        return f"{self.basis or '-'}|{self.segment or '-'}|{self.definition}"


def detect_scope(text: str, source_doc: str = "", vintage: str = "") -> Scope:
    low = " " + text.lower() + " "
    sc = Scope(source_doc=source_doc, source_vintage=vintage)
    for k, v in _SEGMENTS.items():
        if k in low:
            sc.segment = v
            break
    for k, v in _BASIS.items():
        if k in low:
            sc.basis = v
            break
    # revenue "from X" is a definitional variant, not a different metric
    m = re.search(r"revenue\s+(from\s+|for\s+)?(services|customers|operations|traded goods)", low)
    if m:
        sc.definition = m.group(2).replace(" ", "_")
    else:
        for d in _DEFINITIONS:
            if re.search(rf"(?<![a-z]){re.escape(d)}(?![a-z])", low):
                sc.definition = d.replace(".", "")
                break
    if _FORECAST.search(low):
        sc.forecast = True
    return sc


def canonical_metric(raw: str):
    """
    Return (metric_key, base_metric_display).
    Strips units, magnitudes, definition qualifiers, YoY/QoQ noise and periods,
    leaving a stable clustering key.  e.g.
        "Adj. EBITDA / Adj. EBITDA margin" -> "ebitda margin" (definition captured elsewhere)
        "PTL freight tonnage in FY24"      -> "ptl freight tonnage"
        "Revenue from services"            -> "revenue from services"
    """
    s = raw.lower()
    s = re.sub(r"\(.*?\)", " ", s)                     # drop footnote markers
    # collapse "revenue from services/customers/operations" -> "revenue"
    s = re.sub(r"revenue\s+(from\s+|for\s+)?(services|customers|operations|traded goods)",
               "revenue", s)
    s = re.sub(r"fy\s*'?\d{2,4}([-/]\d{2,4})?", " ", s)  # drop FY tokens
    s = re.sub(r"q[1-4]\s*", " ", s)
    s = re.sub(r"\b(yoy|qoq|in|for|the|of|as|at|and|during|during the period)\b", " ", s)
    for d in _DEFINITIONS:
        s = re.sub(rf"(?<![a-z]){re.escape(d)}(?![a-z])", " ", s)
    s = _UNIT_WORDS.sub(" ", s)
    s = re.sub(r"[^a-z ]", " ", s)
    s = s.replace("centres", "centers").replace("centre", "center")
    s = re.sub(r"\s+", " ", s).strip()
    # keep only meaningful head words
    words = [w for w in s.split() if len(w) > 1]
    base = " ".join(words)
    return base, base


def dumps(obj):
    return asdict(obj)
