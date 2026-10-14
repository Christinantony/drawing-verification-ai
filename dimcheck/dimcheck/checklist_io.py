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


def ensure_docx(path: str) -> str:
    """python-docx only reads .docx. A legacy .doc checklist is converted
    first: with LibreOffice if installed, otherwise with Microsoft Word
    through COM on Windows (pip install pywin32). Returns the .docx path."""
    import os
    import shutil
    import subprocess
    import tempfile

    if not path.lower().endswith(".doc"):
        return path
    out_dir = tempfile.mkdtemp(prefix="dimcheck_")
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir", out_dir, path],
                       check=True, capture_output=True, timeout=180)
        out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".docx")
        if os.path.exists(out):
            return out
    try:
        import win32com.client  # type: ignore
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(os.path.abspath(path))
            out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".docx")
            doc.SaveAs2(out, FileFormat=16)  # wdFormatXMLDocument
            doc.Close()
        finally:
            word.Quit()
        return out
    except ImportError:
        pass
    raise RuntimeError(
        f"'{path}' is a legacy .doc file. Save it as .docx in Word, or install LibreOffice "
        "(soffice) or pywin32 (Windows + Word) so it can be converted automatically.")


def load_checklist_rows(
    docx_path: str,
    zone_col: int = 1,
    dim_col: int = 2,
    tol_col: int = 3,
) -> "tuple[docx.Document, Table, List[ChecklistRow]]":
    document = docx.Document(ensure_docx(docx_path))
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


def shade_cell(cell: _Cell, fill_hex: str) -> None:
    """Set a cell's background colour (hex like 'FFF2CC')."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    tc_pr = cell._tc.get_or_add_tcPr()
    for old in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    tc_pr.append(shd)


def write_tolerance(table: Table, row_index: int, tol_col: int, text: str,
                    shade: Optional[str] = None) -> None:
    cell = table.rows[row_index].cells[tol_col]
    set_cell_text(cell, text)
    if shade:
        shade_cell(cell, shade)


def save(document: docx.Document, out_path: str) -> None:
    document.save(out_path)
