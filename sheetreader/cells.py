"""
Ruled cells from loose lines.

CAD title blocks and tables are rarely drawn as closed rectangles: SolidWorks
and AutoCAD both emit them as separate line segments, so looking for
rectangle objects (what the old pipeline did) finds nothing. Instead, for
every word we cast rays left/right/up/down to the nearest ruling line that
actually spans the word, which gives the box it sits in. Words with the same
box are one cell.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .model import BBox, Cell, Segment, Sheet, Word

# Lines are allowed to stop this short of the probe point and still count
# (plotters and CAD exports leave small gaps at corners).
SPAN_TOL = 1.5


def merge_segments(segs: Sequence[Segment], horizontal: bool, gap: float = 1.5,
                   pos_tol: float = 0.8) -> List[Segment]:
    """Join collinear pieces of the same ruling line."""
    if not segs:
        return []
    key = (lambda s: s.y0) if horizontal else (lambda s: s.x0)
    start = (lambda s: s.x0) if horizontal else (lambda s: s.y0)
    end = (lambda s: s.x1) if horizontal else (lambda s: s.y1)
    groups: Dict[int, List[Segment]] = defaultdict(list)
    for s in sorted(segs, key=key):
        groups[int(round(key(s) / pos_tol))].append(s)
    out: List[Segment] = []
    for _, g in sorted(groups.items()):
        g.sort(key=start)
        pos = sum(key(s) for s in g) / len(g)
        a, b = start(g[0]), end(g[0])
        for s in g[1:]:
            if start(s) <= b + gap:
                b = max(b, end(s))
            else:
                out.append(Segment(a, pos, b, pos) if horizontal else Segment(pos, a, pos, b))
                a, b = start(s), end(s)
        out.append(Segment(a, pos, b, pos) if horizontal else Segment(pos, a, pos, b))
    return out


class LineIndex:
    """Vectorised nearest-line lookups."""

    def __init__(self, sheet: Sheet):
        hs = merge_segments(sheet.h_lines, True)
        vs = merge_segments(sheet.v_lines, False)
        self.h = np.array([(s.y0, s.x0, s.x1) for s in hs]) if hs else np.zeros((0, 3))
        self.v = np.array([(s.x0, s.y0, s.y1) for s in vs]) if vs else np.zeros((0, 3))

    def box_around(self, x: float, y: float, bx0: float, by0: float, bx1: float,
                   by1: float) -> Optional[BBox]:
        """Box enclosing the point (x, y), whose content extends over
        (bx0..bx1, by0..by1). The enclosing lines must lie outside that extent."""
        h, v = self.h, self.v
        if not len(h) or not len(v):
            return None
        hv = h[(h[:, 1] - SPAN_TOL <= x) & (h[:, 2] + SPAN_TOL >= x)]
        vv = v[(v[:, 1] - SPAN_TOL <= y) & (v[:, 2] + SPAN_TOL >= y)]
        above = hv[hv[:, 0] <= by0 + 0.5]
        below = hv[hv[:, 0] >= by1 - 0.5]
        left = vv[vv[:, 0] <= bx0 + 0.5]
        right = vv[vv[:, 0] >= bx1 - 0.5]
        if not (len(above) and len(below) and len(left) and len(right)):
            return None
        return (float(left[:, 0].max()), float(above[:, 0].max()),
                float(right[:, 0].min()), float(below[:, 0].min()))


def reading_order(words: Sequence[Word], line_tol: float = None) -> List[List[Word]]:
    """Group words into text lines (in each word's reading frame) and sort.
    Mostly-horizontal content is the common case; vertical words are put in
    their own lines in the order they read."""
    if not words:
        return []
    hz = [w for w in words if w.horizontal]
    vt = [w for w in words if not w.horizontal]
    lines: List[List[Word]] = []
    for group in (hz, vt):
        if not group:
            continue
        ref = group[0]
        proj = sorted(((ref.frame(w.xc, w.yc), w) for w in group), key=lambda t: t[0][1])
        heights = sorted((w.y1 - w.y0) if w.horizontal else (w.x1 - w.x0) for w in group)
        tol = line_tol if line_tol is not None else max(1.5, 0.5 * heights[len(heights) // 2])
        cur: List[Tuple[Tuple[float, float], Word]] = []
        cur_v = None
        for (u, v), w in proj:
            if cur and abs(v - cur_v) > tol:
                lines.append([x for _, x in sorted(cur, key=lambda t: t[0][0])])
                cur = []
            cur.append(((u, v), w))
            cur_v = v if cur_v is None or len(cur) == 1 else (cur_v * (len(cur) - 1) + v) / len(cur)
        if cur:
            lines.append([x for _, x in sorted(cur, key=lambda t: t[0][0])])
    return lines


def text_of(words: Sequence[Word]) -> str:
    return "\n".join(" ".join(w.text for w in line) for line in reading_order(words))


def build_cells(sheet: Sheet, region: Optional[BBox] = None,
                max_cell_frac: float = 0.25) -> List[Cell]:
    """Cells (ruled boxes containing at least one word) on the sheet, or only
    those whose words fall inside `region`. Boxes larger than max_cell_frac
    of the sheet area are the drawing area or the frame, not cells."""
    idx = LineIndex(sheet)
    sheet_area = sheet.width * sheet.height
    groups: Dict[Tuple[int, int, int, int], List[Word]] = defaultdict(list)
    boxes: Dict[Tuple[int, int, int, int], BBox] = {}
    for w in sheet.words:
        if region and not (region[0] <= w.xc <= region[2] and region[1] <= w.yc <= region[3]):
            continue
        # Shrink the word box slightly: glyph boxes from a text layer often
        # touch the ruling line they sit next to.
        sx = min(1.0, (w.x1 - w.x0) * 0.15)
        sy = min(1.0, (w.y1 - w.y0) * 0.15)
        box = idx.box_around(w.xc, w.yc, w.x0 + sx, w.y0 + sy, w.x1 - sx, w.y1 - sy)
        if box is None:
            continue
        bw, bh = box[2] - box[0], box[3] - box[1]
        if bw <= 0 or bh <= 0 or bw * bh > max_cell_frac * sheet_area:
            continue
        key = tuple(int(round(v * 2)) for v in box)
        groups[key].append(w)
        boxes[key] = box
    cells = [Cell(bbox=boxes[k], words=ws, text=text_of(ws)) for k, ws in groups.items()]
    cells.sort(key=lambda c: (c.y0, c.x0))
    return cells


def cells_in_region(cells: Sequence[Cell], region: BBox) -> List[Cell]:
    return [c for c in cells if region[0] <= c.xc <= region[2] and region[1] <= c.yc <= region[3]]


def ocr_cell(sheet: Sheet, cell: Cell, psm: int = 6) -> Optional[str]:
    """Re-read one cell from the line-free raster (OCR sheets only). A single
    small, clean crop reads far better than the same text found by a
    whole-page pass, which is what fixes slips like 'AI-KD' for 'AT-KD'."""
    if sheet.raster is None or not sheet.px_per_pt:
        return None
    from PIL import Image
    from .loader import _tesseract

    k = sheet.px_per_pt
    img = sheet.raster
    h, w = img.shape
    inset = 3
    x0 = max(0, int(cell.x0 * k) + inset)
    y0 = max(0, int(cell.y0 * k) + inset)
    x1 = min(w, int(cell.x1 * k) - inset)
    y1 = min(h, int(cell.y1 * k) - inset)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = img[y0:y1, x0:x1]
    # Pad: Tesseract reads text touching the image edge badly.
    pad = 20
    padded = np.full((crop.shape[0] + 2 * pad, crop.shape[1] + 2 * pad), 255, np.uint8)
    padded[pad:-pad, pad:-pad] = crop
    pil = Image.fromarray(padded)
    vert = [w_ for w_ in cell.words if not w_.horizontal]
    if cell.words and sum(len(w_.text) for w_ in vert) > sum(len(w_.text) for w_ in cell.words) / 2:
        # Mostly vertical text: rotate so it reads left-to-right.
        up = sum(1 for w_ in vert if w_.dir[1] < 0) >= len(vert) / 2
        pil = pil.rotate(-90 if up else 90, expand=True, fillcolor=255)
    try:
        txt = _tesseract().image_to_string(pil, config=f"--psm {psm}")
    except Exception:
        return None
    txt = "\n".join(" ".join(line.split()) for line in txt.splitlines() if line.strip())
    return txt or None


def normalize_ocr(text: str) -> str:
    """Tidy common OCR artefacts without changing meaning."""
    t = text.replace("—", "-").replace("–", "-").replace("~", "-")
    t = re.sub(r"-{2,}", "-", t)
    return t.strip(" |")
