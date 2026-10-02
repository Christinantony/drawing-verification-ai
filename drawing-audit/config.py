"""
config.py
Central configuration for the offline drawing-verification pipeline.
Tweak the values here rather than hunting through the other modules -
especially TITLE_BLOCK_FIELDS / BOM_HEADER_ALIASES / TITLE_BLOCK_REGION,
which you'll likely want to adjust to match your company's drawing template.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Ollama / model settings
# ---------------------------------------------------------------------------
OLLAMA_HOST = "http://localhost:11434"
MODEL_NAME = "gemma4:e4b"          # ollama pull gemma4:e4b
USE_THINKING = True                 # native <|think|> reasoning, see README
MODEL_OPTIONS = {                   # Google/Ollama's recommended sampling config
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 64,
}

# ---------------------------------------------------------------------------
# OCR settings
# ---------------------------------------------------------------------------
# Only needed if Tesseract isn't already on PATH. Typical Windows location
# after installing the UB-Mannheim build:
# TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_CMD = None
OCR_DPI = 300                       # render resolution for scanned/rasterized pages
MIN_EMBEDDED_CHARS = 40             # below this we assume the PDF page is a scanned image

# ---------------------------------------------------------------------------
# Title block heuristics
# ---------------------------------------------------------------------------
# Fraction of the page (measured from a given corner) treated as the title
# block when we crop the page for focused OCR. Most ANSI/ISO templates put
# it bottom-right; tune this if yours differs.
TITLE_BLOCK_REGION = {"width_frac": 0.40, "height_frac": 0.22}

TITLE_BLOCK_FIELDS = {
    # canonical_name: [regex label alternatives, first match wins]
    "drawing_number": [r"DWG\.?\s*NO\.?", r"DRAWING\s*(?:NO\.?|NUMBER)", r"DOCUMENT\s*NO\.?"],
    "title": [r"TITLE", r"PART\s*NAME", r"DESCRIPTION\s*OF\s*PART"],
    "revision": [r"REV(?:ISION)?\.?\s*(?:NO\.?)?"],
    "scale": [r"SCALE"],
    "date": [r"DATE"],
    "drawn_by": [r"DRAWN\s*(?:BY)?", r"DRAFTER"],
    "checked_by": [r"CHK'?D?\.?\s*(?:BY)?", r"CHECKED\s*(?:BY)?"],
    "approved_by": [r"APPR(?:OVED)?\.?\s*(?:BY)?"],
    "material": [r"MATERIAL", r"MATL\.?"],
    "sheet": [r"SHEET", r"SHT\.?"],
    "size": [r"\bSIZE\b"],
    "finish": [r"FINISH"],
    "tolerance": [r"TOLERANCE", r"UNLESS\s*OTHERWISE\s*SPECIFIED"],
    "project": [r"PROJECT", r"CUSTOMER"],
}

# ---------------------------------------------------------------------------
# Title-block cell detection (rectangle-per-field layouts)
# ---------------------------------------------------------------------------
# If your title block draws each field in its own bordered box (typical of
# CAD-exported title blocks), the pipeline isolates each box and reads its
# text individually instead of relying purely on regex over the whole
# title-block text blob. Cell results are still backfilled by the regex
# pass for any field a cell didn't capture. Set to False to disable and use
# the regex-only approach everywhere.
USE_TITLE_BLOCK_CELLS = True

# A candidate box smaller than this fraction of the title-block region's
# area is treated as noise (stray marks, dimension ticks) and ignored;
# larger than MAX_CELL_AREA_FRAC is treated as the outer frame, not a field.
MIN_CELL_AREA_FRAC = 0.002
MAX_CELL_AREA_FRAC = 0.60

# ---------------------------------------------------------------------------
# Bill of Materials heuristics
# ---------------------------------------------------------------------------
BOM_HEADER_ALIASES = {
    "item": ["item", "no", "no.", "#", "itm", "find no", "find no."],
    "part_number": ["part number", "part no", "p/n", "pn", "part#", "part no."],
    "description": ["description", "desc", "part name", "nomenclature"],
    "qty": ["qty", "quantity", "qty.", "qty required", "qty req'd"],
    "material": ["material", "matl", "matl."],
    "remarks": ["remarks", "notes", "comment", "comments"],
}
BOM_TABLE_KEYWORDS = ["BILL OF MATERIAL", "BILL OF MATERIALS", "BOM", "PARTS LIST"]

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = Path("reports")
