# sheetreader

The shared "read a drawing sheet" layer behind drawing-audit, dimcheck and coherence. It turns any page into the same structure, whatever produced the PDF.

```python
from sheetreader import load_sheets, build_cells, read_title_block, find_tables
from sheetreader.record import read_drawing

sheet = load_sheets("drawing.pdf")[0]        # words + ruling lines, upright
cells = build_cells(sheet)                   # ruled boxes with the words inside
tb = read_title_block(sheet, cells)          # tb.fields, tb.general_tolerance
bom = find_tables(cells, "bom", sheet)       # also "revision"

rec = read_drawing("drawing.pdf")[0]         # all of the above as one JSON-friendly record
```

| Module | Does |
| :--- | :--- |
| `loader.py` | Text layer via PyMuPDF, keeping each line's reading direction. With no text layer: rasterise, find the upright rotation by OCR-ing the title-block corner in each orientation, erase ruling lines, OCR horizontal and vertical text. Coloured ink is read via the darkest channel. |
| `cells.py` | For each word, the nearest ruling line on each side that actually spans it gives its box (works on loose line segments, not just rectangles). Per-cell re-OCR for OCR sheets. |
| `titleblock.py` | Title block = the cells connected to the DRAWING No. cell. Fields found by label inside each cell; label-only cells take the next cell below or right. General tolerance block with OCR-aware plausibility checks. |
| `tables.py` | Tables found by their header row; rows collected outward in either direction, anchored on the key column, stopping at anything that doesn't fit the columns. |
| `normalize.py` | Comparing identifiers: letters and digits only, plus OCR look-alike folding for OCR'd text; drawing number and revision from file names; revision ordering. |
| `record.py` | One record per sheet: title block, general tolerances, BOM, revision table, notes, template form ID, dimension values, OCR confidence. |

Coordinates are in "upright sheet points": 72 per inch, origin top-left as a person reads the sheet. A sideways plot has already been turned by the time anything else sees it.
