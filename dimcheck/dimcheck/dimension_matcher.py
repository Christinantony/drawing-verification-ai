"""
Find a checklist row's nominal dimension on the drawing (within its zone)
and read the tolerance printed next to it.

Everything is measured in the dimension text's own reading frame (u = along
the text, v = down across it), so vertical dimensions are handled exactly
like horizontal ones. Three printed forms are recognised:

  symmetric   "101.2 ±0.1"  - SolidWorks draws the "±" as a graphic, so the
                              text layer only has "101.2" then a bare "0.1"
                              on the same baseline. A bare small number
                              right after the dimension is read as ±.
  stacked     "508.1 -0.2" with "0.0" above it: upper and lower limits
                              printed as two figures one over the other,
                              signs often as separate "-"/"+" words.
  fit code    "6.0 f7" / "H7": reported, never converted (ISO 286 lookup
                              depends on the size range).

Matching is by value, not text: the checklist's "102" finds the drawing's
"102.0", "Ø20.7" finds "20.7" (the Ø is a graphic), "*106.1" finds "*106.1".
The *printed* text decides which general-tolerance tier applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .pdf_layout import Word
from .zoning import ZoneGrid

FIT_CODE_RE = re.compile(r"^[A-Za-z]{1,2}\d{1,2}$")
NUM_RE = re.compile(r"^[+\-±]?\d*\.?\d+$")
SIGN_RE = re.compile(r"^[+\-±]$")
PREFIX_RE = re.compile(r"^[*Ø⌀∅R]+")

# If nothing matches inside the zone's cell, widen the search by this many
# points (dimension text often sits just outside the cell its leader
# points into). Result is then marked medium confidence.
WIDEN_MARGIN = 24.0


@dataclass
class Tolerance:
    kind: str                     # "symmetric" | "limits"
    upper: float
    lower: float
    printed: str                  # as read, e.g. "0.0 / -0.2"

    def format(self) -> str:
        """Checklist style: '±0.1', '0/-0.2', '+0.02/+0.01'."""
        def f(x: float) -> str:
            if abs(x) < 1e-12:
                return "0"
            s = f"{abs(x):.4f}".rstrip("0").rstrip(".")
            return ("+" if x > 0 else "-") + s
        if self.kind == "symmetric":
            return "±" + f(self.upper)[1:]
        return f"{f(self.upper)}/{f(self.lower)}"


@dataclass
class MatchResult:
    zone: str
    nominal: str
    matched_token: Optional[Word]
    matched_text: str = ""
    tolerance: Optional[Tolerance] = None
    fit_code: Optional[str] = None
    confidence: str = "low"
    notes: List[str] = field(default_factory=list)

    # Backwards-compatible view used by older callers.
    @property
    def explicit_tolerance(self) -> Optional[str]:
        return self.tolerance.format() if self.tolerance else None


def nominal_value(text: str) -> Optional[float]:
    """Numeric value of a dimension as written on a checklist or drawing:
    '102' -> 102.0, 'Ø20.7' -> 20.7, '*106.1' -> 106.1, 'M3' -> None."""
    t = PREFIX_RE.sub("", (text or "").strip().replace(",", "."))
    t = t.rstrip("°")
    try:
        return float(t) if re.fullmatch(r"\d+(\.\d+)?", t) else None
    except ValueError:
        return None


def printed_decimals(text: str) -> Optional[int]:
    t = PREFIX_RE.sub("", text.strip()).rstrip("°")
    m = re.fullmatch(r"\d+(?:\.(\d+))?", t)
    if not m:
        return None
    return len(m.group(1)) if m.group(1) else 0


def _extent(w: Word, ref: Word) -> Tuple[float, float, float, float]:
    """(u_min, u_max, v_min, v_max) of w's box in ref's reading frame."""
    us, vs = [], []
    for x, y in ((w.x0, w.y0), (w.x1, w.y0), (w.x0, w.y1), (w.x1, w.y1)):
        u, v = ref.frame(x, y)
        us.append(u)
        vs.append(v)
    return min(us), max(us), min(vs), max(vs)


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace("±", ""))
    except ValueError:
        return None


def read_tolerance(words: Sequence[Word], anchor: Word) -> Tuple[Optional[Tolerance], Optional[str], List[str]]:
    """Tolerance figures printed right after `anchor`, in its reading frame.
    Returns (tolerance, fit_code, notes)."""
    au0, au1, av0, av1 = _extent(anchor, anchor)
    h = max(1.0, av1 - av0)
    a_vc = (av0 + av1) / 2
    cand = []
    for w in words:
        if w is anchor:
            continue
        d = w.dir[0] * anchor.dir[0] + w.dir[1] * anchor.dir[1]
        if d < 0.9:
            continue  # different text direction
        if not (NUM_RE.match(w.text) or SIGN_RE.match(w.text) or FIT_CODE_RE.match(w.text)):
            continue
        u0, u1, v0, v1 = _extent(w, anchor)
        dv = (v0 + v1) / 2 - a_vc
        if u0 < au1 - 0.15 * h or u0 - au1 > 2.5 * h or abs(dv) > 1.3 * h:
            continue
        cand.append((u0, u1, dv, w))
    cand.sort(key=lambda t: t[0])

    # Keep only a chain of figures starting right after the dimension text:
    # anything after a gap is a different callout.
    chain = []
    reach = au1
    for u0, u1, dv, w in cand:
        if u0 - reach > 1.3 * h:
            break
        chain.append((u0, u1, dv, w))
        reach = max(reach, u1)

    fit = next((w.text for _, _, _, w in chain if FIT_CODE_RE.match(w.text)), None)
    figs = [(u0, u1, dv, w) for u0, u1, dv, w in chain if not FIT_CODE_RE.match(w.text)]
    if not figs:
        return None, fit, []

    # Group into text rows by dv (baseline row vs raised row).
    rows: List[List[tuple]] = []
    for item in sorted(figs, key=lambda t: t[2]):
        if rows and abs(item[2] - rows[-1][-1][2]) < 0.4 * h:
            rows[-1].append(item)
        else:
            rows.append([item])

    def row_value(row) -> Tuple[Optional[float], str, bool]:
        row = sorted(row, key=lambda t: t[0])
        text = "".join(w.text for *_, w in row)
        sym = "±" in text
        sign = text[0] if text and text[0] in "+-" else ""
        num = re.sub(r"^[+\-±]+", "", text)
        v = _num(num)
        if v is None or v >= 5:  # a tolerance is small; otherwise it's another dimension
            return None, text, sym
        return (-v if sign == "-" else v) if sign else v, text, sym

    def mean_dv(row) -> float:
        return sum(t[2] for t in row) / len(row)

    notes: List[str] = []
    if len(rows) >= 2:
        # Stacked limits sit one over the other around the dimension's line:
        # the upper figure raised, the lower on (or below) the baseline.
        # Two rows both above the baseline belong to a dimension above.
        if not (mean_dv(rows[0]) < 0.1 * h and mean_dv(rows[-1]) > -0.35 * h):
            return None, fit, notes
        up_v, up_t, _ = row_value(rows[0])
        lo_v, lo_t, _ = row_value(rows[-1])
        if up_v is None or lo_v is None:
            return None, fit, notes
        # An unsigned non-zero lower limit printed without its "-" (sign lost
        # as a graphic) is still a minus deviation.
        if lo_t and lo_t[0] not in "+-" and lo_v > 0:
            lo_v = -lo_v
            notes.append(f"Lower limit '{lo_t}' printed without a sign; read as -{lo_t}.")
        if up_t and up_t[0] not in "+-" and up_v < 0:
            up_v = -up_v
        return Tolerance("limits", up_v, lo_v, f"{up_t} / {lo_t}"), fit, notes

    if abs(mean_dv(rows[0])) > 0.35 * h:
        return None, fit, notes  # a single figure off the baseline belongs to a neighbour
    v, t, sym = row_value(rows[0])
    if v is None:
        return None, fit, notes
    if sym or t[:1] not in "+-":
        return Tolerance("symmetric", abs(v), -abs(v), t), fit, notes
    # A single signed figure ("+0.1") is a one-sided limit: the other side is 0.
    notes.append(f"Single limit '{t}' read as one-sided; the other limit taken as 0.")
    return (Tolerance("limits", v, 0.0, t) if v > 0 else Tolerance("limits", 0.0, v, t)), fit, notes


def _tokens_in_box(tokens, box):
    x_min, x_max, y_min, y_max = box
    return [t for t in tokens if x_min <= t.xc <= x_max and y_min <= t.yc <= y_max]


def _widen(box, margin):
    x_min, x_max, y_min, y_max = box
    return (x_min - margin, x_max + margin, y_min - margin, y_max + margin)


def find_dimension(tokens: List[Word], grid: ZoneGrid, zone: str, dimension: str) -> MatchResult:
    box = grid.cell_bounds(zone)
    if box is None:
        return MatchResult(zone=zone, nominal=dimension, matched_token=None, confidence="low",
                           notes=[f"Zone '{zone}' doesn't match this drawing's detected grid "
                                  f"(rows {grid.row_labels}, columns {grid.col_labels})."])

    value = nominal_value(dimension)
    star = dimension.strip().startswith("*")

    def search(cands: List[Word]) -> List[Word]:
        if value is not None:
            hits = [t for t in cands if nominal_value(t.text) is not None
                    and abs(nominal_value(t.text) - value) < 1e-9]
            # A "*" (key characteristic) on the checklist prefers the same mark.
            if star and any(t.text.startswith("*") for t in hits):
                hits = [t for t in hits if t.text.startswith("*")]
            return hits
        target = dimension.strip().split()[0].upper()
        return [t for t in cands if t.text.upper() == target]

    confidence, notes = "high", []
    hits = search(_tokens_in_box(tokens, box))
    if not hits:
        hits = search(_tokens_in_box(tokens, _widen(box, WIDEN_MARGIN)))
        if hits:
            confidence = "medium"
            notes.append(f"Dimension text found just outside zone {zone}'s cell; check the zone.")
    if not hits:
        return MatchResult(zone=zone, nominal=dimension, matched_token=None, confidence="low",
                           notes=[f"No '{dimension}' found in or near zone {zone}."])

    readings = []
    for h in hits:
        tol, fit, tnotes = read_tolerance(tokens, h)
        readings.append((h, tol, fit, tnotes))
    # Prefer a hit that has an explicit tolerance; flag if hits disagree.
    distinct = {(r[1].format() if r[1] else None, r[2]) for r in readings}
    if len(distinct) > 1:
        notes.append(f"'{dimension}' appears {len(hits)} times in zone {zone} with different "
                     f"tolerances ({', '.join(str(d[0] or d[1] or 'none') for d in distinct)}); "
                     "used the first explicit one - confirm which the checklist means.")
        confidence = "medium" if confidence == "high" else confidence
    best = next((r for r in readings if r[1] or r[2]), readings[0])
    h, tol, fit, tnotes = best
    return MatchResult(zone=zone, nominal=dimension, matched_token=h, matched_text=h.text,
                       tolerance=tol, fit_code=fit, confidence=confidence, notes=notes + tnotes)
