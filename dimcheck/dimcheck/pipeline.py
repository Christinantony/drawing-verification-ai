"""
Drawing PDF + dimension checklist (.docx / .doc) -> checklist with the
Tolerance column filled, and every row reported.

Two modes per row, decided by whether the checklist cell is empty:
  * fill    - empty cell: write the tolerance read from the drawing.
  * verify  - cell already filled: compare it with the drawing and report a
              mismatch (never overwritten unless overwrite_existing=True).

This assists a QC engineer, it doesn't replace one: each row says where its
tolerance came from (explicit on the drawing / general table / assumed /
unresolved) and how confident the match was.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import checklist_io
from .dimension_matcher import MatchResult, find_dimension, printed_decimals
from .general_tolerance import GeneralToleranceTable, parse_general_tolerance_table
from .pdf_layout import flat_text, load_page_layout
from .zoning import detect_grid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sheetreader import build_cells, read_title_block  # noqa: E402


@dataclass
class RowReport:
    zone: str
    dimension: str
    existing_tolerance: str
    resolved_tolerance: Optional[str]
    source: str            # "explicit" | "general_table" | "assumed" | "unresolved"
    confidence: str
    notes: List[str]
    verdict: str = ""      # "filled" | "match" | "MISMATCH" | "kept" | "not set"


def parse_tolerance_text(text: str) -> Optional[Tuple[float, float]]:
    """'±0.1' -> (0.1, -0.1); '0/-0.2' -> (0, -0.2); '+0.02/+0.01' -> (0.02, 0.01).
    Returns (upper, lower) with upper >= lower, or None if not numeric."""
    t = (text or "").replace(" ", "").replace("−", "-").replace("+/-", "±")
    m = re.fullmatch(r"±(\d*\.?\d+)", t)
    if m:
        v = float(m.group(1))
        return v, -v
    m = re.fullmatch(r"([+\-]?\d*\.?\d+)/([+\-]?\d*\.?\d+)", t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return max(a, b), min(a, b)
    return None


def same_tolerance(a: str, b: str) -> Optional[bool]:
    pa, pb = parse_tolerance_text(a), parse_tolerance_text(b)
    if pa is None or pb is None:
        return None
    return abs(pa[0] - pb[0]) < 1e-9 and abs(pa[1] - pb[1]) < 1e-9


def general_from_sheet(layout) -> GeneralToleranceTable:
    """General tolerance block, read from the title block's own cells
    (falls back to a regex over the page text)."""
    gt = {}
    if layout.sheet is not None:
        try:
            gt = read_title_block(layout.sheet, build_cells(layout.sheet)).general_tolerance or {}
        except Exception:
            gt = {}
    if gt and any(gt.values()):
        return GeneralToleranceTable(without_decimal=gt.get("without_decimal"),
                                     one_decimal=gt.get("one_decimal"),
                                     two_decimal=gt.get("two_decimal"),
                                     angle=gt.get("angle"))
    return parse_general_tolerance_table(flat_text(layout))


def resolve_tolerance(match: MatchResult, general: GeneralToleranceTable) -> Tuple[Optional[str], str, List[str]]:
    notes = list(match.notes)
    if match.tolerance:
        return match.tolerance.format(), "explicit", notes
    if match.fit_code:
        notes.append(f"Fit code '{match.fit_code}' printed next to the dimension; look up its "
                     "limits for this size (ISO 286) - not converted automatically.")
        return None, "unresolved", notes
    if match.matched_token is None:
        return None, "unresolved", notes
    text = match.matched_text
    if text.rstrip().endswith("°"):
        if general.angle:
            return general.angle, "general_table", notes
        return None, "unresolved", notes + ["Angle dimension and no ANGLE general tolerance read."]
    decimals = printed_decimals(text)
    if decimals is None:
        if general.one_decimal:
            notes.append(f"'{text}' isn't a plain number (thread/feature callout) and has no tolerance "
                         f"beside it; ONE DECIMAL general tolerance ({general.one_decimal}) assumed - "
                         "confirm for this feature type.")
            return general.one_decimal, "assumed", notes
        return None, "unresolved", notes
    value = general.lookup_by_decimals(decimals)
    if value is None:
        notes.append(f"No general tolerance for {decimals}-decimal dimensions ('{text}') was read "
                     "from the title block.")
        return None, "unresolved", notes
    return value, "general_table", notes


FILL_CONFIRMED = "FFF2CC"   # light yellow
FILL_TO_CONFIRM = "F8CBAD"  # light orange


def process(pdf_path: str, checklist_path: str, output_path: Optional[str], page_number: int = 0,
            zone_col: int = 1, dim_col: int = 2, tol_col: int = 3, overwrite_existing: bool = False,
            force_ocr: bool = False, ocr_dpi: int = 300,
            mark_filled: bool = False) -> List[RowReport]:
    """mark_filled: shade every inserted cell in the saved copy - yellow when
    read from the drawing (explicit or general table) at high confidence,
    orange when assumed or found only near its zone (an engineer must confirm)."""
    layout = load_page_layout(pdf_path, page_number=page_number, force_ocr=force_ocr, ocr_dpi=ocr_dpi)
    grid = detect_grid(layout.tokens, layout.page_width, layout.page_height)
    general = general_from_sheet(layout)

    document, table, rows = checklist_io.load_checklist_rows(
        checklist_path, zone_col=zone_col, dim_col=dim_col, tol_col=tol_col)

    reports: List[RowReport] = []
    for i, row in enumerate(rows):
        match = find_dimension(layout.tokens, grid, row.zone, row.dimension)
        tol, source, notes = resolve_tolerance(match, general)
        if i == 0:
            notes = list(grid.warnings) + notes

        existing = row.existing_tolerance
        if existing:
            same = same_tolerance(existing, tol) if tol else None
            if tol is None:
                verdict = "kept"
                notes.append("Checklist already has a value; the drawing value could not be read to verify it.")
            elif same:
                verdict = "match"
            elif same is None:
                verdict = "kept"
                notes.append(f"Checklist value '{existing}' isn't in a form that can be compared.")
            else:
                verdict = "MISMATCH"
                notes.insert(0, f"Checklist says {existing}, drawing says {tol} ({source}).")
            if overwrite_existing and tol and verdict == "MISMATCH":
                checklist_io.write_tolerance(table, row.row_index, tol_col, tol)
                verdict = "MISMATCH (overwritten)"
        else:
            if tol:
                shade = None
                if mark_filled:
                    shade = FILL_CONFIRMED if (source in ("explicit", "general_table")
                                               and match.confidence == "high") else FILL_TO_CONFIRM
                checklist_io.write_tolerance(table, row.row_index, tol_col, tol, shade=shade)
                verdict = "filled"
            else:
                verdict = "not set"

        reports.append(RowReport(zone=row.zone, dimension=row.dimension, existing_tolerance=existing,
                                 resolved_tolerance=tol, source=source, confidence=match.confidence,
                                 notes=notes, verdict=verdict))

    if output_path:
        checklist_io.save(document, output_path)
    return reports


def print_report(reports: List[RowReport]) -> None:
    print(f"{'ZONE':<6}{'DIMENSION':<16}{'CHECKLIST':<11}{'DRAWING':<11}{'VERDICT':<10}{'SOURCE':<14}{'CONF':<8}NOTES")
    for r in reports:
        print(f"{r.zone:<6}{r.dimension:<16}{(r.existing_tolerance or '-'):<11}"
              f"{(r.resolved_tolerance or '-- NOT SET --'):<11}{r.verdict:<10}{r.source:<14}"
              f"{r.confidence:<8}{' | '.join(r.notes)}")
