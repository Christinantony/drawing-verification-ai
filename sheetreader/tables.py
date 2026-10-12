"""
Ruled tables (BOM, revision table, any parts list) read from cells.

A table is found by its header row: cells whose text matches known column
names, lined up horizontally. Rows are then collected outward from the
header in whichever direction the data is - SolidWorks and AutoCAD BOMs
anchored to the title block usually have the header at the *bottom* with
item 1 just above it, revision tables have it at the top. Each row is
anchored on its key column (item / rev number) and must touch the previous
row, so a different table drawn nearby isn't swallowed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .cells import normalize_ocr, ocr_cell
from .model import BBox, Cell, Sheet


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper().replace("0", "O"))


# kind -> (key column, {column: [header aliases]})
TABLE_SPECS: Dict[str, tuple] = {
    "bom": ("item", {
        "item": ["SL NO", "SL. NO.", "SI NO", "S NO", "ITEM", "ITEM NO", "FIND NO", "NO."],
        "part_number": ["PART NUMBER", "PART NO", "P/N", "PART NO."],
        "description": ["DESCRIPTION", "PART NAME", "NOMENCLATURE", "DESC"],
        "qty": ["QTY", "QTY.", "QUANTITY", "QTY REQD"],
        "ref": ["REF./MATERIAL", "REF/MATERIAL", "MATERIAL / REF.", "MATERIAL/REF", "REF",
                "REF.", "REFERENCE", "MATERIAL", "REMARKS"],
    }),
    "revision": ("rev", {
        "rev": ["REV NO.", "REV NO", "REV.", "REV", "REVISION"],
        "zone": ["ZONE"],
        "details": ["REVISION DETAILS", "DESCRIPTION", "DETAILS", "CHANGE"],
        "date": ["DATE"],
        "approved": ["APPROVED", "APPD"],
    }),
}


@dataclass
class Table:
    kind: str
    header_cells: Dict[str, Cell]
    rows: List[Dict[str, str]]
    bbox: BBox
    header_at: str  # "top" | "bottom"
    warnings: List[str] = field(default_factory=list)


def _match_column(text: str, columns: Dict[str, List[str]]) -> Optional[str]:
    t = _norm(text)
    if not t or len(t) > 24:
        return None
    best, best_len = None, 0
    for col, aliases in columns.items():
        for a in aliases:
            na = _norm(a)
            if t == na and len(na) > best_len:
                best, best_len = col, len(na)
    return best


def _y_overlap(a: Cell, b: Cell) -> float:
    return min(a.y1, b.y1) - max(a.y0, b.y0)


def _header_rows(cells: Sequence[Cell], key: str, columns) -> List[Dict[str, Cell]]:
    matched = [(c, _match_column(c.text, columns)) for c in cells]
    matched = [(c, m) for c, m in matched if m]
    rows: List[Dict[str, Cell]] = []
    used = set()
    for c, m in matched:
        if id(c) in used or m != key:
            continue
        row = {m: c}
        for d, n in matched:
            if d is c or n in row or id(d) in used:
                continue
            if _y_overlap(c, d) > 0.5 * min(c.height, d.height) and abs(d.yc - c.yc) < c.height:
                row[n] = d
        if len(row) >= 2:
            for d in row.values():
                used.add(id(d))
            rows.append(row)
    return rows


_QTY_RE = re.compile(r"^(\d+(\.\d+)?\s*(NOS?|PCS?|NO|SETS?|m|mm|ml|g|kg|L)?\.?|A/?R|AS\s*REQ'?D?)$", re.I)


def _columns(header: Dict[str, Cell]) -> List[Tuple[str, float, float]]:
    """Column spans from the header cells, with any gap between two header
    cells kept as an unnamed column (a header word OCR missed)."""
    cols = sorted(((n, c.x0, c.x1) for n, c in header.items()), key=lambda t: t[1])
    out: List[Tuple[str, float, float]] = []
    for i, (n, x0, x1) in enumerate(cols):
        if out and x0 - out[-1][2] > 8:
            out.append((f"col_{len(out)}", out[-1][2], x0))
        out.append((n, x0, x1))
    return out


def _ocr_box(sheet: Sheet, box, psm: int = 7) -> Optional[str]:
    from .model import Cell as _C
    t = ocr_cell(sheet, _C(bbox=box), psm=psm)
    return normalize_ocr(t) if t else None


def _collect(header: Dict[str, Cell], cells: Sequence[Cell], key: str, direction: int,
             sheet: Optional[Sheet], reocr: bool) -> List[Dict[str, str]]:
    """Collect rows outward from the header (direction -1 = upward).

    Each row is a band between two ruling lines. The band is anchored on the
    key-column cell (item / rev number) when there is one, otherwise on the
    nearest common cell edge. A cell belongs to the band its *top* is in, so
    text that overflows its row's bottom line stays with the row it starts
    in, and one item spanning sub-rows (PAINT / TOP COAT / PRIMER) stays one
    row. A band holding a cell that doesn't fit inside one column belongs to
    something else (title block, zone labels) and collection stops there."""
    cols = _columns(header)
    left = cols[0][1] - 2
    right = cols[-1][2] + 2

    def column_of(c: Cell) -> Optional[str]:
        for name, x0, x1 in cols:
            if x0 - 3 <= c.x0 and c.x1 <= x1 + 3:
                return name
        return None

    hdr_h = sorted(c.height for c in header.values())[len(header) // 2]
    max_row_h = max(4.0 * hdr_h, 30.0)
    near = sorted((c.y0 if direction < 0 else c.y1) for c in header.values())
    edge = near[len(near) // 2]
    hdr_ids = {id(c) for c in header.values()}
    side = [c for c in cells if left <= c.xc <= right and id(c) not in hdr_ids
            and ((c.yc < edge) if direction < 0 else (c.yc > edge))]
    used = set()

    def anchor_y(c: Cell) -> float:
        return c.y0 + min(2.0, c.height / 2)

    rows: List[Dict[str, str]] = []
    while True:
        starts = [c for c in side if id(c) not in used
                  and abs((c.y1 if direction < 0 else c.y0) - edge) <= 3.0]
        if not starts:
            break
        keyed = [c for c in starts if column_of(c) == key]
        # Row ends where its item cell ends; with no item cell, at the
        # farthest edge (a tall cell = one item spanning sub-rows).
        fars = [(c.y0 if direction < 0 else c.y1) for c in (keyed or starts)]
        far = fars[0] if keyed else (min(fars) if direction < 0 else max(fars))
        lo, hi = (far, edge) if direction < 0 else (edge, far)
        if hi - lo > max_row_h:
            break  # a box much taller than a table row is something else
        band = [c for c in side if id(c) not in used and lo - 0.5 <= anchor_y(c) <= hi + 0.5]
        if not band or any(column_of(c) is None for c in band):
            break
        row: Dict[str, List[str]] = {}
        for c in sorted(band, key=lambda c: (c.y0, c.x0)):
            used.add(id(c))
            text = c.text
            if reocr and sheet is not None and sheet.text_source == "ocr":
                t = ocr_cell(sheet, c)
                if t and re.search(r"[A-Za-z0-9]", t):
                    text = normalize_ocr(t)
            row.setdefault(column_of(c), []).append(" ".join(text.split()))
        if sheet is not None and sheet.text_source == "ocr":
            # Short entries ("3.", a quantity of "0") are easy for the
            # page-wide OCR pass to miss entirely, so their cell never formed.
            # Read each empty column's box in this row directly.
            for name, cx0, cx1 in cols:
                if name in row:
                    continue
                t = _ocr_box(sheet, (cx0, lo, cx1, hi), psm=7)
                t = (t or "").strip()
                if not t or not re.search(r"[A-Za-z0-9]", t):
                    continue
                if name == key and not re.fullmatch(r"[0-9]{1,3}[A-Z]?\.?", t):
                    continue
                row[name] = [t]
        if key in row or len(row) >= 2:
            rows.append({k: " / ".join(v) for k, v in row.items()})
        edge = far
    return rows


def _name_gap_columns(rows: List[Dict[str, str]], found: set) -> None:
    """Rename an unnamed (OCR-missed header) column to 'qty' if that column
    is missing and the values look like quantities."""
    gaps = {k for r in rows for k in r if k.startswith("col_")}
    for g in gaps:
        vals = [r[g] for r in rows if r.get(g)]
        if "qty" not in found and vals and sum(1 for v in vals if _QTY_RE.match(v.strip())) >= 0.6 * len(vals):
            for r in rows:
                if g in r:
                    r["qty"] = r.pop(g)
            found.add("qty")


def find_tables(cells: Sequence[Cell], kind: str = "bom", sheet: Optional[Sheet] = None,
                reocr: bool = True, region: Optional[BBox] = None) -> List[Table]:
    """All tables of `kind` ("bom" or "revision") on the sheet."""
    key, columns = TABLE_SPECS[kind]
    if region:
        cells = [c for c in cells if region[0] <= c.xc <= region[2] and region[1] <= c.yc <= region[3]]
    tables: List[Table] = []
    for header in _header_rows(cells, key, columns):
        up = _collect(header, cells, key, -1, sheet, reocr)
        down = _collect(header, cells, key, +1, sheet, reocr)
        rows, at = (up, "bottom") if len(up) > len(down) else (down, "top")
        for r in rows:
            if key in r:
                r[key] = re.sub(r"[.\s]+$", "", r[key]).strip()
        _name_gap_columns(rows, set(header))
        hcs = list(header.values())
        bbox = (min(c.x0 for c in hcs), min(c.y0 for c in hcs), max(c.x1 for c in hcs), max(c.y1 for c in hcs))
        tables.append(Table(kind=kind, header_cells=header, rows=rows, bbox=bbox, header_at=at))
    return tables
