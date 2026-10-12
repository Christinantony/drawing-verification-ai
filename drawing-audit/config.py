"""
Settings for drawing-audit. Reading the sheet itself (text layer vs OCR,
title-block labels, table headers) lives in the shared sheetreader package;
this file holds what's specific to the audit.
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# Ollama / model
# ---------------------------------------------------------------------------
OLLAMA_HOST = "http://localhost:11434"
MODEL_NAME = "gemma4:e4b"          # ollama pull gemma4:e4b

# Language check (default model use): short prompt, thinking off, small
# answer. At ~7 tokens/s this is the only model call in a normal run.
LANGUAGE_OPTIONS = {"temperature": 0.2, "num_predict": 400}

# --deep: the original full-drawing audit with Gemma's <|think|> reasoning.
# Much slower (thinking alone is typically 1,500-3,000 tokens).
USE_THINKING = True
MODEL_OPTIONS = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}

# ---------------------------------------------------------------------------
# OCR (only used for PDFs without a text layer, and for images)
# ---------------------------------------------------------------------------
# Only needed if Tesseract isn't on PATH. Typical Windows location after
# installing the UB-Mannheim build:
# TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESSERACT_CMD = None

# ---------------------------------------------------------------------------
# Template wording
# ---------------------------------------------------------------------------
# Extra title-block label wording, merged into sheetreader's defaults:
#   {"drawing_number": [r"PART\s*CODE"], "drawn_by": [r"PREPARED\s*BY"]}
EXTRA_TITLE_BLOCK_LABELS = {}

# Words the spell check should accept (trade names, in-house terms).
EXTRA_ENGINEERING_WORDS = []

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = Path("reports")
