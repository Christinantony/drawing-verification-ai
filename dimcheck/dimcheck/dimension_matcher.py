"""
Locate a checklist row's nominal dimension on the drawing (within its
stated zone), and read off whatever tolerance -- if any -- is printed
right next to it on the sheet.

This is the piece that turns a flat word list + a zone grid into a single
per-row verdict: "found it, here's the explicit value/fit code printed
beside it", "found it, but nothing's printed beside it -- caller should
fall back to the general tolerance table", or "couldn't find it at all --
flag for manual review." pipeline.py drives the rest (choosing the
general-tolerance tier, writing the checklist back out); this module only
answers "where is this dimension, and what's printed right next to it?"

Handles three real wrinkles seen even in the validated sample drawing:
  - rotated text: a dimension rotated 180 degrees can OCR/extract with its
    characters reversed ("8.5" -> "5.8"); both orderings are checked.
  - multi-token dimensions: e.g. "1.0" + "THRU" on the same line, or a
    thread/feature callout like "M3 TAPPED HOLE" that isn't a plain number
    at all -- matched by its first word instead of a numeric value.
  - zone-boundary imprecision: a dimension's *text* doesn't always sit
    exactly inside the cell its *leader line* points into (the grid is
    derived from the margin labels' positions, not the true tick marks).
    If nothing matches in the exact cell, the search widens and the
    result is marked lower confidence rather than silently accepted.

KNOWN LIMITATION (see project README): asymmetric/unilateral limits are
usually drawn as two stacked figures above/below the dimension line (e.g.
an upper limit of '+0.10' over a lower limit of '0') rather than a single
inline '+x/-y' pair or a '±' value. _find_tolerance_near() only recognizes
the inline forms, so a stacked-figure limit currently falls through to the
general-tolerance table instead of being read as an explicit value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .pdf_layout import Token
from .zoning import ZoneGrid

NUMERIC_RE = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")
TOLERANCE_RE = re.compile(r"±\s*([0-9]+(?:\.[0-9]+)?)")
DUAL_LIMIT_RE = re.compile(r"([+-]\s*0?\.\d+)\s*/\s*([+-]\s*0?\.\d+)")
# Fit/limit codes (ISO 286-style): one or two letters -- lowercase for a
# shaft tolerance class ("g6", "js6"), uppercase for a hole tolerance class
# ("H7", "F8") -- followed by 1-2 digits. Only checked among tokens near an
# already-matched dimension, so it isn't confused with a zone-grid label of
# the same shape (e.g. "F6") living elsewhere on the sheet.
FIT_CODE_RE = re.compile(r"^[A-Za-z]{1,2}\d{1,2}$")

# How far (in PDF points) to look for a tolerance/fit-code annotation next
# to a matched dimension token -- just its immediate neighborhood, not the
# whole zone cell, so an unrelated callout elsewhere in the cell isn't
# mistaken for this dimension's tolerance.
NEARBY_X_RADIUS = 120.0
NEARBY_Y_RADIUS = 40.0

# If nothing matches inside the zone's own cell, widen the search by this
# many points in every direction before giving up -- covers the common case
# described above, where a dimension's text sits just outside the cell
# boundary its leader line actually points into.
WIDEN_MARGIN = 24.0

# How far to the right (points) / how close in `top` a second token has to
# be to count as part of the same multi-word callout as the matched token,
# when building matched_text (e.g. "M3" + "TAPPED" -> "M3 TAPPED").
SAME_LINE_TOP_TOLERANCE = 2.0
SAME_LINE_MAX_GAP = 60.0


@dataclass
class MatchResult:
    zone: str
    nominal: str
    matched_token: Optional[Token]
    matched_text: str = ""
    explicit_tolerance: Optional[str] = None
    fit_code: Optional[str] = None
    confidence: str = "low"
    notes: List[str] = field(default_factory=list)


def _split_nominal(dimension: str) -> Tuple[Optional[str], List[str]]:
    """Split a checklist dimension string into its leading numeric value
    (if any) and the remaining words, e.g. "1.0 THRU" -> ("1.0", ["THRU"]);
    "M3 THREADED HOLE3" -> (None, ["M3", "THREADED", "HOLE3"])."""
    parts = dimension.strip().split()
    if not parts:
        return None, []
    head, rest = parts[0], parts[1:]
    return (head, rest) if NUMERIC_RE.match(head) else (None, parts)


def _reversed_digits(numeric_text: str) -> str:
    """Character-reversal for 180-degree-rotated dimension text, e.g.
    '8.5' read upside down can come back as '5.8'."""
    return numeric_text[::-1]


def _tokens_in_box(tokens: List[Token], box: Tuple[float, float, float, float]) -> List[Token]:
    x_min, x_max, y_min, y_max = box
    return [t for t in tokens if x_min <= t.xc <= x_max and y_min <= t.yc <= y_max]


def _widen(box: Tuple[float, float, float, float], margin: float) -> Tuple[float, float, float, float]:
    x_min, x_max, y_min, y_max = box
    return (x_min - margin, x_max + margin, y_min - margin, y_max + margin)


def _nearby_tokens(tokens: List[Token], anchor: Token) -> List[Token]:
    return [
        t for t in tokens
        if t is not anchor
        and abs(t.xc - anchor.xc) <= NEARBY_X_RADIUS
        and abs(t.yc - anchor.yc) <= NEARBY_Y_RADIUS
    ]


def _find_tolerance_near(tokens: List[Token], anchor: Token) -> Tuple[Optional[str], Optional[str]]:
    """Look for an explicit per-dimension tolerance or fit code printed next
    to `anchor`. Returns (explicit_tolerance, fit_code) -- at most one of
    these is set, since a dimension carries one or the other, not both.
    See the module docstring for the known stacked-limit gap."""
    nearby = sorted(_nearby_tokens(tokens, anchor), key=lambda t: (t.top, t.x0))
    joined = " ".join(t.text for t in nearby)

    m = TOLERANCE_RE.search(joined)
    if m:
        return f"±{m.group(1)}", None

    m = DUAL_LIMIT_RE.search(joined)
    if m:
        hi, lo = m.group(1).replace(" ", ""), m.group(2).replace(" ", "")
        return f"{hi}/{lo}", None

    for t in nearby:
        if FIT_CODE_RE.match(t.text):
            return None, t.text

    return None, None


def _build_matched_text(tokens: List[Token], match: Token) -> str:
    """The matched token plus one token immediately to its right on the
    same line, if any -- so a multi-word callout (e.g. "M3" + "TAPPED")
    reads back as more than just its first word."""
    same_line = [
        t for t in tokens
        if t is not match
        and abs(t.top - match.top) <= SAME_LINE_TOP_TOLERANCE
        and 0 <= t.x0 - match.x1 <= SAME_LINE_MAX_GAP
    ]
    same_line.sort(key=lambda t: t.x0)
    return " ".join([match.text] + [t.text for t in same_line[:1]])


def find_dimension(tokens: List[Token], grid: ZoneGrid, zone: str, dimension: str) -> MatchResult:
    """Find `dimension` (as printed on the checklist, e.g. '10', '8.5',
    '1.0 THRU', 'M3 THREADED HOLE3') within `zone` (e.g. 'F6') on the
    drawing, and read off whatever tolerance/fit code is printed beside it.
    """
    box = grid.cell_bounds(zone)
    if box is None:
        return MatchResult(
            zone=zone, nominal=dimension, matched_token=None, confidence="low",
            notes=[f"Zone '{zone}' doesn't match this drawing's detected grid "
                   f"(rows {grid.row_labels}, columns {grid.col_labels}) -- check the "
                   "checklist's zone code or the grid detection."],
        )

    numeric_head, extra_words = _split_nominal(dimension)

    def _search(candidates: List[Token]) -> Optional[Token]:
        if numeric_head is not None:
            reversed_head = _reversed_digits(numeric_head)
            for t in candidates:
                if t.text == numeric_head or t.text == reversed_head:
                    return t
            return None
        # Non-numeric nominal (thread/feature callout): match its first word.
        target = (extra_words[0] if extra_words else dimension.strip()).upper()
        for t in candidates:
            if t.text.upper() == target:
                return t
        return None

    confidence = "high"
    notes: List[str] = []

    match = _search(_tokens_in_box(tokens, box))
    if match is None:
        widened = _tokens_in_box(tokens, _widen(box, WIDEN_MARGIN))
        match = _search(widened)
        if match is not None:
            confidence = "medium"
            notes.append(
                f"Dimension text found just outside zone {zone}'s expected area -- "
                "verify the zone grid was read correctly for this drawing."
            )

    if match is None:
        return MatchResult(
            zone=zone, nominal=dimension, matched_token=None, confidence="low",
            notes=[f"No matching text for '{dimension}' found in or near zone {zone}."],
        )

    matched_text = _build_matched_text(tokens, match)
    explicit_tolerance, fit_code = _find_tolerance_near(tokens, match)

    return MatchResult(
        zone=zone, nominal=dimension, matched_token=match, matched_text=matched_text,
        explicit_tolerance=explicit_tolerance, fit_code=fit_code,
        confidence=confidence, notes=notes,
    )
