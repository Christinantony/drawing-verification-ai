# Drawing QC agent

A first-pass QC check of a drawing set before it goes to print. It reads the
**literal parts** of each drawing (title block, BOM, revision table, notes,
references, callouts, view labels, balloons) on text-layer PDFs (SolidWorks)
and scanned or print-to-PDF sheets (AutoCAD, read by OCR). It then checks
them against each other and across the set. It does not check views or
geometry.

Everything runs offline: Python rules plus a local model through Ollama
(default `gemma4:26b`; any local model works).

## What happens, in order

| Station | What it does | Who |
|---|---|---|
| 1. Intake | Lists the folder: PDFs, Word checklists, unsupported files | code |
| 2. Read | Text layer if present, else OCR (rotation found automatically) | code |
| 3. Locate the parts | Title block, BOM, revision table, every note block, callouts, view labels, legends, balloons; text that fits nowhere is flagged | code |
| 4. Map the set | Learns the numbering (one family per product) and resolves every reference in any text: BOM, NEXT ASSY., notes, USE FILE, jigs/moulds | code |
| 5. QC checks | The catalogue below; spelling and "does this note make sense" questions go to the model | code + model |
| 6. Doubt loop | For items the rules can't settle: the model picks one tool (re-read the box, read an image of the box, search the set, open the linked drawing, hand to you), the rules check again; at most 2 passes | model chooses, code acts |
| 7. Report | `report.html` with a crop for every item, `findings.csv` for your verdicts | code |

## The checks

| | Check | Default |
|---|---|---|
| **A** | A1 BOM row complete (item, name, qty; no gaps/duplicates) · A2 balloons match BOM · A3 BOM name vs child title · A4 view label vs drawing · A5 title block complete (exemptions by drawing type) · A6 number and revision vs file name | must / look |
| **B** | B1 known misspellings · B2 unknown words (model decides) · B3 same term spelt two ways in the set · B4 standard callouts written two ways · B5 unsure OCR words | must / look |
| **C** | C1 every drawing reference exists and is written in the set's style · C2 NEXT ASSY. vs parent BOM · C3 item numbers in notes exist · C4 note fits its item (model) · C5 SEE NOTE / TABLE / DETAIL target exists · C6 USE FILE names this drawing · C7 jig, mould and template numbers listed · C8 checklist vs drawing (fills empty tolerance cells in a copy) | must / look |
| **D** | D1 note numbering · D2 note values vs title block · D3 same note worded differently · D4 note complete and clear (model) · D5 legend vs symbol | must / look |
| **E** | E1 general tolerances within a product · E2 dates within a product | look |

**must** = must fix before print · **look** = please look · **check by hand** =
the agent could not settle it.

## Set up (Windows)

1. Install Python 3.10+, [Ollama](https://ollama.com/download) and
   [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki). Tesseract is
   needed for scanned and print-to-PDF sheets. LibreOffice is needed only for
   old `.doc` checklists.
2. In PowerShell, in the repository folder:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
ollama pull gemma4:26b
python -m qc doctor
```

`doctor` checks the packages, Tesseract, Ollama and the model. It then makes
two test calls: a spelling judgement and, if the model can read images, a
small image. It prints the measured speed. The first call loads the model
into memory, which can take a minute.

**Memory:** the 26B model at 4-bit needs about 16 GB plus working memory. With
32 GB RAM, close other large programs while it runs. If it is too slow, try
`--model gemma4:e4b`, then compare the two reports.

## Run

```powershell
python -m qc review "C:\drawings\project-set" --out "C:\qc-out"
python -m qc review "C:\drawings\project-set" --out "C:\qc-out" --no-model   # rules and OCR only, about 2 min
python -m qc review "C:\drawings\project-set" --out "C:\qc-out" --model gemma4:e4b
```

Put all drawings of the set, and their dimension checklists, in one folder.
A checklist is matched to its drawing by the longest shared file-name ending.

Output, in `<out>\<date-time>\`:

- `report.html`: **must fix**, **please look**, **check by hand**, text not
  placed in a region, the set map (parents, children, references not in the
  folder, jigs/moulds), checklists, what was cleared and how, what was not
  checked
- `findings.csv`: one row per item, with empty `your_verdict` and
  `your_comment` columns. Mark each one real / false / don't care. This is
  the feedback that tunes the rules.
- `checklists\*_completed.docx`: checklist copies with empty tolerance cells
  filled (yellow = read from the drawing, orange = please confirm). Rows are
  never added and filled cells are never changed.
- `<out>\latest.txt`: the path of the newest report.

Exit code: 0 = no must-fix item, 1 = must-fix items, 3 = setup problem.

## Your settings

Copy `qc/qc_config.example.json`, edit it, and pass it with `--config`.
You can set: the model, shop words (correct words the dictionary doesn't
know), the severity of each check, required title-block fields, and which
fields each drawing type is exempt from (for example, a schematic needs no
material).

## Results on a real 17-sheet test set (rules only, no model)

The test set is confidential and is not in this repository. On it the agent
found:

- one title-block drawing number without the company prefix
- a BOM reference without a leading zero ("E-9B" for "E-09B")
- two "USE FILE" notes without the category letter
- one finish spelt two ways (CHROMATISATION / CHROMATIZATION)
- one sheet whose general tolerances differ from the other 16
- missing MATERIAL / WEIGHT fields
- the jig, mould and template numbers to confirm

It rebuilt the assembly tree from the drawings alone. Balloons matched the
BOM on all four assemblies. The doubt loop cleared two OCR misreads by
re-reading them and left two scanned-sheet items for a person. Time: about
2 minutes, of which about 2 × 40 s is OCR.

## Limits

- Literal text only: views, geometry, dimensions and GD&T are not checked.
  The checklist step (C8) reads tolerances next to the listed dimensions.
- OCR'd sheets are read well but not perfectly. Their unsure items are re-read
  or handed to you, never reported as certain.
- Balloons are checked on text-layer sheets only.
- The model's quality on your drawings is not yet measured. Use the verdicts
  in `findings.csv` to measure it.
