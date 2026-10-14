"""
One structured, JSON-friendly record per drawing sheet: everything the
checks need, read once.

    from sheetreader.record import read_drawing
    records = read_drawing("AT-RL_05_B2-E-03_REV._00.pdf")
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .cells import build_cells, reading_order
from .loader import load_sheets
from .model import Sheet
from .normalize import QTY_ASREQ, ids_from_filename
from .tables import find_tables
from .titleblock import read_title_block

# Drawing-template form number printed in the sheet margin, e.g.
# "AT-F-DRG-01 REV.00". Override with read_drawing(form_id_re=...).
DEFAULT_FORM_ID_RE = r"\b[A-Z]{1,4}-F-[A-Z]{2,5}-\d{2}\b(?:\s*REV\.?\s*\d{1,2})?"

# Words that may stand right before a dimension value.
DIM_PREFIXES = {"R", "SR", "M", "DIA", "PCD", "PCD.", "TYP", "THK", "MAX", "MIN", "REF", "X", "CSK", "C/B", "CBORE", "DEPTH", "DP"}
DIM_RE = re.compile(r"^[*ØR]?(\d{1,5}(?:\.\d{1,3})?)(?:°)?$")


@dataclass
class DrawingRecord:
    file: str
    page: int
    page_count: int
    text_source: str              # "embedded" | "ocr"
    producer: str
    rotation_deg: int
    paper_size: Optional[str]
    orientation: str
    filename_drawing_number: Optional[str]
    filename_revision: Optional[str]
    title_block: Dict[str, Optional[str]]
    general_tolerance: Dict[str, Optional[str]]
    bom: List[Dict[str, str]]
    revision_table: List[Dict[str, str]]
    notes: List[str]
    form_id: Optional[str]
    dimension_values: List[float]
    low_confidence_words: List[str]
    text_lines: List[str]         # all readable text, line by line
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _notes(sheet: Sheet, exclude, tb_region=None) -> List[str]:
    """Lines under a 'NOTE' / 'NOTES' heading, until a gap in the text."""
    heads = [w for w in sheet.words if w.horizontal and re.fullmatch(r"NOTES?\s*:?", w.text, re.I)]
    out: List[str] = []
    for h in heads:
        right_limit = h.x0 + 600
        if tb_region and h.x1 < tb_region[0]:
            right_limit = min(right_limit, tb_region[0] - 2)
        bottom_limit = sheet.height * 0.965  # zone labels in the border
        same_line = [w for w in sheet.words if w.horizontal and abs(w.yc - h.yc) < 3 and w.x0 > h.x1
                     and w.x0 - h.x1 < 60 and w.x1 <= right_limit and not exclude(w)]
        below = [w for w in sheet.words if w.horizontal and h.y1 - 1 <= w.y0 <= h.y1 + 150
                 and w.y1 < bottom_limit and w.x0 >= h.x0 - 5 and w.x1 <= right_limit and not exclude(w)]
        lines = reading_order(below)
        lh = max(4.0, (h.y1 - h.y0))
        last_y = h.y1
        kept = []
        for ln in lines:
            y = min(w.y0 for w in ln)
            if y - last_y > 3.0 * lh:
                break
            kept.append(" ".join(w.text for w in ln).strip("•· "))
            last_y = max(w.y1 for w in ln)
        if same_line:
            first = " ".join(w.text for w in sorted(same_line, key=lambda w: w.x0))
            kept.insert(0, first)
        out.extend(k for k in kept if len(re.sub(r"[^A-Za-z0-9]", "", k)) >= 3)
    return out


def read_drawing(path, force_ocr: bool = False, vertical_text: bool = True,
                 form_id_re: str = DEFAULT_FORM_ID_RE,
                 labels: Optional[dict] = None) -> List[DrawingRecord]:
    path = str(path)
    sheets = load_sheets(path, force_ocr=force_ocr, vertical_text=vertical_text)
    return [read_sheet(sh, path, len(sheets), form_id_re=form_id_re, labels=labels)[0] for sh in sheets]


def read_sheet(sh: Sheet, path, page_count: int = 1, form_id_re: str = DEFAULT_FORM_ID_RE,
               labels: Optional[dict] = None):
    """One loaded sheet -> (DrawingRecord, parts). `parts` keeps what the
    record flattens away (cells, title block, table objects) for callers that
    need positions, such as the QC agent."""
    path = str(path)
    fn_dn, fn_rev = ids_from_filename(path)
    if True:
        cells = build_cells(sh)
        tb = read_title_block(sh, cells, labels=labels)
        boms = find_tables(cells, "bom", sh)
        revs = find_tables(cells, "revision", sh)
        bom = max(boms, key=lambda t: len(t.rows)).rows if boms else []
        if sh.text_source == "ocr":
            # 'A/R' (as required) is the one quantity OCR reliably mangles
            # ('AJR', 'AMR', 'A1R'); nothing else in a qty column looks like it.
            for r in bom:
                if r.get("qty") and QTY_ASREQ.match(r["qty"].strip()):
                    r["qty"] = "A/R"
        rev_rows = max(revs, key=lambda t: len(t.rows)).rows if revs else []

        boxes = [b for b in ([tb.region] if tb.region else [])]
        for t in boms + revs:
            ys = [c.y0 for c in t.header_cells.values()] + [c.y1 for c in t.header_cells.values()]
            span = 30 * max(1, len(t.rows) + 1)
            boxes.append((t.bbox[0], min(ys) - span, t.bbox[2], max(ys) + span))

        def excluded(w) -> bool:
            return any(b[0] <= w.xc <= b[2] and b[1] <= w.yc <= b[3] for b in boxes)

        mx, my = sh.width * 0.03, sh.height * 0.03
        # A number right after a word ("ARALDITE AW 4510", "ANABOND 666",
        # "LOCTITE 263", "IS 319") is a product or standard code in a note, not
        # a dimension. Dimension prefixes (Ø, R, M, PCD...) still count.
        coded = set()
        lines_all = reading_order(sh.words)
        for ln in lines_all:
            for prev, w in zip(ln, ln[1:]):
                p = prev.text.strip(":,")
                if (re.fullmatch(r"[A-Za-z][A-Za-z&/.\-]+", p) and p.upper() not in DIM_PREFIXES
                        and w.x0 - prev.x1 < 3.0 * max(w.height, prev.height)):
                    coded.add(id(w))
        for ln in lines_all:
            for w, nxt in zip(ln, ln[1:]):
                # '86±4 g' read as '8644 g': a value followed by a mass unit
                # is a weight, never a dimension.
                if re.fullmatch(r"(g|gm|grams?|kg)\.?", nxt.text, re.I) and nxt.x0 - w.x1 < 1.5 * w.height:
                    coded.add(id(w))
        dims = []
        for w in sh.words:
            if excluded(w) or w.conf < 70 or not (mx < w.xc < sh.width - mx and my < w.yc < sh.height - my):
                continue
            if id(w) in coded:
                continue
            m = DIM_RE.match(w.text)
            if m and "." in m.group(1) or (m and len(m.group(1)) >= 2):
                try:
                    dims.append(float(m.group(1)))
                except ValueError:
                    pass

        full = "\n".join(" ".join(w.text for w in ln) for ln in lines_all)
        fm = re.search(form_id_re, full) if form_id_re else None
        low = sorted({w.text for w in sh.words if w.conf < 60 and len(w.text) >= 3}) if sh.text_source == "ocr" else []

        record = DrawingRecord(
            file=Path(path).name, page=sh.page_number, page_count=page_count,
            text_source=sh.text_source, producer=sh.producer, rotation_deg=sh.rotation_deg,
            paper_size=sh.paper_size, orientation=sh.orientation,
            filename_drawing_number=fn_dn, filename_revision=fn_rev,
            title_block=tb.fields, general_tolerance=tb.general_tolerance,
            bom=bom, revision_table=rev_rows, notes=_notes(sh, excluded, tb.region),
            form_id=fm.group(0) if fm else None, dimension_values=sorted(set(dims)),
            low_confidence_words=low, text_lines=full.splitlines(),
            warnings=sh.warnings + tb.warnings,
        )
    parts = {"cells": cells, "title_block": tb, "boms": boms, "revisions": revs, "excluded_boxes": boxes}
    return record, parts
