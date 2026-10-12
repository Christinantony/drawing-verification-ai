"""
Plain data types shared by every tool in the repository.

All coordinates are in "upright sheet points": 72 points per inch, origin at
the top-left corner of the sheet as a person would read it, y growing
downward. A sheet that was printed sideways onto a portrait page is rotated
into this frame by the loader, so nothing downstream ever deals with page
rotation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

BBox = Tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    # Reading direction as a unit vector in upright-sheet coordinates:
    # (1, 0) normal text, (0, -1) text reading bottom-to-top, (0, 1) top-to-bottom.
    dir: Tuple[float, float] = (1.0, 0.0)
    conf: float = 100.0  # OCR confidence 0-100; 100 for embedded PDF text

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def bbox(self) -> BBox:
        return (self.x0, self.y0, self.x1, self.y1)

    # pdfplumber-style aliases, so older code that reads .top/.bottom works.
    @property
    def top(self) -> float:
        return self.y0

    @property
    def bottom(self) -> float:
        return self.y1

    @property
    def height(self) -> float:
        """Glyph height measured across the text line (works for vertical text)."""
        return (self.y1 - self.y0) if self.horizontal else (self.x1 - self.x0)

    @property
    def horizontal(self) -> bool:
        return abs(self.dir[0]) >= abs(self.dir[1])

    def frame(self, x: float, y: float) -> Tuple[float, float]:
        """Project a point into this word's own reading frame:
        u = distance along the reading direction, v = distance "down" the
        text (toward the baseline side). Lets tolerance/label logic treat
        vertical text exactly like horizontal text."""
        dx, dy = self.dir
        nx, ny = -dy, dx  # rotate reading direction +90deg in screen coords = "down"
        return (x * dx + y * dy, x * nx + y * ny)


@dataclass
class Segment:
    """A straight ruling line, axis-aligned (diagonals are discarded)."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def is_horizontal(self) -> bool:
        return abs(self.y1 - self.y0) <= abs(self.x1 - self.x0)

    @property
    def length(self) -> float:
        return max(abs(self.x1 - self.x0), abs(self.y1 - self.y0))


@dataclass
class Cell:
    """A ruled box and the words found inside it."""
    bbox: BBox
    words: List[Word] = field(default_factory=list)
    text: str = ""

    @property
    def x0(self): return self.bbox[0]
    @property
    def y0(self): return self.bbox[1]
    @property
    def x1(self): return self.bbox[2]
    @property
    def y1(self): return self.bbox[3]
    @property
    def xc(self): return (self.bbox[0] + self.bbox[2]) / 2
    @property
    def yc(self): return (self.bbox[1] + self.bbox[3]) / 2
    @property
    def width(self): return self.bbox[2] - self.bbox[0]
    @property
    def height(self): return self.bbox[3] - self.bbox[1]


@dataclass
class Sheet:
    source_file: str
    page_number: int           # 1-based
    width: float               # upright sheet width, points
    height: float
    words: List[Word]
    h_lines: List[Segment]
    v_lines: List[Segment]
    text_source: str           # "embedded" (PDF text layer) | "ocr"
    rotation_deg: int = 0      # clockwise rotation applied to make the sheet upright
    producer: str = ""         # PDF producer/creator, e.g. "SOLIDWORKS PDF Publisher"
    # OCR-only: the upright, line-cleaned raster and its pixels-per-point,
    # kept so table/title-block cells can be re-read one at a time.
    raster: Optional[object] = None
    px_per_pt: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @property
    def orientation(self) -> str:
        return "landscape" if self.width > self.height else "portrait"

    @property
    def paper_size(self) -> Optional[str]:
        """ISO A-series size from the sheet dimensions (+/-3%), else None."""
        mm = sorted((self.width / 72 * 25.4, self.height / 72 * 25.4))
        sizes = {"A0": (841, 1189), "A1": (594, 841), "A2": (420, 594),
                 "A3": (297, 420), "A4": (210, 297)}
        for name, (a, b) in sizes.items():
            if abs(mm[0] - a) / a < 0.03 and abs(mm[1] - b) / b < 0.03:
                return name
        return None
