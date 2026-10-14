"""
Turn any page of a drawing PDF (or a scanned image) into a Sheet: words with
positions plus the axis-aligned ruling lines, in upright sheet coordinates.

Two kinds of input are handled, and the rest of the code never needs to
know which one it got:

* PDFs with a real text layer (SolidWorks PDF Publisher, most CAD "Save as
  PDF" exports): words are read straight from the text layer with their
  reading direction, ruling lines from the PDF's vector line items. No OCR.

* PDFs with no text layer, and scans/images (AutoCAD plotted through
  "Microsoft Print to PDF", where every letter is drawn as line strokes):
  the page is rasterised, the upright orientation is found by trying each
  rotation and keeping the one where title-block labels actually read,
  ruling lines are found with morphology and erased, and the line-free
  image is OCR'd. Vertical text gets its own OCR pass.

Coloured ink (green/blue/red dimensions are common on plotted sheets) is
handled by taking the darkest channel per pixel, so it OCRs like black.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .model import Segment, Sheet, Word

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# A page needs at least this many words in its text layer to be trusted
# without OCR. A plotted AutoCAD sheet has zero.
MIN_TEXT_WORDS = 20

OCR_DPI = 300
# Ruling lines shorter than this are ignored (letter strokes, arrowheads).
MIN_LINE_MM = 4.0
# Words used to decide which way up an OCR'd sheet is.
ORIENTATION_KEYWORDS = ("DRAWING", "TITLE", "SCALE", "MATERIAL", "CHECKED",
                        "APPROVED", "DRAWN", "SHEET", "SIZE", "REV", "WEIGHT",
                        "TOLERANCES", "FINISH", "ASSY", "DATE", "QTY",
                        "DESCRIPTION")


# --------------------------------------------------------------------------
# Rotation helpers: rotate a W x H frame clockwise by k * 90 degrees.
# --------------------------------------------------------------------------

def _rot_point(x: float, y: float, w: float, h: float, cw_deg: int) -> Tuple[float, float]:
    if cw_deg == 90:
        return h - y, x
    if cw_deg == 180:
        return w - x, h - y
    if cw_deg == 270:
        return y, w - x
    return x, y


def _rot_dir(dx: float, dy: float, cw_deg: int) -> Tuple[float, float]:
    for _ in range((cw_deg // 90) % 4):
        dx, dy = -dy, dx
    return dx, dy


def _rot_bbox(b, w, h, cw_deg):
    xa, ya = _rot_point(b[0], b[1], w, h, cw_deg)
    xb, yb = _rot_point(b[2], b[3], w, h, cw_deg)
    return min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)


def _rotated_size(w: float, h: float, cw_deg: int) -> Tuple[float, float]:
    return (h, w) if cw_deg in (90, 270) else (w, h)


def _dominant_rotation(words: Sequence[Word]) -> int:
    """The clockwise rotation that makes most of the text read left-to-right."""
    votes = {0: 0, 90: 0, 180: 0, 270: 0}
    for w in words:
        dx, dy = w.dir
        n = len(w.text)
        if abs(dx) >= abs(dy):
            votes[0 if dx > 0 else 180] += n
        else:
            votes[90 if dy < 0 else 270] += n
    return max(votes, key=votes.get)


# --------------------------------------------------------------------------
# Text-layer PDFs
# --------------------------------------------------------------------------

def _vector_words(page) -> List[Word]:
    """Words from the PDF text layer, keeping each line's reading direction.
    PyMuPDF's plain "words" output drops direction, which is what made
    rotated dimension text come back reversed in the old pipeline."""
    import pymupdf

    mat = page.rotation_matrix  # honours a declared /Rotate
    words: List[Word] = []
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            d = line.get("dir", (1.0, 0.0))
            dvec = pymupdf.Point(d) * mat - pymupdf.Point(0, 0) * mat
            ddir = (round(dvec.x, 3), round(dvec.y, 3))
            buf: List[Tuple[str, pymupdf.Rect]] = []

            def flush():
                if not buf:
                    return
                text = "".join(c for c, _ in buf)
                r = pymupdf.Rect(buf[0][1])
                for _, cr in buf[1:]:
                    r |= cr
                r = r * mat
                words.append(Word(text=text, x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1, dir=ddir))
                buf.clear()

            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch["c"]
                    if c.isspace():
                        flush()
                    else:
                        buf.append((c, pymupdf.Rect(ch["bbox"])))
                # A font change inside one visual run (e.g. a "0.1" set in a
                # different span right after "101.2") still counts as a
                # separate word only if there's a real gap; spans that touch
                # are glued, which is what a reader would see.
            flush()
    return words


def _vector_segments(page, min_len_pt: float) -> Tuple[List[Segment], List[Segment]]:
    mat = page.rotation_matrix
    hs: List[Segment] = []
    vs: List[Segment] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return hs, vs
    for d in drawings:
        for item in d.get("items", []) or []:
            kind = item[0]
            pts = []
            if kind == "l":
                pts = [(item[1], item[2])]
            elif kind == "re":
                r = item[1]
                pts = [(r.tl, r.tr), (r.bl, r.br), (r.tl, r.bl), (r.tr, r.br)]
            elif kind == "qu":
                q = item[1]
                pts = [(q.ul, q.ur), (q.ll, q.lr), (q.ul, q.ll), (q.ur, q.lr)]
            for a, b in pts:
                a, b = a * mat, b * mat
                dx, dy = abs(b.x - a.x), abs(b.y - a.y)
                if dy <= 0.02 * max(dx, 1e-6) and dx >= 0.5:
                    y = (a.y + b.y) / 2
                    hs.append(Segment(min(a.x, b.x), y, max(a.x, b.x), y))
                elif dx <= 0.02 * max(dy, 1e-6) and dy >= 0.5:
                    x = (a.x + b.x) / 2
                    vs.append(Segment(x, min(a.y, b.y), x, max(a.y, b.y)))
    # CAD exports often draw a table's column lines one row at a time, and a
    # BOM row can be shorter than the minimum line length (3.7 mm rows on a
    # real SolidWorks sheet). Join touching collinear pieces first, then apply
    # the length limit, so the column line survives and short geometry
    # (ticks, arrowheads) still doesn't.
    from .cells import merge_segments
    hs = [s for s in merge_segments(hs, True, gap=0.6, pos_tol=0.4) if s.x1 - s.x0 >= min_len_pt]
    vs = [s for s in merge_segments(vs, False, gap=0.6, pos_tol=0.4) if s.y1 - s.y0 >= min_len_pt]
    return hs, vs


def _load_vector_page(page, path: str, page_no: int, producer: str) -> Sheet:
    words = _vector_words(page)
    min_len = MIN_LINE_MM / 25.4 * 72
    hs, vs = _vector_segments(page, min_len)
    w, h = page.rect.width, page.rect.height

    # A text-layer sheet can still have been composed sideways on the page.
    rot = _dominant_rotation(words)
    if rot:
        for wd in words:
            wd.x0, wd.y0, wd.x1, wd.y1 = _rot_bbox(wd.bbox, w, h, rot)
            wd.dir = _rot_dir(*wd.dir, rot)
        new_h, new_v = [], []
        for s in hs + vs:
            x0, y0, x1, y1 = _rot_bbox((s.x0, s.y0, s.x1, s.y1), w, h, rot)
            seg = Segment(x0, y0, x1, y1)
            (new_h if seg.is_horizontal else new_v).append(seg)
        hs, vs = new_h, new_v
        w, h = _rotated_size(w, h, rot)

    return Sheet(source_file=path, page_number=page_no, width=w, height=h,
                 words=words, h_lines=hs, v_lines=vs, text_source="embedded",
                 rotation_deg=rot, producer=producer)


# --------------------------------------------------------------------------
# OCR path (no text layer, or images)
# --------------------------------------------------------------------------

def _ink_gray(rgb: np.ndarray) -> np.ndarray:
    """Darkest channel per pixel: green/blue/red ink becomes dark like black."""
    if rgb.ndim == 2:
        return rgb
    return rgb[:, :, :3].min(axis=2)


def _np_rotate_cw(img: np.ndarray, cw_deg: int) -> np.ndarray:
    k = (cw_deg // 90) % 4
    return np.ascontiguousarray(np.rot90(img, k=-k)) if k else img


def _tesseract():
    import pytesseract
    import os
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return pytesseract


def _keyword_score(text: str) -> int:
    up = text.upper()
    return sum(1 for k in ORIENTATION_KEYWORDS if k in up)


def find_upright_rotation(gray: np.ndarray) -> Tuple[int, int]:
    """Try each clockwise rotation, OCR the corner where a title block sits
    (bottom-right after rotation) and keep the rotation whose text contains
    the most title-block keywords. Tesseract's own orientation detector is
    unreliable on drawings (mostly lines, little text), so it's only used
    to decide which rotation to try first.

    Returns (cw_rotation_deg, keyword_score)."""
    from PIL import Image
    pt = _tesseract()

    order = [0, 90, 270, 180]
    try:
        osd = pt.image_to_osd(Image.fromarray(gray))
        m = re.search(r"Rotate:\s*(\d+)", osd)
        if m:
            hint = int(m.group(1)) % 360
            order = [hint] + [r for r in order if r != hint]
    except Exception:
        pass

    best_rot, best_score = 0, -1
    for rot in order:
        img = _np_rotate_cw(gray, rot)
        h, w = img.shape
        crop = img[int(h * 0.62):, int(w * 0.45):]
        # Downsample for speed; title-block labels are large enough.
        pil = Image.fromarray(crop)
        pil = pil.resize((max(1, pil.width // 2), max(1, pil.height // 2)))
        try:
            txt = pt.image_to_string(pil, config="--psm 11")
        except Exception:
            txt = ""
        score = _keyword_score(txt)
        if score > best_score:
            best_rot, best_score = rot, score
        if score >= 6:
            break
    return best_rot, best_score


def _raster_lines(gray: np.ndarray, px_per_pt: float):
    """Find horizontal/vertical ruling lines with morphological opening.
    Returns (h_segments_px, v_segments_px, line_mask)."""
    import cv2

    ink = (gray < 170).astype(np.uint8) * 255
    k = max(15, int(MIN_LINE_MM / 25.4 * 72 * px_per_pt))
    h_mask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    v_mask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))

    def comps(mask, horizontal):
        segs = []
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            x, y, w, h, _ = stats[i]
            if horizontal and w >= k:
                segs.append((x, y + h / 2, x + w, y + h / 2, h))
            elif not horizontal and h >= k:
                segs.append((x + w / 2, y, x + w / 2, y + h, w))
        return segs

    line_mask = cv2.dilate(cv2.bitwise_or(h_mask, v_mask), np.ones((3, 3), np.uint8))
    return comps(h_mask, True), comps(v_mask, False), line_mask


def _ocr_words(pil_img, pt, min_conf: float, psm: int = 11):
    data = pt.image_to_data(pil_img, config=f"--psm {psm}", output_type=pt.Output.DICT)
    out = []
    for i in range(len(data["text"])):
        t = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if not t or conf < min_conf:
            continue
        # Pure punctuation/noise from hatching and leader lines.
        if not re.search(r"[A-Za-z0-9]", t):
            continue
        out.append((t, data["left"][i], data["top"][i], data["width"][i], data["height"][i], conf))
    return out


def _overlap_frac(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))
    return ix * iy / area


def sheet_from_gray(gray: np.ndarray, path: str, page_no: int, dpi: int,
                    producer: str = "", vertical_text: bool = True,
                    rotation: Optional[int] = None) -> Sheet:
    """OCR a single page raster into a Sheet (see module docstring)."""
    from PIL import Image
    pt = _tesseract()
    warnings: List[str] = []

    if rotation is None:
        rotation, score = find_upright_rotation(gray)
        if score < 2:
            warnings.append(
                f"Could not confirm which way up page {page_no} is (only {score} title-block "
                "keywords read in any orientation); results for this page may be unreliable.")
    up = _np_rotate_cw(gray, rotation)
    px_per_pt = dpi / 72.0
    H, W = up.shape

    h_px, v_px, line_mask = _raster_lines(up, px_per_pt)
    clean = up.copy()
    clean[line_mask > 0] = 255

    words: List[Word] = []
    for t, l, tp, w, h, conf in _ocr_words(Image.fromarray(clean), pt, min_conf=30):
        words.append(Word(t, l / px_per_pt, tp / px_per_pt, (l + w) / px_per_pt,
                          (tp + h) / px_per_pt, (1.0, 0.0), conf))

    if vertical_text:
        # Text reading bottom-to-top is upright after rotating the image 90deg
        # clockwise; top-to-bottom after 270deg. Map boxes back to the upright
        # frame and keep only confident words that don't duplicate a
        # horizontal-pass word.
        for cw, text_dir in ((90, (0.0, -1.0)), (270, (0.0, 1.0))):
            rimg = _np_rotate_cw(clean, cw)
            rh, rw = rimg.shape
            for t, l, tp, w, h, conf in _ocr_words(Image.fromarray(rimg), pt, min_conf=55):
                if len(t) < 2:
                    continue
                # inverse rotation back into the upright frame
                bx = _rot_bbox((l, tp, l + w, tp + h), rw, rh, (360 - cw) % 360)
                bb = tuple(v / px_per_pt for v in bx)
                if any(_overlap_frac(bb, x.bbox) > 0.3 for x in words):
                    continue
                words.append(Word(t, *bb, dir=text_dir, conf=conf))

    # Drop "lines" that are really strokes of large lettering.
    def inside_word(x0, y0, x1, y1):
        box = (x0 / px_per_pt, y0 / px_per_pt, x1 / px_per_pt, y1 / px_per_pt)
        return any(_overlap_frac(box, w.bbox) > 0.8 and w.conf > 60 for w in words)

    hs = [Segment(a / px_per_pt, b / px_per_pt, c / px_per_pt, d / px_per_pt)
          for a, b, c, d, _ in h_px if not inside_word(a, b - 1, c, d + 1)]
    vs = [Segment(a / px_per_pt, b / px_per_pt, c / px_per_pt, d / px_per_pt)
          for a, b, c, d, _ in v_px if not inside_word(a - 1, b, c + 1, d)]

    return Sheet(source_file=path, page_number=page_no, width=W / px_per_pt, height=H / px_per_pt,
                 words=words, h_lines=hs, v_lines=vs, text_source="ocr",
                 rotation_deg=rotation, producer=producer, raster=clean,
                 px_per_pt=px_per_pt, warnings=warnings)


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def load_sheets(path, pages: Optional[Iterable[int]] = None, force_ocr: bool = False,
                vertical_text: bool = True, dpi: int = OCR_DPI) -> List[Sheet]:
    """Load every page (or the given 0-based `pages`) of a PDF, or a single
    image, as upright Sheets."""
    path = str(path)
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        from PIL import Image
        rgb = np.array(Image.open(path).convert("RGB"))
        dpi_img = dpi
        try:
            dpi_img = int(Image.open(path).info.get("dpi", (dpi, dpi))[0]) or dpi
        except Exception:
            pass
        return [sheet_from_gray(_ink_gray(rgb), path, 1, dpi_img, vertical_text=vertical_text)]
    if suffix != ".pdf":
        raise ValueError(f"Unsupported file type '{suffix}': use a PDF or an image.")

    import pymupdf
    doc = pymupdf.open(path)
    meta = doc.metadata or {}
    producer = " / ".join(v for v in (meta.get("creator"), meta.get("producer")) if v)
    sheets: List[Sheet] = []
    page_ids = list(pages) if pages is not None else range(len(doc))
    for i in page_ids:
        page = doc[i]
        n_words = len(page.get_text("words"))
        if n_words >= MIN_TEXT_WORDS and not force_ocr:
            sheets.append(_load_vector_page(page, path, i + 1, producer))
            continue
        pix = page.get_pixmap(dpi=dpi)
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        sheets.append(sheet_from_gray(_ink_gray(rgb), path, i + 1, dpi, producer,
                                      vertical_text=vertical_text))
    doc.close()
    return sheets
