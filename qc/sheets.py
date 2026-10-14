"""Stations 2 and 3: read each sheet, then sort its text into regions.

Every sheet, whether it has a text layer or was read by OCR, becomes a
QCSheet: the reader's record (title block, BOM, revision table), the words
with positions and confidence, and a list of Blocks - the "floating parts"
of the drawing:

    title_block, bom, revision   ruled tables (found by the reader)
    notes                        every NOTE / NOTES block, split into notes
    callout                      hole / thread callouts (Ø, THRU, CSK, TAPPED...)
    view_label                   DETAIL A, SECTION A-A, "XY-... (PCB-A)"
    legend                       'XXXX' - SOLDERING POINTS, '*' - KEY ...
    balloon                      a number inside a circle
    unplaced                     text that fits none of the above (flagged)

Dimension text inside the views (numbers, tolerances, Ø-values) is ignored:
the agent checks the literal parts of a drawing, not its geometry.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sheetreader.cells import reading_order  # noqa: E402
from sheetreader.loader import _ink_gray, _np_rotate_cw, load_sheets  # noqa: E402
from sheetreader.record import read_sheet  # noqa: E402

BBox = Tuple[float, float, float, float]

CROP_DPI = 110          # report crops
BALLOON_DPI = 150       # circle test around balloon numbers
REREAD_DPI = 400        # doubt-loop re-reads


def union(boxes) -> Optional[BBox]:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def inside(w, box, pad=0.0) -> bool:
    return box is not None and box[0] - pad <= w.xc <= box[2] + pad and box[1] - pad <= w.yc <= box[3] + pad


@dataclass
class Line:
    text: str
    bbox: BBox
    words: list
    conf: float  # lowest word confidence (100 for a text layer)


@dataclass
class Block:
    kind: str
    bbox: BBox
    lines: List[Line]
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)


@dataclass
class Note:
    number: Optional[str]   # "1", "2" ... or None for a bullet
    text: str
    bbox: BBox
    block: int              # which note block on the sheet
    conf: float = 100.0


@dataclass(eq=False)   # compared and hashed by identity: one object per sheet
class QCSheet:
    path: str
    file: str
    page: int
    sheet: object
    record: dict
    parts: dict
    blocks: List[Block] = field(default_factory=list)
    notes: List[Note] = field(default_factory=list)
    balloons: Dict[str, List[BBox]] = field(default_factory=dict)
    frame: Optional[BBox] = None
    _img: dict = field(default_factory=dict, repr=False)

    # -- identity ---------------------------------------------------------
    @property
    def ocr(self) -> bool:
        return self.record["text_source"] == "ocr"

    @property
    def number(self) -> Optional[str]:
        return self.record["title_block"].get("drawing_number") or self.record.get("filename_drawing_number")

    @property
    def label(self) -> str:
        return self.file if self.record.get("page_count", 1) == 1 else f"{self.file} p{self.page}"

    def blocks_of(self, kind) -> List[Block]:
        return [b for b in self.blocks if b.kind == kind]

    # -- images -----------------------------------------------------------
    def image(self, dpi: int = CROP_DPI) -> np.ndarray:
        """Upright greyscale image of the sheet at `dpi` (cached)."""
        if dpi not in self._img:
            import pymupdf
            doc = pymupdf.open(self.path)
            pix = doc[self.page - 1].get_pixmap(dpi=dpi)
            rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
            doc.close()
            self._img[dpi] = _np_rotate_cw(_ink_gray(rgb), self.sheet.rotation_deg)
        return self._img[dpi]

    def crop(self, bbox: BBox, dpi: int = CROP_DPI, pad: float = 6.0) -> np.ndarray:
        img = self.image(dpi)
        k = dpi / 72.0
        h, w = img.shape
        x0, y0 = max(0, int((bbox[0] - pad) * k)), max(0, int((bbox[1] - pad) * k))
        x1, y1 = min(w, int((bbox[2] + pad) * k)), min(h, int((bbox[3] + pad) * k))
        return img[y0:y1, x0:x1]

    def drop_images(self):
        self._img.clear()


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
NOTE_HEAD = re.compile(r"^NOTES?\s*:?$", re.I)
# Other headed text blocks, read the same way as notes.
TEXT_HEAD = re.compile(r"^(?:INSTALLATION\s+)?INSTRUCTIONS?\s*:?$|^[A-Z]+\s+DETAILS\s*:?$|^SPECIFICATIONS?\s*:?$", re.I)
NOTE_NUM = re.compile(r"^\(?(\d{1,2})\)?[.):]\s*(?:[.)]\s*)?(.*)$")
BULLET = re.compile(r"^(?:[•·\-–*]|e|o|°)\s+(.*)$")
CALLOUT = re.compile(r"(Ø|⌀|\bDIA\b|\bTHRU\b|\bCSK\b|C'?BORE|COUNTER\s*BORE|\bTAPPED\b|\bDEEP\b|"
                     r"\bM\d+(\.\d+)?\b(?!\s*SS)|\bHOLES?\b|\bDRILL\b|\bSPOT\s*FACE\b)", re.I)
VIEW_LABEL = re.compile(r"\b(DETAIL|SECTION|VIEW)\s+['\"]?[A-Z0-9]{1,3}['\"]?(\s*-\s*[A-Z0-9]{1,3})?\b|"
                        r"\bSCALE\s*\d+(\.\d+)?\s*:\s*\d+", re.I)
LEGEND = re.compile(r"^(['\"‘’`“”][*X#@]{1,6}['\"‘’`“”]\s*[-–=:]?|[*X#@]{1,6}\s*[-–=:])\s*[A-Z]", re.I)
DIMENSION_ONLY = re.compile(r"^[\sØ⌀R*±+\-–.,/°xX()0-9]*$")


def _lines(words, gap_factor: float = 3.5) -> List[Line]:
    """Words -> text lines, split where the gap inside a line is large (two
    separate labels on one row stay two lines)."""
    out = []
    for ln in reading_order(words):
        cur = [ln[0]]
        for prev, w in zip(ln, ln[1:]):
            gap = (w.x0 - prev.x1) if prev.horizontal else abs(w.yc - prev.yc) - (prev.height + w.height)
            if gap > gap_factor * max(prev.height, w.height, 4.0):
                out.append(_mk_line(cur))
                cur = []
            cur.append(w)
        out.append(_mk_line(cur))
    return out


def _mk_line(ws) -> Line:
    return Line(" ".join(w.text for w in ws), union(w.bbox for w in ws), ws, min(w.conf for w in ws))


# ---------------------------------------------------------------------------
# Station 2: read
# ---------------------------------------------------------------------------
def read_pdf(path) -> List[QCSheet]:
    path = str(path)
    sheets = load_sheets(path)
    out = []
    for sh in sheets:
        rec, parts = read_sheet(sh, path, len(sheets))
        q = QCSheet(path=path, file=Path(path).name, page=sh.page_number, sheet=sh,
                    record=rec.to_dict(), parts=parts)
        locate(q)
        out.append(q)
    return out


# ---------------------------------------------------------------------------
# Station 3: locate the parts
# ---------------------------------------------------------------------------
def _frame(sh) -> BBox:
    """Inner drawing border: the long ruling lines closest to the middle on
    each side. Text outside it is border text (zone letters, the
    confidentiality line, the form number)."""
    W, H = sh.width, sh.height
    vs = [s.x0 for s in sh.v_lines if (s.y1 - s.y0) > 0.6 * H]
    hs = [s.y0 for s in sh.h_lines if (s.x1 - s.x0) > 0.6 * W]
    # Only lines in the outer 8% of the sheet are border candidates (a long
    # centre line in a view is not); the innermost of them is the frame.
    left = max([x for x in vs if x < 0.08 * W], default=0.0)
    right = min([x for x in vs if x > 0.92 * W], default=W)
    top = max([y for y in hs if y < 0.08 * H], default=0.0)
    bottom = min([y for y in hs if y > 0.92 * H], default=H)
    return (left, top, right, bottom)


def _table_box(table, cells) -> BBox:
    """A table's full extent: its header plus every cell under its columns
    whose text is one of its row values."""
    hdr = list(table.header_cells.values())
    x0, x1 = min(c.x0 for c in hdr) - 2, max(c.x1 for c in hdr) + 2
    span = 30.0 * (len(table.rows) + 1)
    ylo = min(c.y0 for c in hdr) - (span if table.header_at == "bottom" else 2)
    yhi = max(c.y1 for c in hdr) + (span if table.header_at == "top" else 2)
    values = set()
    for r in table.rows:
        for v in r.values():
            for part in str(v).split(" / "):
                values.add(" ".join(part.split()))
    got = [c.bbox for c in hdr]
    for c in cells:
        if x0 <= c.x0 and c.x1 <= x1 and ylo <= c.yc <= yhi and " ".join(c.text.split()) in values:
            got.append(c.bbox)
    return union(got)


def _notes(q: QCSheet, taken) -> None:
    sh = q.sheet
    words = [w for w in sh.words if w.horizontal and not taken(w) and inside(w, q.frame)]
    heads = []
    # Two-word headings ("INSTALLATION INSTRUCTIONS", "MARKING DETAILS") are
    # matched on their line and stand in as one heading word.
    from sheetreader.model import Word as _W
    for ln in _lines(words):
        t = ln.text.strip()
        if len(ln.words) > 1 and TEXT_HEAD.match(t):
            a, b = ln.words[0], ln.words[-1]
            heads.append(_W(t, a.x0, min(x.y0 for x in ln.words), b.x1, max(x.y1 for x in ln.words)))
    for i, w in enumerate(words):
        if NOTE_HEAD.match(w.text) or TEXT_HEAD.match(w.text):
            heads.append(w)
        elif w.text.upper() in ("NOTE", "NOTES"):
            nxt = [x for x in words if abs(x.yc - w.yc) < 2 and 0 <= x.x0 - w.x1 < 6 and x.text.strip() == ":"]
            if nxt:
                heads.append(w)
    used = set()
    for h in heads:
        for w in words:
            if inside(w, h.bbox, 0.5):
                used.add(id(w))
    for bi, h in enumerate(sorted(heads, key=lambda w: (w.y0, w.x0))):
        lh = max(4.0, h.y1 - h.y0)
        # A heading inside its own ruled box ("INSTALLATION INSTRUCTIONS"):
        # the box is the block.
        box = None
        for c in (q.parts["cells"] if not NOTE_HEAD.match(h.text) else []):
            if (c.x0 - 1 <= h.xc <= c.x1 + 1 and c.y0 - 1 <= h.yc <= c.y1 + 1 and c.height > 3 * lh
                    and c.height < 0.5 * sh.height and c.width < 0.7 * sh.width):
                if box is None or c.width * c.height < box.width * box.height:
                    box = c
        if box is not None:
            cand = [w for w in words if id(w) not in used and inside(w, box.bbox, 0.5) and w.yc > h.yc + 0.45 * lh]
            kept = sorted(_lines(cand, gap_factor=6.0), key=lambda l: (l.bbox[1], l.bbox[0]))
            lines = []
        else:
            cand = [w for w in words if id(w) not in used and w is not h and w.y0 >= h.y0 - 1
                    and w.x0 >= h.x0 - 3 * lh and w.y0 <= h.y1 + 400]
            lines = sorted(_lines(cand, gap_factor=6.0), key=lambda l: l.bbox[1])
            kept = []
        last_y = h.y1
        same_row = [l for l in lines if abs((l.bbox[1] + l.bbox[3]) / 2 - h.yc) < lh * 0.45 and l.bbox[0] > h.x1]
        for l in lines:
            if l in same_row:
                if l.bbox[0] - h.x1 < 80:
                    kept.append(l)
                continue
            if (l.bbox[1] + l.bbox[3]) / 2 < h.yc + 0.45 * lh:
                continue
            if l.bbox[1] - last_y > 2.6 * lh:
                break
            if l.bbox[0] > h.x0 + 40 * lh:   # far to the right: a different block
                continue
            kept.append(l)
            last_y = max(last_y, l.bbox[3])
        # Lines of single marks (soldering 'X' symbols, centre marks) are not notes.
        kept = [l for l in kept if any(len(re.sub(r"[^A-Za-z0-9]", "", w.text)) >= 2 for w in l.words)]
        if not kept:
            continue
        for l in kept:
            for w in l.words:
                used.add(id(w))
        used.add(id(h))
        block = Block("notes", union([h.bbox] + [l.bbox for l in kept]), kept,
                      {"heading": h.text, "is_notes": bool(NOTE_HEAD.match(h.text))})
        q.blocks.append(block)
        # Split into notes: a numbered or bulleted line starts a new note,
        # anything else continues the previous one.
        cur: Optional[Note] = None
        for l in sorted(kept, key=lambda l: (l.bbox[1], l.bbox[0])):
            t = l.text.strip()
            m = NOTE_NUM.match(t)
            b = BULLET.match(t)
            if m and m.group(2):
                cur = Note(m.group(1), m.group(2).strip(), l.bbox, bi, l.conf)
                q.notes.append(cur)
            elif b:
                cur = Note(None, b.group(1).strip(), l.bbox, bi, l.conf)
                q.notes.append(cur)
            elif cur is not None and l.bbox[1] - cur.bbox[3] < 1.6 * lh:
                cur.text = (cur.text + " " + t).strip()
                cur.bbox = union([cur.bbox, l.bbox])
                cur.conf = min(cur.conf, l.conf)
            else:
                cur = Note(None, t, l.bbox, bi, l.conf)
                q.notes.append(cur)


def _ring_score(img: np.ndarray, k: float, cx: float, cy: float, r_pt: float) -> float:
    """Fraction of a circle of radius r around (cx, cy) that is inked."""
    n = 72
    hits = 0
    h, w = img.shape
    for i in range(n):
        a = 2 * math.pi * i / n
        best = 255
        for dr in (-0.6, 0.0, 0.6):
            x = int(round((cx + (r_pt + dr) * math.cos(a)) * k))
            y = int(round((cy + (r_pt + dr) * math.sin(a)) * k))
            if 0 <= x < w and 0 <= y < h:
                best = min(best, int(img[y, x]))
        hits += best < 140
    return hits / n


def _vector_ink(q: QCSheet) -> np.ndarray:
    """Points along curves and very short segments (how CAD exports draw
    circles), in upright sheet coordinates."""
    import pymupdf
    from sheetreader.loader import _rot_point
    doc = pymupdf.open(q.path)
    page = doc[q.page - 1]
    W, H = page.rect.width, page.rect.height
    mat = page.rotation_matrix
    pts = []
    for d in page.get_drawings():
        for it in d.get("items", []):
            if it[0] == "l" and abs(it[2] - it[1]) < 3:
                ps = [it[1]]
            elif it[0] == "c":
                ps = list(it[1:5])
            else:
                continue
            for p in ps:
                p = p * mat
                pts.append(_rot_point(p.x, p.y, W, H, q.sheet.rotation_deg) if q.sheet.rotation_deg else (p.x, p.y))
    doc.close()
    return np.array(pts) if pts else np.zeros((0, 2))


def _circle_around(P: np.ndarray, cx: float, cy: float, rmin: float) -> bool:
    if not len(P):
        return False
    d = np.hypot(P[:, 0] - cx, P[:, 1] - cy)
    m = (d > rmin) & (d < 16.0)
    if m.sum() < 8:
        return False
    for r in np.unique(np.round(d[m] * 2) / 2):
        ring = m & (np.abs(d - r) < 0.8)
        if ring.sum() < 8:
            continue
        ang = np.arctan2(P[ring, 1] - cy, P[ring, 0] - cx)
        if len(set(np.floor((ang + math.pi) / (math.pi / 4)).astype(int))) >= 7:
            return True
    return False


def _balloons(q: QCSheet, taken) -> None:
    sh = q.sheet
    nums = [w for w in sh.words if re.fullmatch(r"\d{1,3}[A-Z]?", w.text) and not taken(w) and inside(w, q.frame)]
    if not nums:
        return
    if not q.ocr:
        P = _vector_ink(q)
        for w in nums:
            half = max(w.x1 - w.x0, w.y1 - w.y0) / 2
            if _circle_around(P, w.xc, w.yc, half + 0.5):
                q.balloons.setdefault(w.text, []).append(w.bbox)
        return
    img = q.image(BALLOON_DPI)
    k = BALLOON_DPI / 72.0
    for w in nums:
        half = max(w.x1 - w.x0, w.y1 - w.y0) / 2
        best = 0.0
        r = max(half + 1.5, 4.0)
        while r <= 16.0:
            best = max(best, _ring_score(img, k, w.xc, w.yc, r))
            if best >= 0.85:
                break
            r += 0.5
        if best >= 0.85:
            q.balloons.setdefault(w.text, []).append(w.bbox)


def locate(q: QCSheet) -> None:
    sh = q.sheet
    q.frame = _frame(sh)
    tb = q.parts["title_block"]
    regions: List[BBox] = []
    if tb.region:
        q.blocks.append(Block("title_block", tb.region, [], {}))
        regions.append(tb.region)
    for kind, key in (("bom", "boms"), ("revision", "revisions")):
        for t in q.parts[key]:
            if not t.rows and kind == "revision":
                box = union(c.bbox for c in t.header_cells.values())
            else:
                box = _table_box(t, q.parts["cells"])
            if box:
                q.blocks.append(Block(kind, box, [], {"rows": len(t.rows), "header_at": t.header_at}))
                regions.append(box)

    def taken(w) -> bool:
        return any(inside(w, b, 1.0) for b in regions)

    _notes(q, taken)
    for b in q.blocks_of("notes"):
        regions.append(b.bbox)
    _balloons(q, taken)
    balloon_boxes = [b for bs in q.balloons.values() for b in bs]

    rest = [w for w in sh.words if not taken(w) and inside(w, q.frame)
            and not any(abs(w.xc - (b[0] + b[2]) / 2) < 1 and abs(w.yc - (b[1] + b[3]) / 2) < 1 for b in balloon_boxes)]
    unplaced: List[Line] = []
    for l in _lines(rest):
        t = l.text.strip()
        if not t:
            continue
        if LEGEND.match(t):
            q.blocks.append(Block("legend", l.bbox, [l]))
        elif VIEW_LABEL.search(t) or re.search(r"\(\s*[A-Z][A-Z0-9 &\-]{1,30}\s*\)\s*$", t) and re.search(r"\d", t):
            q.blocks.append(Block("view_label", l.bbox, [l]))
        elif CALLOUT.search(t):
            q.blocks.append(Block("callout", l.bbox, [l]))
        elif DIMENSION_ONLY.match(t) or len(re.sub(r"[^A-Za-z]", "", t)) <= 2:
            continue   # dimension text or a single letter (section arrows, centre-line marks)
        else:
            unplaced.append(l)
    # Group nearby unplaced lines into blocks: one flag per block, not per line.
    groups: List[List[Line]] = []
    for l in sorted(unplaced, key=lambda l: (l.bbox[1], l.bbox[0])):
        lh = max(4.0, l.bbox[3] - l.bbox[1])
        for g in groups:
            gb = union(x.bbox for x in g)
            if (l.bbox[1] - gb[3] < 1.8 * lh and l.bbox[0] < gb[2] + 2 * lh and l.bbox[2] > gb[0] - 2 * lh):
                g.append(l)
                break
        else:
            groups.append([l])
    for g in groups:
        q.blocks.append(Block("unplaced", union(x.bbox for x in g), g))
