"""
End-to-end pipeline: drawing PDF + checklist DOCX -> checklist DOCX with the
Tolerance column filled in, plus a per-row report for human review.

This tool assists a QC engineer -- it does not replace one. Every row it
touches is reported with where the tolerance came from (explicit callout on
the drawing vs. the general tolerance table vs. an assumption) and a
confidence level, so a reviewer can spot-check anything uncertain before the
checklist is used.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .pdf_layout import load_page_layout, flat_text
from .zoning import detect_grid, ZoneGrid
from .general_tolerance import parse_general_tolerance_table, count_decimals, GeneralToleranceTable
from .dimension_matcher import find_dimension, MatchResult
from . import checklist_io


@dataclass
class RowReport:
    zone: str
    dimension: str
    existing_tolerance: str
    resolved_tolerance: Optional[str]
    source: str          # "explicit" | "general_table" | "assumed" | "unresolved" | "kept_existing"
    confidence: str
    notes: List[str]


def resolve_tolerance(match: MatchResult, general: GeneralToleranceTable) -> "tuple[Optional[str], str, List[str]]":
    """Decide the tolerance text + where it came from, for one matched dimension."""
    notes = list(match.notes)

    if match.explicit_tolerance:
        return match.explicit_tolerance, "explicit", notes

    if match.fit_code:
        # We deliberately don't guess a numeric value for a fit/limit code.
        return None, "unresolved", notes

    if match.matched_token is None:
        return None, "unresolved", notes

    numeric_text = match.matched_text.split()[0] if match.matched_text else match.nominal
    decimals = count_decimals(numeric_text)
    if decimals is None:
        # Non-numeric (thread/feature) callout with no explicit tolerance nearby.
        # Fall back to the ONE DECIMAL tier as a labeled assumption, matching
        # common shop-floor convention -- but flag it clearly rather than
        # presenting it as read-off-the-drawing fact.
        if general.one_decimal:
            notes.append(
                f"'{match.matched_text}' isn't a plain numeric dimension; no explicit tolerance "
                f"was found next to it, so the ONE DECIMAL general tolerance ({general.one_decimal}) "
                "was assumed. Confirm this is correct for this feature type (e.g. thread class)."
            )
            return general.one_decimal, "assumed", notes
        return None, "unresolved", notes

    value = general.lookup_by_decimals(decimals)
    if value is None:
        notes.append(
            f"No general-tolerance entry found for {decimals}-decimal dimensions "
            f"('{numeric_text}') in this drawing's title block."
        )
        return None, "unresolved", notes
    return value, "general_table", notes


def process(
    pdf_path: str,
    checklist_path: str,
    output_path: str,
    page_number: int = 0,
    zone_col: int = 1,
    dim_col: int = 2,
    tol_col: int = 3,
    overwrite_existing: bool = False,
    force_ocr: bool = False,
    ocr_dpi: int = 300,
) -> List[RowReport]:
    layout = load_page_layout(pdf_path, page_number=page_number, force_ocr=force_ocr, ocr_dpi=ocr_dpi)
    grid = detect_grid(layout.tokens, layout.page_width, layout.page_height)
    general = parse_general_tolerance_table(flat_text(layout))

    document, table, rows = checklist_io.load_checklist_rows(
        checklist_path, zone_col=zone_col, dim_col=dim_col, tol_col=tol_col
    )

    reports: List[RowReport] = []
    for row in rows:
        if row.existing_tolerance and not overwrite_existing:
            reports.append(RowReport(
                zone=row.zone, dimension=row.dimension, existing_tolerance=row.existing_tolerance,
                resolved_tolerance=row.existing_tolerance, source="kept_existing", confidence="n/a",
                notes=["Tolerance cell already had a value; left unchanged (use --overwrite to replace)."],
            ))
            continue

        match = find_dimension(layout.tokens, grid, row.zone, row.dimension)
        tol_value, source, notes = resolve_tolerance(match, general)
        notes = list(grid.warnings) + notes if not reports else notes  # surface grid warnings once

        if tol_value:
            checklist_io.write_tolerance(table, row.row_index, tol_col, tol_value)

        reports.append(RowReport(
            zone=row.zone, dimension=row.dimension, existing_tolerance=row.existing_tolerance,
            resolved_tolerance=tol_value, source=source,
            confidence=match.confidence, notes=notes,
        ))

    checklist_io.save(document, output_path)
    return reports


def print_report(reports: List[RowReport]) -> None:
    print(f"{'ZONE':<6}{'DIMENSION':<22}{'TOLERANCE':<12}{'SOURCE':<15}{'CONF':<8}NOTES")
    for r in reports:
        tol = r.resolved_tolerance or "-- NOT SET --"
        print(f"{r.zone:<6}{r.dimension:<22}{tol:<12}{r.source:<15}{r.confidence:<8}{' | '.join(r.notes)}")
