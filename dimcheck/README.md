# dimcheck: drawing tolerances ↔ inspection checklist

Reads the tolerance of every dimension listed on a dimension inspection checklist off the drawing, using each row's **zone** (ISO 7200-style border grid, e.g. `F6`) and **nominal dimension**. For each checklist row:

- **empty Tolerance cell → fill it** from the drawing;
- **filled cell → verify it** against the drawing and report `match` or `MISMATCH`. Existing values are never overwritten unless you pass `--overwrite`.

```powershell
python check_tolerances.py DRAWING.pdf CHECKLIST.doc -o CHECKED.docx --report review.csv
```

Legacy `.doc` checklists are converted automatically: with LibreOffice if it's installed, otherwise through Microsoft Word on Windows (`pip install pywin32`). The output is always `.docx`.

## Result on a real checklist

On a real drawing + checklist pair (a SolidWorks A3 sheet, 20 dimension rows), the tool gets **20 / 20** right, both when filling a blanked copy and when verifying the original. Before this rebuild it got 8 / 20 right and 2 silently wrong. The drawing is confidential and not in this repository. The breakdown:

| Printed on the drawing | Rows | Read as |
| :--- | :---: | :--- |
| Stacked limits, `0.0` over `-0.2`, signs as separate words, several on vertical dimensions | 6 | `0/-0.2`, `0/-0.1` |
| `101.2 ±0.1` where SolidWorks draws the `±` as a graphic (text layer has `101.2` then `0.1`) | 9 | `±0.1` |
| No tolerance printed: the general table by printed decimals (`102.0` → ONE DECIMAL) | 5 | `±0.1` |

![dimcheck on the synthetic example: zone grid and four checklist dimensions located](../assets/dimcheck_overlay.png)

![Console: verify mode, one planted MISMATCH](../assets/dimcheck_console.png)

## How it works

1. **Read the sheet** with the shared [`sheetreader`](../sheetreader/). Vertical dimensions keep their reading direction (the old reader returned `20.5` as `5.02`). Sheets without a text layer are OCR'd after being turned upright.
2. **Zone grid** (`zoning.py`): column numbers along the top/bottom margins, row letters along the left/right, upright text only. This keeps the letters in the confidentiality note that runs up the border from being mistaken for row labels.
3. **General tolerances**: read from the title block's own cells (`WITHOUT DECIMAL ±1 / ONE DECIMAL ±0.1 / TWO DECIMAL ±0.05 / ANGLE ±½°`).
4. **Find the dimension** (`dimension_matcher.py`) **by value**: `102` finds `102.0`, `Ø20.7` finds `20.7` (Ø is a graphic), `*106.1` prefers the `*106.1` key characteristic. If it isn't in the exact zone cell, the search widens slightly and the row is marked medium confidence.
5. **Read its tolerance** in the dimension text's own frame:
   - one small figure on the baseline right after it means `±`;
   - two figures one over the other are upper/lower limits;
   - a fit code (`f7`, `H7`) is reported but never converted.

   Figures belonging to a neighbouring dimension (raised but with nothing on the baseline) are ignored.
6. **Write and report** (`pipeline.py`, `checklist_io.py`): the source of every value (`explicit` / `general_table` / `assumed` / `unresolved`), a confidence, and notes.

## Options

- `--page N`: PDF page (0-based).
- `--zone-col / --dim-col / --tol-col`: checklist column indices if your template differs from `[Sl No, Zone, Dimension, Tolerance, ...]`.
- `--overwrite`: replace checklist values that disagree with the drawing.
- `--force-ocr`, `--ocr-dpi`: OCR controls.

## What still needs a human

- **Fit codes** (H7, f7): the limits depend on size (ISO 286) and aren't looked up.
- **Thread and feature callouts** with no tolerance printed beside them: these get the ONE DECIMAL tier as a labelled *assumption*.
- **Medium-confidence rows**: the dimension text sat just outside its zone cell.
- **Rows marked `kept`**: the checklist has a value but the drawing value couldn't be read to compare.
