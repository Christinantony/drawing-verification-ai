"""
structuring.py
Step 2 of the pipeline: turn the raw text/tables extraction.py produced into
a clean, canonical shape - a title_block dict with known fields, and a
normalized list of BOM rows - and render that as Markdown for the model.

This is where Gemma 4 E4B's weak spatial reasoning gets bypassed: by the
time anything reaches the model, the drawing is already plain structured
text, not an image it has to visually parse.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import config
from extraction import DocumentData, TitleBlockCell, crop_region, ocr_image


def _find_field_value(text: str, label_patterns: List[str]) -> Optional[str]:
    for pattern in label_patterns:
        regex = rf"{pattern}\s*[:\-]?\s*(.+)"
        m = re.search(regex, text, re.IGNORECASE)
        if m:
            value = m.group(1).strip(" \t:-|")
            # cut the match short if another ALLCAPS label starts nearby
            value = re.split(r"\s{2,}[A-Z][A-Z .]{2,}:?", value)[0].strip()
            if value and not re.fullmatch(r"[\W_]+", value):
                return value[:120]
    return None


def parse_title_block(text: str) -> Dict[str, Optional[str]]:
    return {name: _find_field_value(text, patterns)
            for name, patterns in config.TITLE_BLOCK_FIELDS.items()}


def _label_match(text: str) -> Optional[Tuple[str, str]]:
    """Return (field_name, remainder) if `text` starts with a recognized
    title-block label, else None. Anchored at the start of the text (not
    just searched anywhere) - in a cell-per-field layout, the label is
    expected to be the leading content of its own box."""
    for field_name, patterns in config.TITLE_BLOCK_FIELDS.items():
        for pattern in patterns:
            m = re.match(rf"\s*{pattern}\s*[:\-]?\s*(.*)", text, re.IGNORECASE | re.DOTALL)
            if m:
                return field_name, m.group(1).strip(" \t:-|\n")
    return None


def _band_overlap(a: Tuple[float, float, float, float],
                   b: Tuple[float, float, float, float], axis: str) -> float:
    """axis='y' -> vertical overlap (for left/right neighbors);
    axis='x' -> horizontal overlap (for above/below neighbors)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    if axis == "y":
        return min(ay1, by1) - max(ay0, by0)
    return min(ax1, bx1) - max(ax0, bx0)


def _find_adjacent_value_cell(label_bbox, label_id: int, cells: List[TitleBlockCell],
                               consumed_ids: set) -> Optional[TitleBlockCell]:
    """Find the nearest neighboring cell to a label-only box: to the right
    first (label-left/value-right layouts), then below (label-top/
    value-bottom layouts) - whichever is geometrically closest. A neighbor
    that itself looks like another field's label is skipped, so we don't
    grab the next label instead of an actual value."""
    lx0, ly0, lx1, ly1 = label_bbox
    lw, lh = lx1 - lx0, ly1 - ly0
    best, best_dist = None, None

    for cell in cells:
        if id(cell) == label_id or id(cell) in consumed_ids:
            continue
        cx0, cy0, cx1, cy1 = cell.bbox
        if cx0 >= lx1 - 1 and _band_overlap(label_bbox, cell.bbox, "y") > 0.3 * max(lh, 1):
            dist = cx0 - lx1
            if dist < lw * 2 and (best_dist is None or dist < best_dist):
                best, best_dist = cell, dist
        if cy0 >= ly1 - 1 and _band_overlap(label_bbox, cell.bbox, "x") > 0.3 * max(lw, 1):
            dist = cy0 - ly1
            if dist < lh * 2 and (best_dist is None or dist < best_dist):
                best, best_dist = cell, dist
    return best


def classify_title_block_cells(cells: List[TitleBlockCell]) -> Dict[str, Optional[str]]:
    """Given the individually-boxed cells of a title block, identify which
    field each one is. Handles two common layouts:
      - label and value printed in the SAME box, e.g. "DWG NO: AM-99"
      - a label-only box (just "SCALE", nothing else) paired with an
        adjoining value-only box - whichever neighbor (right, then below)
        is closest and doesn't itself look like another label
    First match wins per field; a field found from a same-box label+value
    match is never overwritten by a weaker adjacent-cell guess.
    """
    result: Dict[str, Optional[str]] = {name: None for name in config.TITLE_BLOCK_FIELDS}
    label_only: List[Tuple[str, TitleBlockCell]] = []

    for cell in cells:
        text = cell.text.strip()
        if not text:
            continue
        match = _label_match(text)
        if not match:
            continue
        field_name, remainder = match
        if result[field_name]:
            continue
        if remainder:
            result[field_name] = remainder[:120]
        else:
            label_only.append((field_name, cell))

    consumed_ids = {id(c) for _, c in label_only}
    for field_name, label_cell in label_only:
        if result[field_name]:
            continue
        neighbor = _find_adjacent_value_cell(label_cell.bbox, id(label_cell), cells, consumed_ids)
        if neighbor and not _label_match(neighbor.text.strip()):
            result[field_name] = neighbor.text.strip()[:120]

    return result


def _match_header(cell: str) -> Optional[str]:
    cell_norm = (cell or "").strip().lower()
    for canonical, aliases in config.BOM_HEADER_ALIASES.items():
        if any(cell_norm == a or cell_norm.startswith(a) for a in aliases):
            return canonical
    return None


def _table_looks_like_bom(table: List[List[str]]) -> bool:
    if not table or not table[0]:
        return False
    return sum(1 for c in table[0] if _match_header(c or "")) >= 2


def parse_bom_from_tables(tables: List[List[List[str]]]) -> List[Dict[str, str]]:
    """Prefer real ruled tables (pdfplumber) - most reliable when the PDF
    has an actual table, not just visually-aligned text."""
    for table in tables:
        if not _table_looks_like_bom(table):
            continue
        header = table[0]
        col_map = {i: _match_header(c or "") for i, c in enumerate(header)}
        rows = []
        for raw_row in table[1:]:
            row = {}
            for i, cell in enumerate(raw_row):
                canonical = col_map.get(i)
                if canonical:
                    row[canonical] = (cell or "").strip()
            if any(row.values()):
                rows.append(row)
        if rows:
            return rows
    return []


_BOM_HEADER_LINE = re.compile("|".join(re.escape(k) for k in config.BOM_TABLE_KEYWORDS), re.IGNORECASE)
_SPLIT_COLS = re.compile(r"\s{2,}|\t")


def parse_bom_from_text(text: str) -> List[Dict[str, str]]:
    """Fallback for scanned drawings with no ruled table pdfplumber can see:
    find the 'BILL OF MATERIALS' heading, locate a plausible header row
    within the next few lines, then split subsequent lines on wide
    whitespace/tabs the way OCR usually preserves column gaps."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if _BOM_HEADER_LINE.search(line)), None)
    if start is None:
        return []

    header_cols, header_idx = None, None
    for j in range(start, min(start + 5, len(lines))):
        cells = [c for c in _SPLIT_COLS.split(lines[j].strip()) if c]
        hits = [_match_header(c) for c in cells]
        if sum(1 for h in hits if h) >= 2:
            header_cols = [h or f"col_{k}" for k, h in enumerate(hits)]
            header_idx = j
            break
    if header_cols is None:
        return []

    rows = []
    for line in lines[header_idx + 1:]:
        if not line.strip():
            if rows:
                break
            continue
        cells = [c for c in _SPLIT_COLS.split(line.strip()) if c]
        if not cells:
            continue
        rows.append({(header_cols[k] if k < len(header_cols) else f"col_{k}"): c
                      for k, c in enumerate(cells)})
    return rows


def extract_notes(text: str) -> List[str]:
    m = re.search(r"NOTES?\s*:?\s*\n(.+)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    block = re.split(r"\n[A-Z][A-Z ]{4,}\n", m.group(1))[0]
    items = re.split(r"\n\s*(?:\d+[.)]|-)\s*", block)
    return [i.strip() for i in items if i.strip()][:30]


def build_structured_document(doc: DocumentData) -> dict:
    full_text = doc.full_text
    first_page = doc.pages[0] if doc.pages else None

    # Fallback blob text for regex extraction (fills gaps left by cell
    # classification, or is used entirely when no cells were found).
    title_block_text = full_text
    if first_page is not None:
        if first_page.source == "embedded" and first_page.region_text.strip():
            title_block_text = first_page.region_text + "\n" + full_text
        elif first_page.image is not None:
            try:
                crop = crop_region(first_page.image, corner="bottom-right")
                title_block_text = ocr_image(crop) + "\n" + full_text
            except Exception:
                pass

    # Prefer per-cell classification (isolates each bordered field box and
    # reads only what's inside it - see classify_title_block_cells) over
    # regex on the raw text blob, since the blob approach can't tell where
    # one field's box ends and the next one's starts. Any field a cell
    # pass didn't capture is backfilled from the blob regex pass.
    title_block = {name: None for name in config.TITLE_BLOCK_FIELDS}
    used_cells = bool(first_page and first_page.title_block_cells)
    if used_cells:
        cell_result = classify_title_block_cells(first_page.title_block_cells)
        title_block.update({k: v for k, v in cell_result.items() if v})

    blob_result = parse_title_block(title_block_text)
    for k, v in blob_result.items():
        if not title_block.get(k):
            title_block[k] = v

    all_tables = [t for p in doc.pages for t in p.tables]
    bom = parse_bom_from_tables(all_tables) or parse_bom_from_text(full_text)

    return {
        "source_file": str(doc.file_path),
        "page_count": len(doc.pages),
        "extraction_sources": sorted({p.source for p in doc.pages}),
        "page_orientations": [p.orientation for p in doc.pages],
        "rotation_corrections_deg": [p.rotation_applied_deg for p in doc.pages],
        "title_block_method": "cells+regex" if used_cells else "regex_only",
        "title_block_cell_count": len(first_page.title_block_cells) if first_page else 0,
        "title_block": title_block,
        "bom": bom,
        "notes": extract_notes(full_text),
        "raw_text": full_text,
    }


def to_markdown(structured: dict) -> str:
    lines = []
    orientations = structured.get("page_orientations", [])
    rotations = structured.get("rotation_corrections_deg", [])
    if orientations:
        page_notes = []
        for i, orient in enumerate(orientations):
            rot = rotations[i] if i < len(rotations) else 0
            note = f"p{i + 1}: {orient}"
            if rot:
                note += f" (rotated {rot} deg to correct scan orientation)"
            page_notes.append(note)
        lines.append(f"### Page orientation: {', '.join(page_notes)}")
        lines.append("")
    method = structured.get("title_block_method", "regex_only")
    cell_count = structured.get("title_block_cell_count", 0)
    method_label = (f"{cell_count} bordered field boxes detected, cell-classified + regex backfill"
                     if method == "cells+regex" else "regex over title-block text (no bordered field boxes detected)")
    lines.append(f"### Title block extraction: {method_label}")
    lines.append("")
    lines += ["### Title Block", ""]
    for k, v in structured["title_block"].items():
        lines.append(f"- **{k}**: {v if v else '(not found)'}")
    lines.append("")

    lines.append("### Bill of Materials")
    bom = structured["bom"]
    if bom:
        headers = sorted({k for row in bom for k in row.keys()})
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "---|" * len(headers))
        for row in bom:
            lines.append("| " + " | ".join(row.get(h, "") for h in headers) + " |")
    else:
        lines.append("(no BOM rows detected)")
    lines.append("")

    if structured["notes"]:
        lines.append("### Notes")
        for n in structured["notes"]:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("### Full extracted text (for spelling/grammar context)")
    lines.append("```")
    lines.append(structured["raw_text"][:6000])
    lines.append("```")
    return "\n".join(lines)
