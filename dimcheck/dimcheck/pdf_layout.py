"""
Word-level layout of one drawing page, via the shared sheetreader package.

sheetreader replaces the old pdfplumber/pdftoppm reader here. The difference
that matters for dimcheck: every word keeps its reading direction, so a
vertical dimension like "20.5" comes back as "20.5" (pdfplumber returned
"5.02"), and its tolerance figures can be found "after" and "above" it in
its own frame. It also handles AutoCAD sheets with no text layer that were
printed sideways (OCR after finding which way is up).

Coordinates: upright sheet points, origin top-left, y down. Words expose
.top/.bottom/.xc/.yc like the old Token type, so zoning.py is unchanged.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sheetreader import Sheet, Word, load_sheets  # noqa: E402

Token = Word  # backwards-compatible name


@dataclass
class PageLayout:
    page_width: float
    page_height: float
    tokens: List[Word]
    source: str            # "embedded" | "ocr"
    sheet: Optional[Sheet] = None


def load_page_layout(pdf_path: str, page_number: int = 0, force_ocr: bool = False,
                     ocr_dpi: int = 300) -> PageLayout:
    sheet = load_sheets(pdf_path, pages=[page_number], force_ocr=force_ocr, dpi=ocr_dpi)[0]
    return PageLayout(page_width=sheet.width, page_height=sheet.height, tokens=sheet.words,
                      source=sheet.text_source, sheet=sheet)


def flat_text(layout: PageLayout, line_tolerance: float = 4.0) -> str:
    """Words in reading order, space-separated (for regex lookups)."""
    from sheetreader.cells import reading_order
    return " ".join(" ".join(w.text for w in line) for line in reading_order(layout.tokens))
