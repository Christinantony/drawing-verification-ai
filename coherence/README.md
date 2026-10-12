# coherence: cross-file consistency for a drawing set

Point it at the folder for one product (drawing PDFs, dimension checklists, other Word/Excel files) and it reports where the files disagree with each other.

```powershell
python check_coherence.py path\to\folder --out coherence_report.md
python check_coherence.py path\to\folder --no-model        # drawings + checklists only, no Ollama
```

Drawings are read with the shared [`sheetreader`](../sheetreader/) (text layer or OCR), so **no model is involved for PDFs or checklists**: every finding comes from the rules below.

## What it checks

| Rule | Severity |
| :--- | :--- |
| Same drawing number on several sheets/files with a different revision, date or title | high |
| A drawing's `NEXT ASSY.` parent doesn't list it in its BOM | high |
| BOM description disagrees with the referenced drawing's title | medium |
| A child's `NEXT ASSY.` points to a different drawing than the parent that lists it | medium |
| Drawings on the **same template form** (e.g. `XY-F-DRG-01`) with different general tolerances | medium |
| Checklist material or weight differs from its drawing's title block | high |
| Checklist tolerance row differs from the drawing (dimcheck in verify mode) | high |
| BOM line or `NEXT ASSY.` points to a drawing that isn't in the folder | low ("add it to check it") |

Details that make this work on real sets:

- **Drawing references vs hardware.** A BOM `REF.` counts as a drawing reference only if it starts like the drawing numbers in the set. Fasteners (`M2.5x4 CSK SCREWS`), materials (`LOCTITE 263`) and vendor part numbers are not expected to have drawings.
- **Combined drawings.** A drawing numbered `…-E-03 A&B` answers to BOM references `…-E-03A` and `…-E-03B`.
- **OCR look-alikes.** On OCR'd sheets, a number that matches a known drawing number except for look-alike characters (`AI-` for `AT-`) is corrected against the exact numbers from file names and text-layer sheets. The report shows the corrected value.
- **Checklists** are matched to their drawing by the longest shared file-name ending (`XX-F-DCL-PART_REV._00.doc` ↔ `PART_REV._00.pdf`).

Other `.docx` / `.xlsx` files still go through the original model-based extraction (Gemma via Ollama): the same part number with different names, revisions or dates, plus an optional fuzzy same-part pass.

![Cross-file rules on a small drawing set](../assets/coherence_rules.png)

*`check_drawings()` on the hand-built records from `../tests/test_coherence.py`.*
