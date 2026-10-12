# drawing-audit

Checks one drawing, or every drawing in a folder, and writes a Markdown report per run. Works on PDFs with a text layer (SolidWorks and most CAD "Save as PDF" exports), on PDFs without one (AutoCAD plotted through *Microsoft Print to PDF*, scans), and on images. Everything runs offline.

```powershell
python main.py path\to\drawing.pdf
python main.py path\to\folder --json          # all PDFs/images in the folder, plus a JSON dump
python main.py drawing.pdf --no-model         # rules + spelling lists only, Ollama not needed
python main.py drawing.pdf --deep             # also the full reasoning audit (slow on CPU)
python main.py drawing.pdf --force-ocr        # ignore a thin or unreliable text layer
```

## What happens to each sheet

1. **Read** with [`sheetreader`](../sheetreader/): words, ruling lines and cells, then the title block, BOM, revision table, general tolerance block and notes. With no text layer, the sheet is first turned upright and OCR'd.
2. **Rules** (`rules.py`, instant, no model):

   | Check | Raises |
   | :--- | :--- |
   | Required title-block fields (drawing no., title, rev, date, drawn, checked, approved/verified, material, scale, size, sheet) | medium if missing |
   | Drawing number and revision vs the file name | high if different; info if they only differ by OCR look-alikes |
   | Title-block REV vs the latest revision-table entry; revisions without a description | high / medium |
   | SIZE vs the real sheet size; SHEET n/m sanity | high |
   | Date valid, not in the future | high / medium |
   | Title-block SCALE can fit the largest dimension on the sheet | high |
   | BOM: duplicate or skipped item numbers, blank or zero quantity, missing description, a line referencing the drawing itself | high / medium |
   | NEXT ASSY. is the drawing's own number; DRAWN = APPROVED | high / medium |
   | General tolerance block readable; WEIGHT has a unit | low |

3. **Language** (`language.py`):
   - Known engineering misspellings and wrong words (`ANEROBIC`, `SILICON SEALANT`, …).
   - Words an offline dictionary doesn't know, minus an allow-list of shop terms.
   - **One** short Gemma call, thinking off, that sees only the title, notes, BOM and revision text.

   Any phrase the model reports has to occur in that text or it is dropped. At ~7 tokens/s this call takes well under a minute.
4. **`--deep`** (optional): the structured sheet goes to Gemma with its `<|think|>` reasoning on (`audit.py`). Expect several minutes per sheet on CPU. Its findings are labelled `(model-deep)`.

Status per sheet: **FAIL** if any high finding, **NEEDS_REVIEW** if medium/low, **PASS** otherwise. Info lines never change the status.

## Setup (Windows)

| Tool | Why | Get it |
|---|---|---|
| Python 3.10+ | runs everything | [python.org](https://www.python.org/downloads/) – tick **Add python.exe to PATH** |
| Ollama | serves Gemma 4 E4B locally | [ollama.com/download](https://ollama.com/download) |
| Tesseract OCR | only for sheets with no text layer | [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) |

```powershell
pip install -r requirements.txt
ollama pull gemma4:e4b
```

If Tesseract isn't on PATH, set `TESSERACT_CMD` in `config.py` (typically `C:\Program Files\Tesseract-OCR\tesseract.exe`).

## Fitting your templates (`config.py`)

- `EXTRA_TITLE_BLOCK_LABELS`: add your own label wording, e.g. `{"drawing_number": [r"PART\s*CODE"]}`. The built-in list (`sheetreader/titleblock.py`) covers DRAWING No./DWG NO, TITLE, REV, DATE, DRAWN, CHECKED, APPROVED, VERIFIED, MATERIAL, FINISH, WEIGHT, SCALE, SIZE, SHEET and NEXT ASSY.
- `EXTRA_ENGINEERING_WORDS`: trade names and in-house terms the spell check should accept.
- `LANGUAGE_OPTIONS` / `MODEL_OPTIONS`: sampling settings for the language call and for `--deep`.

## Speed

| Sheet | Time |
| :--- | :--- |
| Text layer (SolidWorks export) | under 1 s to read and check |
| No text layer (AutoCAD Print to PDF, A4) | 15–30 s, almost all OCR |
| Gemma language call | ~10–40 s at 7 tok/s |
| `--deep` | several minutes |

## Troubleshooting

- **"Model 'gemma4:e4b' not available"**: run `ollama pull gemma4:e4b` and check `ollama list`. The run continues without the model.
- **A field reads "(not found)"**: if the label is worded differently on your template, add it to `EXTRA_TITLE_BLOCK_LABELS`. Run with `--json` to see every cell that was read.
- **"Could not confirm which way up page N is"**: the sheet has too little readable text in its title-block corner after every rotation. Check the scan quality (300 dpi or more) or that the title block is bottom-right.
- **OCR slips in values** (`12045 g` for `120±5 g`): OCR'd values are labelled in the report. Export PDFs from CAD with text as text to avoid OCR entirely.
