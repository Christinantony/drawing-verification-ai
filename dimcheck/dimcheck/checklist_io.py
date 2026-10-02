"""
Read a dimension checklist (.docx) table and write resolved tolerances back
into it.

The checklist's own header row is often not part of the table at all -- in
the sample template it lives in the page header/footer (repeated on every
printed page) instead of the table body, so we don't rely on header text to
find the columns. Column positions are configurable and default to the
layout seen in the sample: [Sl No, ZONE, DIMENSIONS, TOLERANCE, ...measured
values...].
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

import docx
from docx.table import Table, _Cell

FOOTER_ROW_KEYWORDS = ("weight", "verified by", "qc clearance", "key characteristic")


@dataclass
class ChecklistRow:
    row_index: int
    zone: str
    dimension: str
    existing_tolerance: str


def _find_data_table(document: docx.Document, zone_col: int, dim_col: int) -> Optional[Table]:
    """Pick the table that actually holds zone/dimension rows: the one with
    the most rows where zone_col looks like a zone code (e.g. 'F6') and
    dim_col is non-empty."""
    zone_re = re.compile(r"^[A-Za-z]{1,2}\d{1,2}$")
    best, best_score = None, 0
    for table in document.tables:
        score = 0
        for row in table.rows:
            cells = row.cells
            if len(cells) <= max(zone_col, dim_col):
                continue
            if zone_re.match(cells[zone_col].text.strip()) and cells[dim_col].text.strip():
                score += 1
        if score > best_score:
            best, best_score = table, score
    return best


def load_checklist_rows(
    docx_path: str,
    zone_col: int = 1,
    dim_col: int = 2,
    tol_col: int = 3,
) -> "tuple[docx.Document, Table, List[ChecklistRow]]":
    document = docx.Document(docx_path)
    table = _find_data_table(document, zone_col, dim_col)
    if table is None:
        raise ValueError(
            "Could not find a table with zone-code-looking cells "
            f"(column {zone_col}) and dimension values (column {dim_col}). "
            "Pass --zone-col/--dim-col to match this checklist's layout."
        )

    zone_re = re.compile(r"^[A-Za-z]{1,2}\d{1,2}$")
    rows: List[ChecklistRow] = []
    for i, row in enumerate(table.rows):
        cells = row.cells
        if len(cells) <= max(zone_col, dim_col, tol_col):
            continue
        zone_text = cells[zone_col].text.strip()
        dim_text = cells[dim_col].text.strip()
        if not zone_re.match(zone_text) or not dim_text:
            continue  # skip footer rows (Weight, Verified by, QC Clearance, ...)
        rows.append(ChecklistRow(
            row_index=i,
            zone=zone_text,
            dimension=dim_text,
            existing_tolerance=cells[tol_col].text.strip(),
        ))
    return document, table, rows


def set_cell_text(cell: _Cell, text: str) -> None:
    """Replace a cell's text while keeping the first run's formatting
    (font, size, bold, etc.) instead of appending a differently-styled run."""
    paragraph = cell.paragraphs[0]
    if paragraph.runs:
        paragraph.runs[0].text = text
        for extra in paragraph.runs[1:]:
            extra.text = ""
    else:
        paragraph.add_run(text)
    # Clear any additional paragraphs the cell might have
    for p in cell.paragraphs[1:]:
        for r in p.runs:
            r.text = ""


def write_tolerance(table: Table, row_index: int, tol_col: int, text: str) -> None:
    cell = table.rows[row_index].cells[tol_col]
    set_cell_text(cell, text)


def save(document: docx.Document, out_path: str) -> None:
    document.save(out_path)
