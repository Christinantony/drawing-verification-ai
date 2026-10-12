"""
Read the title block from the cells of a sheet.

Each field is found by its label *inside a ruled cell* and its value is the
rest of that cell's text (how both SolidWorks and AutoCAD templates lay out
"DRAWN / P.K", "TITLE : ...", "REV.00", "SHEET:1/1"). If a label's cell has
nothing else in it, the value is taken from the next unlabelled cell below
or to the right. A cell holding several labels (a missing separator line on
a scan) is split at each label.

Labels are configurable: pass `labels=` to read_title_block() to add a
company's own wording. The defaults cover the common ANSI/ISO wording plus
what common company templates use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .cells import build_cells, normalize_ocr, ocr_cell
from .model import BBox, Cell, Sheet

# field -> regex alternatives. Matched case-insensitively, at a word start.
DEFAULT_LABELS: Dict[str, List[str]] = {
    "drawing_number": [r"DRAWING\s*N[O0]\.?", r"DWG\.?\s*N[O0]\.?", r"DRG\.?\s*N[O0]\.?",
                       r"DOCUMENT\s*N[O0]\.?", r"PART\s*N[O0]\.?"],
    "next_assy": [r"NEXT\s*ASS(?:Y|EMBLY)\.?", r"USED\s*ON"],
    "title": [r"TITLE"],
    "material": [r"MATERIAL", r"MATL\.?"],
    "finish": [r"FINISH"],
    "drawn_by": [r"DRAWN(?:\s*BY)?", r"DRAFTED(?:\s*BY)?"],
    "checked_by": [r"CHECKED(?:\s*BY)?", r"CHK'?D\.?"],
    "verified_by": [r"VERIFIED(?:\s*BY)?(?!\s*AND)"],
    # AutoCAD template's customer sign-off strip, read separately so its
    # heading isn't mistaken for a "VERIFIED" field.
    "_customer_signoff": [r"VERIFIED\s*AND\s*APPROVED\s*BY"],
    "approved_by": [r"APPROVED(?:\s*BY)?", r"APPR?D\.?"],
    "weight": [r"WEIGHT", r"WT\."],
    "scale": [r"SCALE"],
    "size": [r"SIZE"],
    "sheet": [r"SHEET", r"SHT\.?"],
    "revision": [r"REV(?:ISION)?\.?(?:\s*N[O0]\.?)?"],
    "date": [r"DATE"],
}

# Fields that can legitimately appear more than once (two CHECKED boxes).
MULTI_FIELDS = {"checked_by"}

DATE_RE = re.compile(r"\b(\d{1,2}[/.\-]\d{1,2}[/.\-](?:\d{4}|\d{2}))\b")


@dataclass
class TitleBlock:
    fields: Dict[str, Optional[str]]
    region: Optional[BBox]
    cells: List[Cell]
    general_tolerance: Dict[str, Optional[str]] = field(default_factory=dict)
    general_tolerance_text: str = ""
    warnings: List[str] = field(default_factory=list)

    def get(self, name: str) -> Optional[str]:
        return self.fields.get(name)


def _compile(labels: Dict[str, List[str]]):
    pats = []
    for name, alts in labels.items():
        for a in alts:
            pats.append((name, re.compile(rf"(?<![A-Z0-9])(?:{a})(?![A-Z])", re.IGNORECASE)))
    return pats


def _label_hits(text: str, pats) -> List[Tuple[int, int, str]]:
    """All label occurrences in text as (start, end, field), longest match
    wins at overlapping positions."""
    hits = []
    for name, rx in pats:
        for m in rx.finditer(text):
            hits.append((m.start(), m.end(), name))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    out = []
    last_end = -1
    for s, e, n in hits:
        if s >= last_end:
            out.append((s, e, n))
            last_end = e
    return out


def _clean_value(v: str) -> str:
    v = v.replace("\n", " ")
    v = re.sub(r"\s+", " ", v).strip(" :.-|")
    # A lone leading colon/period left over from "No. :" style labels.
    v = re.sub(r"^[:.]\s*", "", v)
    return v.strip()


_TABLE_HEADER_RE = re.compile(r"\bQTY\b|DESCRIPTION|SL\.?\s*N[O0]|REF\.?\s*/|/\s*REF|REVISION\s*DETAILS|\bZONE\b", re.I)


def _is_table_header(c: Cell) -> bool:
    """BOM / revision-table header cells ("REF./MATERIAL", "REV NO.") contain
    title-block label words but are not title-block fields."""
    return bool(_TABLE_HEADER_RE.search(c.text))


def _touching(a: Cell, b: Cell, gap: float = 3.0) -> bool:
    x_ov = min(a.x1, b.x1) - max(a.x0, b.x0)
    y_ov = min(a.y1, b.y1) - max(a.y0, b.y0)
    return (x_ov > -gap and y_ov > 0.3 * min(a.height, b.height)) or \
           (y_ov > -gap and x_ov > 0.3 * min(a.width, b.width))


def _find_region(cells: Sequence[Cell], sheet: Sheet, pats):
    """The title block is the group of ruled cells connected to the
    drawing-number cell in the lower part of the sheet. Growing only through
    cells that touch each other keeps out free-standing labels that happen to
    contain a title-block word ("DETAIL A / SCALE 2 : 5", "SECTION C-C")."""
    # Zone-reference boxes in the border ("8 7 6 5 ...") touch the title
    # block's bottom edge; a single character is never a title-block field.
    lower = [c for c in cells if c.yc > sheet.height * 0.55
             and c.width < sheet.width * 0.45 and c.height < sheet.height * 0.12
             and len(re.sub(r"\s", "", c.text)) > 1]
    # A box that encloses other cells is a container (a view's frame zone
    # holding "DETAIL A / SCALE 2 : 5"), not a title-block field.
    lower = [c for c in lower if not any(
        o is not c and c.x0 < o.xc < c.x1 and c.y0 < o.yc < c.y1 and o.width * o.height < c.width * c.height
        for o in lower)]
    labelled = [c for c in lower if not _is_table_header(c) and _label_hits(c.text, pats)]
    if not labelled:
        return None, []
    anchors = [c for c in labelled if any(n == "drawing_number" for *_, n in _label_hits(c.text, pats))]
    anchor = max(anchors, key=lambda c: c.yc) if anchors else max(labelled, key=lambda c: c.x1 + c.yc)

    group = [anchor]
    seen = {id(anchor)}
    frontier = [anchor]
    while frontier:
        cur = frontier.pop()
        for c in lower:
            if id(c) not in seen and _touching(cur, c):
                seen.add(id(c))
                group.append(c)
                frontier.append(c)
    # A BOM or revision table sitting directly on top of the title block also
    # touches it; cut the group at the topmost labelled title-block row.
    lab = [c for c in group if not _is_table_header(c) and _label_hits(c.text, pats)]
    top = min(c.y0 for c in lab)
    group = [c for c in group if c.y0 >= top - 2]
    region = (min(c.x0 for c in group) - 1, top - 1,
              max(c.x1 for c in group) + 1, max(c.y1 for c in group) + 1)
    return region, group


_GT_ROWS = (("without_decimal", r"WITHOUT\s*DECIMAL", 0), ("one_decimal", r"ONE\s*DECIMAL", 1),
            ("two_decimal", r"TWO\s*DECIMAL", 2), ("three_decimal", r"THREE\s*DECIMAL", 3),
            ("angle", r"ANGLE", None))


def _plausible_tol(raw: str, decimals: Optional[int]) -> Optional[str]:
    """Pick a plausible reading of a general-tolerance value. OCR reads the
    "±" as '+', '#', '=', '£' or even a digit ('205' for '±0.5'), and can
    drop the decimal point ('05'). A tier's tolerance is a small number with
    no more decimals than the tier itself, which rules out the misreads."""
    raw = raw.strip()
    if "½" in raw or "1/2" in raw:
        return "0.5"
    digits = re.sub(r"[^0-9.]", "", raw)
    cands = [digits, digits[1:]] if digits else []
    out = []
    for c in cands:
        for v in (c, ("0." + c[1:]) if c.startswith("0") and "." not in c and len(c) > 1 else None):
            if not v or v.count(".") > 1 or v.startswith(".") or v.endswith("."):
                continue
            try:
                f = float(v)
            except ValueError:
                continue
            dec = len(v.split(".")[1]) if "." in v else 0
            limit = 2.0 if decimals is None else (5.0 if decimals == 0 else 1.0)
            if 0 < f <= limit and (decimals is None or dec <= max(decimals, 1)):
                out.append(v)
    return out[0] if out else None


def _parse_general_tolerance(*texts: str, ocr: bool = False) -> Dict[str, Optional[str]]:
    """Parse the 'TOLERANCES UNLESS OTHERWISE SPECIFIED' block. Several
    readings of the same block (page OCR, per-cell OCR, text layer) can be
    passed; the first plausible value per tier wins."""
    out: Dict[str, Optional[str]] = {k: None for k, _, _ in _GT_ROWS}
    for text in texts:
        if not text:
            continue
        t = normalize_ocr(text).upper()
        for key, rx, dec in _GT_ROWS:
            if out[key]:
                continue
            m = re.search(rx + r"[ \t:.;=]*([^\n]{0,12})", t)
            if not m:
                continue
            v = _plausible_tol(m.group(1), dec)
            if v and ocr and key == "angle" and "." not in v and v not in ("1", "0.5"):
                v = None  # OCR turns the '½' glyph into '2', '4' or 'h': don't trust it
            if v:
                out[key] = "±" + v + ("°" if key == "angle" else "")
    return out


def _ocr_value_fixes(name: str, v: str) -> str:
    """Repairs that are only safe on OCR text, where the symbol was never
    in the source: '320+20g' is '320±20g' (a weight can't be one-sided),
    '35q' is '35g'."""
    if name == "weight":
        v = re.sub(r"(\d)\s*[+£#]\s*(\d)", r"\1±\2", v)
        v = re.sub(r"(\d)\s*q\b", r"\1g", v)
    return v


def read_title_block(sheet: Sheet, cells: Optional[List[Cell]] = None,
                     labels: Optional[Dict[str, List[str]]] = None,
                     reocr: bool = True) -> TitleBlock:
    labels = labels or DEFAULT_LABELS
    pats = _compile(labels)
    cells = cells if cells is not None else build_cells(sheet)
    warnings: List[str] = []

    region, tb_cells = _find_region(cells, sheet, pats)
    fields: Dict[str, Optional[str]] = {k: None for k in labels}
    if region is None:
        warnings.append("No title block found (no labelled ruled cells in the lower part of the sheet).")
        return TitleBlock(fields, None, [], warnings=warnings)


    page_text = {id(c): c.text for c in tb_cells}
    if reocr and sheet.text_source == "ocr":
        # Re-read each cell on its own. Keep the re-read only if it still
        # finds every label the page-wide pass found: a small label in the
        # corner of a big cell ("TITLE :") is sometimes lost in the crop,
        # while values ("AT-KD" not "AI-KD") usually come out better.
        for c in tb_cells:
            t = ocr_cell(sheet, c)
            if not t or not re.search(r"[A-Za-z0-9]", t):
                continue
            t = normalize_ocr(t)
            before = {n for *_, n in _label_hits(c.text, pats)}
            after = {n for *_, n in _label_hits(t, pats)}
            if before <= after:
                c.text = t

    multi: Dict[str, List[str]] = {}
    label_only: List[Tuple[str, Cell]] = []
    labelled_ids = set()
    for c in tb_cells:
        if _is_table_header(c):
            continue
        hits = _label_hits(c.text, pats)
        if not hits:
            continue
        labelled_ids.add(id(c))
        for i, (s, e, name) in enumerate(hits):
            nxt = hits[i + 1][0] if i + 1 < len(hits) else len(c.text)
            value = _clean_value(c.text[e:nxt])
            if name == "revision":
                # "REV.00" / "REV. 01" - keep just the code
                m = re.match(r"([A-Z0-9]{1,3})\b", value, re.I)
                value = m.group(1) if m else value
            if not value:
                label_only.append((name, c))
                continue
            if name in MULTI_FIELDS:
                multi.setdefault(name, []).append(value)
            elif fields.get(name) is None:
                fields[name] = value

    # Label-only cells: take the nearest unlabelled cell below or right.
    for name, lc in label_only:
        if name.startswith("_") or (fields.get(name) and name not in MULTI_FIELDS):
            continue
        best, best_d = None, None
        for c in tb_cells:
            if id(c) in labelled_ids or c is lc:
                continue
            x_ov = min(c.x1, lc.x1) - max(c.x0, lc.x0)
            y_ov = min(c.y1, lc.y1) - max(c.y0, lc.y0)
            if c.y0 >= lc.y1 - 2 and x_ov > 0.5 * min(c.width, lc.width):
                d = c.y0 - lc.y1
            elif c.x0 >= lc.x1 - 2 and y_ov > 0.5 * min(c.height, lc.height):
                d = c.x0 - lc.x1
            else:
                continue
            if d < 30 and (best_d is None or d < best_d):
                best, best_d = c, d
        if best is not None:
            v = _clean_value(best.text)
            if name in MULTI_FIELDS:
                multi.setdefault(name, []).append(v)
            else:
                fields[name] = v

    for name, vals in multi.items():
        fields[name] = ", ".join(vals)
    fields = {k: v for k, v in fields.items() if not k.startswith("_")}

    # Date with no "DATE" label (e.g. under the REV box).
    if not fields.get("date"):
        for c in sorted(tb_cells, key=lambda c: -c.x1):
            m = DATE_RE.search(c.text)
            if m:
                fields["date"] = m.group(1)
                break

    # General tolerance block: every cell in the column under "TOLERANCES".
    tol_cells = [c for c in tb_cells if re.search(r"TOLERANCE", c.text, re.I)]
    gt_text = ""
    if tol_cells:
        tc = tol_cells[0]
        col = [c for c in cells if c.x0 >= tc.x0 - 2 and c.x1 <= tc.x1 + 60
               and c.y0 >= tc.y0 - 2 and c.y1 <= region[3] + 2]
        gt_text = "\n".join(c.text for c in sorted(col, key=lambda c: (c.y0, c.x0)))
    general = {}
    if gt_text:
        alt = "\n".join(page_text.get(id(c), "") for c in tb_cells if re.search(r"DECIMAL|ANGLE", page_text.get(id(c), ""), re.I))
        general = _parse_general_tolerance(gt_text, alt, ocr=sheet.text_source == "ocr")

    if sheet.text_source == "ocr":
        for name, v in list(fields.items()):
            if v:
                fields[name] = _ocr_value_fixes(name, v)

    return TitleBlock(fields=fields, region=region, cells=tb_cells,
                      general_tolerance=general, general_tolerance_text=gt_text,
                      warnings=warnings)
