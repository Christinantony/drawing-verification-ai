"""
Detect the ISO-7200-style zone grid printed on a drawing's border (numbers
1..N along the top/bottom edges, letters A..Z along the left/right edges) and
map an (x, y) position on the sheet to a zone code such as "F6".

This assumes the common convention also used in the sample drawing:
  - column numbers run along the top and bottom margins
  - row letters run along the left and right margins
  - a zone is written as <letter><number>, e.g. "C5"

If a drawing uses the opposite order (number then letter), pass
zone_format="NL" to ZoneGrid.locate()/code_for().
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .pdf_layout import Token

MARGIN_FRACTION = 0.07  # how close to an edge a label must be to count as a grid label


@dataclass
class ZoneGrid:
    col_labels: List[str]       # e.g. ["8","7","6","5","4","3","2","1"], left-to-right
    col_edges: List[float]      # len(col_labels)+1 boundaries in x, left-to-right
    row_labels: List[str]       # e.g. ["G","F","E","D","C","B","A"], top-to-bottom
    row_edges: List[float]      # len(row_labels)+1 boundaries in y (top-down "top"), top-to-bottom
    warnings: List[str] = field(default_factory=list)

    def cell_bounds(self, zone_code: str) -> Optional[Tuple[float, float, float, float]]:
        """Returns (x_min, x_max, y_min, y_max) for a zone like 'F6', or None
        if the zone code doesn't match any detected row/column."""
        m = re.match(r"^\s*([A-Za-z]+)\s*(\d+)\s*$", zone_code)
        if not m:
            return None
        letter, number = m.group(1).upper(), m.group(2)
        if letter not in self.row_labels or number not in self.col_labels:
            return None
        ri = self.row_labels.index(letter)
        ci = self.col_labels.index(number)
        return (self.col_edges[ci], self.col_edges[ci + 1], self.row_edges[ri], self.row_edges[ri + 1])

    def locate(self, x: float, y: float) -> Optional[str]:
        """Reverse lookup: which zone code contains point (x, y)."""
        ci = _bin_index(x, self.col_edges)
        ri = _bin_index(y, self.row_edges)
        if ci is None or ri is None:
            return None
        return f"{self.row_labels[ri]}{self.col_labels[ci]}"


def _bin_index(v: float, edges: List[float]) -> Optional[int]:
    for i in range(len(edges) - 1):
        if edges[i] <= v < edges[i + 1]:
            return i
    return None


def _cluster_by_text(candidates: List[Token], axis: str) -> dict:
    """Group tokens with identical text (e.g. both copies of 'C' on the left
    and right margins) and average their position along `axis` ('x' or 'y')."""
    groups: dict = {}
    for t in candidates:
        groups.setdefault(t.text, []).append(t)
    centers = {}
    for text, toks in groups.items():
        if axis == "x":
            centers[text] = sum(t.xc for t in toks) / len(toks)
        else:
            centers[text] = sum(t.yc for t in toks) / len(toks)
    return centers


def detect_grid(tokens: List[Token], page_width: float, page_height: float) -> ZoneGrid:
    warnings: List[str] = []

    top_margin = page_height * MARGIN_FRACTION
    bottom_margin = page_height * (1 - MARGIN_FRACTION)
    left_margin = page_width * MARGIN_FRACTION
    right_margin = page_width * (1 - MARGIN_FRACTION)

    number_re = re.compile(r"^[1-9][0-9]?$")
    letter_re = re.compile(r"^[A-Za-z]$")

    # Zone labels are always upright; vertical words in the margin (the
    # confidentiality text running up the border contains "A", "T", ...)
    # must not be mistaken for row letters.
    upright = [t for t in tokens if getattr(t, "horizontal", True)]
    number_candidates = [
        t for t in upright
        if number_re.match(t.text) and (t.bottom <= top_margin or t.top >= bottom_margin)
    ]
    letter_candidates = [
        t for t in upright
        if letter_re.match(t.text) and (t.x1 <= left_margin or t.x0 >= right_margin)
    ]

    col_centers = _cluster_by_text(number_candidates, axis="x")
    row_centers = _cluster_by_text(letter_candidates, axis="y")

    if len(col_centers) < 2:
        warnings.append(
            f"Only found {len(col_centers)} column-number label(s) near the page margins; "
            "zone detection may be unreliable. Check that this drawing uses a numbered/lettered "
            "border grid."
        )
    if len(row_centers) < 2:
        warnings.append(
            f"Only found {len(row_centers)} row-letter label(s) near the page margins; "
            "zone detection may be unreliable."
        )

    # Columns: order left-to-right by x center (label text itself may count down,
    # e.g. "8 7 6 5 4 3 2 1" left-to-right -- we don't assume a numbering direction).
    col_items = sorted(col_centers.items(), key=lambda kv: kv[1])
    col_labels = [k for k, _ in col_items]
    col_positions = [v for _, v in col_items]

    # Rows: order top-to-bottom by y center ("top" grows downward).
    row_items = sorted(row_centers.items(), key=lambda kv: kv[1])
    row_labels = [k for k, _ in row_items]
    row_positions = [v for _, v in row_items]

    col_edges = _edges_from_centers(col_positions, 0.0, page_width)
    row_edges = _edges_from_centers(row_positions, 0.0, page_height)

    return ZoneGrid(
        col_labels=col_labels, col_edges=col_edges,
        row_labels=row_labels, row_edges=row_edges,
        warnings=warnings,
    )


def _edges_from_centers(centers: List[float], lo: float, hi: float) -> List[float]:
    if not centers:
        return [lo, hi]
    edges = [lo]
    for a, b in zip(centers, centers[1:]):
        edges.append((a + b) / 2)
    edges.append(hi)
    return edges
