"""
extraction.py
Step 1 of the pipeline: pull raw text, per-page tables, page images, and
title-block CELLS out of a drawing PDF or a scanned drawing image. This is
the ONLY module that touches PDF/image libraries - structuring.py works
purely on the plain Python data this module returns.

- Vector/text-based PDFs (typical AutoCAD/SolidWorks/Inventor exports): we
  read the embedded text directly via PyMuPDF - full page AND the
  title-block corner (via a clipped text query). No OCR runs at all for
  these pages; OCR is strictly a fallback for when there's no embedded text
  to read.
- Scanned/rasterized PDFs or plain images (PNG/JPG/TIFF scans of paper
  drawings): we fall back to Tesseract OCR. Because a scan's physical
  orientation isn't declared anywhere (unlike a vector PDF's /Rotate), we
  first run Tesseract's orientation-and-script detection (OSD) to catch
  pages fed in sideways or upside down and rotate them upright, then apply
  a light OpenCV cleanup pass (grayscale, Otsu threshold, sub-degree
  deskew) before the actual OCR pass.
- Title-block CELLS: many title blocks draw each field in its own bordered
  box. Rather than only reading a blob of text out of the title-block
  corner, we isolate each individual box and read just what's inside it:
    * vector PDFs - PyMuPDF's get_drawings() gives every rectangle actually
      drawn on the page; we keep the ones inside the title-block corner
      (excluding the one big outer frame) and pull the embedded text under
      each one directly - still zero OCR.
    * scanned pages/images - OpenCV contour detection finds the same boxes
      in the cropped title-block region, and each one is OCR'd individually.
  structuring.py then classifies each cell's text against the known field
  labels (see classify_title_block_cells there). If no usable cells are
  found either way, the caller falls back to the whole-region text blob.
- Tables: pdfplumber is used to find ruled tables (the BOM is usually one)
  on text-based PDF pages, in addition to the plain text dump.
- Orientation (landscape vs portrait) is never assumed either way: page
  size is read per-page/per-image and every region crop is computed as a
  fraction of that page's actual (corrected) width/height, so a D-size
  landscape sheet and an A-size portrait sheet are both handled correctly
  without special-casing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

import config

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

BBox = Tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass
class TitleBlockCell:
    """One bordered box from the title block, with the text found inside
    it. `bbox` is only ever compared against other cells from the SAME
    page/source: PDF points for vector-derived cells, pixels relative to
    the cropped title-block image for OCR-derived cells."""
    bbox: BBox
    text: str


@dataclass
class PageData:
    page_number: int
    text: str
    source: str  # "embedded" | "ocr"
    image: Optional[Image.Image] = None
    tables: List[List[List[str]]] = field(default_factory=list)
    region_text: str = ""                                  # embedded text under the title-block corner; "" if source != "embedded"
    title_block_cells: List[TitleBlockCell] = field(default_factory=list)
    orientation: str = "portrait"                          # "portrait" | "landscape", of the final (corrected) page
    rotation_applied_deg: int = 0                           # OSD correction applied before OCR; 0 for embedded/vector pages


@dataclass
class DocumentData:
    file_path: Path
    pages: List["PageData"]

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)


def _configure_tesseract():
    import pytesseract
    if config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
    return pytesseract


def correct_orientation(pil_image: Image.Image) -> Tuple[Image.Image, int]:
    """Scanned/photographed drawings are frequently fed in sideways or
    upside down, and (unlike a vector PDF's declared /Rotate) there's no
    metadata to trust here. Tesseract's orientation-and-script-detection
    (OSD) samples the text to estimate how many degrees clockwise the image
    needs to rotate to read upright, so we can correct it BEFORE running
    full-page OCR or cropping a title-block corner out of it - cropping a
    sideways page would grab the wrong corner entirely.

    Returns (corrected_image, angle_applied). angle_applied is 0 when OSD
    is confident the page is already upright, or when OSD can't get a
    reading (e.g. very sparse text) - we don't guess in that case.
    """
    pytesseract = _configure_tesseract()
    angle = 0
    try:
        osd = pytesseract.image_to_osd(pil_image)
        m = re.search(r"Rotate:\s*(\d+)", osd)
        angle = int(m.group(1)) if m else 0
    except Exception:
        angle = 0
    if angle:
        pil_image = pil_image.convert("RGB").rotate(-angle, expand=True, fillcolor="white")
    return pil_image, angle


def preprocess_for_ocr(pil_image: Image.Image) -> Image.Image:
    """Grayscale + Otsu threshold + sub-degree deskew. This is a FINE
    correction for a slightly crooked scan; the coarse 90/180/270 rotation
    (a page fed in sideways) is handled separately by correct_orientation(),
    which should already have run by the time an image reaches here."""
    import cv2

    img = np.array(pil_image.convert("L"))
    _, thresh = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    coords = np.column_stack(np.where(thresh < 255))
    angle = 0.0
    if coords.size:
        rect = cv2.minAreaRect(coords)
        angle = rect[-1]
        angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) > 0.3:
        h, w = thresh.shape
        m = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
        thresh = cv2.warpAffine(thresh, m, (w, h), flags=cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(thresh)


def ocr_image(pil_image: Image.Image) -> str:
    pytesseract = _configure_tesseract()
    cleaned = preprocess_for_ocr(pil_image)
    return pytesseract.image_to_string(cleaned)


def _page_orientation(width: float, height: float) -> str:
    return "landscape" if width > height else "portrait"


def crop_region(pil_image: Image.Image, corner: str = "bottom-right",
                 width_frac: Optional[float] = None,
                 height_frac: Optional[float] = None) -> Image.Image:
    """Crop a corner of the (already correctly-oriented) page image - used
    to give OCR a focused, high signal-to-noise shot at just the title
    block instead of the whole (often crowded) sheet. Fractions are of
    whatever this image's actual width/height are, so this works the same
    for a landscape or a portrait sheet."""
    width_frac = width_frac if width_frac is not None else config.TITLE_BLOCK_REGION["width_frac"]
    height_frac = height_frac if height_frac is not None else config.TITLE_BLOCK_REGION["height_frac"]
    w, h = pil_image.size
    cw, ch = int(w * width_frac), int(h * height_frac)
    boxes = {
        "bottom-right": (w - cw, h - ch, w, h),
        "bottom-left": (0, h - ch, cw, h),
        "top-right": (w - cw, 0, w, ch),
        "top-left": (0, 0, cw, ch),
    }
    return pil_image.crop(boxes[corner])


def _pdf_region_rect(page, corner: str, width_frac: float, height_frac: float):
    """Same corner-fraction logic as crop_region(), but in PDF point-space
    (via page.rect) so we can ask PyMuPDF for the embedded text/geometry
    under that rectangle directly - no OCR involved.

    page.rect already reflects the page's declared /Rotate: PyMuPDF applies
    rotation automatically to both get_text() and get_pixmap(), so a
    landscape-authored D-size sheet and a portrait A-size sheet both come
    out already right-side-up, and this rectangle lines up with
    crop_region() on the rendered image either way. There is nothing to
    manually rotate for a vector/text PDF - only scanned pages (see
    correct_orientation()) have an unknown physical orientation.
    """
    import fitz
    x0, y0, x1, y1 = page.rect
    cw, ch = (x1 - x0) * width_frac, (y1 - y0) * height_frac
    boxes = {
        "bottom-right": fitz.Rect(x1 - cw, y1 - ch, x1, y1),
        "bottom-left": fitz.Rect(x0, y1 - ch, x0 + cw, y1),
        "top-right": fitz.Rect(x1 - cw, y0, x1, y0 + ch),
        "top-left": fitz.Rect(x0, y0, x0 + cw, y0 + ch),
    }
    return boxes[corner]


def _rects_overlap(a, b) -> bool:
    return not (a.x1 <= b.x0 or a.x0 >= b.x1 or a.y1 <= b.y0 or a.y0 >= b.y1)


def _rects_close(a, b, tol: float = 2.0) -> bool:
    return (abs(a.x0 - b.x0) < tol and abs(a.y0 - b.y0) < tol and
            abs(a.x1 - b.x1) < tol and abs(a.y1 - b.y1) < tol)


def extract_title_block_cells_vector(page, region_rect) -> List[TitleBlockCell]:
    """For vector/text PDFs: find each bordered cell of the title block by
    reading the page's vector drawing objects (the CAD software's actual
    rectangle/box entities), then pull the embedded text under each one -
    no OCR. Returns [] if the page has no usable vector rectangles there
    (e.g. a title block built from freeform lines rather than closed
    boxes) - the caller then falls back to the whole-region text blob."""
    import fitz

    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    region_area = region_rect.width * region_rect.height
    if region_area <= 0:
        return []

    candidates: List["fitz.Rect"] = []
    for d in drawings:
        # A CAD export frequently batches several cell borders under one
        # graphics state, so the PATH's overall bounding rect can span
        # multiple cells - decompose into the individual `re` (explicit
        # rectangle) primitives within it whenever present, and only fall
        # back to the whole path's bounding rect when it wasn't built from
        # those (e.g. a box drawn as four separate line segments).
        found_rect_item = False
        for item in d.get("items", []) or []:
            try:
                if item and item[0] == "re":
                    candidates.append(fitz.Rect(item[1]))
                    found_rect_item = True
            except Exception:
                continue
        if not found_rect_item:
            rect = d.get("rect")
            if rect is not None:
                try:
                    candidates.append(fitz.Rect(rect))
                except Exception:
                    pass

    filtered: List["fitz.Rect"] = []
    for r in candidates:
        if not _rects_overlap(r, region_rect):
            continue
        area = r.width * r.height
        if area < config.MIN_CELL_AREA_FRAC * region_area or area > config.MAX_CELL_AREA_FRAC * region_area:
            continue
        filtered.append(r)

    # De-duplicate near-identical rectangles (borders are sometimes drawn twice).
    unique: List["fitz.Rect"] = []
    for r in filtered:
        if not any(_rects_close(r, u) for u in unique):
            unique.append(r)

    cells = []
    for r in unique:
        text = page.get_text(clip=r).strip()
        if text:
            cells.append(TitleBlockCell(bbox=(r.x0, r.y0, r.x1, r.y1), text=text))
    return cells


def extract_title_block_cells_raster(pil_crop: Image.Image) -> List[TitleBlockCell]:
    """For scanned pages/images: detect the bordered cells of the title
    block via OpenCV contour detection on the (already
    orientation-corrected) cropped region, then OCR each cell individually.
    Returns [] if no plausible rectangular cells are found - the caller
    then OCRs the whole crop as one blob instead."""
    import cv2

    img = np.array(pil_crop.convert("L"))
    _, thresh = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    h_img, w_img = thresh.shape
    region_area = h_img * w_img
    if region_area <= 0:
        return []

    boxes: List[Tuple[int, int, int, int]] = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = w * h
        if area < config.MIN_CELL_AREA_FRAC * region_area or area > config.MAX_CELL_AREA_FRAC * region_area:
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if not (4 <= len(approx) <= 6):
            continue  # keep roughly rectangular shapes only
        boxes.append((x, y, x + w, y + h))

    unique: List[Tuple[int, int, int, int]] = []
    for b in boxes:
        if not any(all(abs(b[i] - u[i]) < 3 for i in range(4)) for u in unique):
            unique.append(b)

    pytesseract = _configure_tesseract()
    cells = []
    for (x0, y0, x1, y1) in unique:
        crop = pil_crop.crop((x0, y0, x1, y1))
        text = pytesseract.image_to_string(crop).strip()
        if text:
            cells.append(TitleBlockCell(bbox=(x0, y0, x1, y1), text=text))
    return cells


def load_pdf(path: Path) -> DocumentData:
    import fitz  # PyMuPDF
    import pdfplumber

    pages: List[PageData] = []
    doc = fitz.open(path)
    plumber_doc = pdfplumber.open(path)

    for i, page in enumerate(doc):
        orientation = _page_orientation(page.rect.width, page.rect.height)
        embedded_text = page.get_text().strip()

        try:
            raw_tables = plumber_doc.pages[i].extract_tables()
            tables = [t for t in raw_tables if t]
        except Exception:
            tables = []

        region_text = ""
        rotation_applied = 0
        cells: List[TitleBlockCell] = []

        region_rect = _pdf_region_rect(
            page, "bottom-right",
            config.TITLE_BLOCK_REGION["width_frac"],
            config.TITLE_BLOCK_REGION["height_frac"],
        )

        if len(embedded_text) >= config.MIN_EMBEDDED_CHARS:
            # Real text is available straight from the PDF - skip OCR
            # entirely, for the full page, the title-block region, AND the
            # per-cell breakdown.
            text, source = embedded_text, "embedded"
            region_text = page.get_text(clip=region_rect).strip()
            if config.USE_TITLE_BLOCK_CELLS:
                cells = extract_title_block_cells_vector(page, region_rect)
        else:
            text, source = "", "ocr"

        # Still rendered even for embedded-text pages: structuring.py keeps
        # this as a fallback in case a title block is a rasterized
        # logo/stamp within an otherwise vector page.
        pix = page.get_pixmap(dpi=config.OCR_DPI)
        pil_image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        if source == "ocr":
            # Unlike a vector page's /Rotate, a scanned page's physical scan
            # angle is unknown - detect and correct it before OCR-ing
            # anything, or a sideways/upside-down page both OCRs badly and
            # gets its title block cropped from the wrong corner.
            pil_image, rotation_applied = correct_orientation(pil_image)
            orientation = _page_orientation(pil_image.width, pil_image.height)
            text = ocr_image(pil_image)
            if config.USE_TITLE_BLOCK_CELLS:
                crop = crop_region(pil_image, corner="bottom-right")
                cells = extract_title_block_cells_raster(crop)

        pages.append(PageData(page_number=i + 1, text=text, source=source,
                               image=pil_image, tables=tables, region_text=region_text,
                               title_block_cells=cells, orientation=orientation,
                               rotation_applied_deg=rotation_applied))

    plumber_doc.close()
    doc.close()
    return DocumentData(file_path=path, pages=pages)


def load_image(path: Path) -> DocumentData:
    pil_image = Image.open(path)
    # A standalone scanned/photographed image has no rotation metadata at
    # all - always run OSD correction before anything else.
    pil_image, rotation_applied = correct_orientation(pil_image)
    orientation = _page_orientation(pil_image.width, pil_image.height)
    text = ocr_image(pil_image)
    cells: List[TitleBlockCell] = []
    if config.USE_TITLE_BLOCK_CELLS:
        crop = crop_region(pil_image, corner="bottom-right")
        cells = extract_title_block_cells_raster(crop)
    page = PageData(page_number=1, text=text, source="ocr", image=pil_image, tables=[],
                     title_block_cells=cells, orientation=orientation,
                     rotation_applied_deg=rotation_applied)
    return DocumentData(file_path=path, pages=[page])


def load_document(path: Path) -> DocumentData:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return load_pdf(path)
    if suffix in IMAGE_SUFFIXES:
        return load_image(path)
    raise ValueError(
        f"Unsupported file type: '{suffix}'. Use a PDF or an image "
        f"({', '.join(sorted(IMAGE_SUFFIXES))})."
    )
