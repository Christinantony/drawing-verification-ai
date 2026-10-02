"""
Extract word-level tokens (text + bounding box) from a single drawing PDF page,
using the native text layer when present, falling back to OCR when it isn't.

All coordinates are returned in PDF points, in "top-down" convention:
    top    = distance from the top edge of the page to the top of the glyph
    bottom = distance from the top edge of the page to the bottom of the glyph
    x0/x1  = left/right edge, distance from the left edge of the page

This matches pdfplumber's convention, which the rest of the pipeline assumes.
"""
from __future__ import annotations

import subprocess
import shutil
import tempfile
import os
import re
from dataclasses import dataclass
from typing import List

import pdfplumber


@dataclass
class Token:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class PageLayout:
    page_width: float
    page_height: float
    tokens: List[Token]
    source: str  # "text" or "ocr"


def page_has_text_layer(pdf_path: str, page_number: int = 0) -> bool:
    """
    Cheap check: does this page have an extractable text layer, or is it a
    raster scan? Mirrors the pdffonts-based check recommended for reading
    arbitrary PDFs, but scoped to pdfplumber so we don't need a second tool
    just to decide.
    """
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number]
        words = page.extract_words()
        # A handful of stray glyphs (e.g. a scanned page with a digital stamp)
        # shouldn't count as "has a text layer" for a drawing sheet.
        return len(words) >= 5


def extract_tokens_native(pdf_path: str, page_number: int = 0) -> PageLayout:
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number]
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        tokens = [
            Token(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"])
            for w in words
        ]
        return PageLayout(page_width=page.width, page_height=page.height, tokens=tokens, source="text")


def extract_tokens_ocr(pdf_path: str, page_number: int = 0, dpi: int = 300) -> PageLayout:
    """
    Rasterize the page with poppler's pdftoppm, then OCR it with tesseract's
    word-level bounding-box output. Pixel coordinates are converted back to
    PDF points (72 pt/inch) so this PageLayout is interchangeable with the
    native-text one.
    """
    import pytesseract
    from PIL import Image

    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm (poppler-utils) is required for OCR fallback but was not found.")

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "page")
        subprocess.run(
            [
                "pdftoppm", "-png", "-r", str(dpi),
                "-f", str(page_number + 1), "-l", str(page_number + 1),
                pdf_path, prefix,
            ],
            check=True, capture_output=True,
        )
        png_files = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
        if not png_files:
            raise RuntimeError("pdftoppm produced no output image for OCR.")
        img_path = os.path.join(tmp, png_files[0])
        img = Image.open(img_path)
        px_w, px_h = img.size

        # Also get the true page size in points, so the pixel->point scale is exact
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[page_number]
            page_width, page_height = page.width, page.height

        scale_x = page_width / px_w
        scale_y = page_height / px_h

        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        tokens = []
        n = len(data["text"])
        for i in range(n):
            text = data["text"][i].strip()
            conf = float(data["conf"][i]) if data["conf"][i] not in ("-1", "") else -1
            if not text or conf < 0:
                continue
            left, top, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            tokens.append(Token(
                text=text,
                x0=left * scale_x,
                x1=(left + w) * scale_x,
                top=top * scale_y,
                bottom=(top + h) * scale_y,
            ))
        return PageLayout(page_width=page_width, page_height=page_height, tokens=tokens, source="ocr")


def load_page_layout(pdf_path: str, page_number: int = 0, force_ocr: bool = False, ocr_dpi: int = 300) -> PageLayout:
    """
    Main entry point: pick native-text extraction when a text layer exists,
    OCR otherwise (or when force_ocr=True, e.g. for a drawing that has a thin,
    unreliable text layer such as a re-scanned/printed-and-rescanned PDF).
    """
    if not force_ocr and page_has_text_layer(pdf_path, page_number):
        return extract_tokens_native(pdf_path, page_number)
    return extract_tokens_ocr(pdf_path, page_number, dpi=ocr_dpi)


def flat_text(layout: PageLayout, line_tolerance: float = 4.0) -> str:
    """Tokens joined in reading order, space-separated -- good enough for
    regex-based lookups (general tolerance table, etc.) where exact layout
    doesn't matter.

    Groups tokens into text lines by clustering on `top`, rather than
    dividing into fixed-size buckets: two tokens on the same visual line can
    still land 0.5-1pt apart in `top` (rounding/kerning), and a fixed bucket
    boundary can slice right between them, scattering a label from its
    value across the sorted output. A running-cluster pass avoids that.
    """
    toks = sorted(layout.tokens, key=lambda t: t.top)
    lines: List[List[Token]] = []
    current: List[Token] = []
    cluster_top = None
    for t in toks:
        if cluster_top is None or abs(t.top - cluster_top) <= line_tolerance:
            current.append(t)
            cluster_top = t.top if cluster_top is None else (cluster_top + t.top) / 2
        else:
            lines.append(current)
            current = [t]
            cluster_top = t.top
    if current:
        lines.append(current)

    out = []
    for line in lines:
        line.sort(key=lambda t: t.x0)
        out.append(" ".join(t.text for t in line))
    return " ".join(out)
