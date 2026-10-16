# Drawing QC agent

A first-pass QC check of a drawing set before it goes to print. It reads the
**literal parts** of each drawing (title block, BOM, revision table, notes,
references, callouts, view labels, balloons) on text-layer PDFs (SolidWorks)
and scanned or print-to-PDF sheets (AutoCAD, read by OCR). It then checks
them against each other and across the set. It does not check views or
geometry.

Everything runs offline: Python rules plus a local model through Ollama
(default `gemma4:26b`; any local model works).

**Code finds the facts, the model makes the decisions.** The code reads the
sheets, finds the parts and checks them exactly: does this reference exist,
is item 9 in the BOM, are the notes numbered in order. The model then decides
what to report, how serious it is and how to word it. It follows your **QC
policy**, a plain-English document (`qc/policy.md`) that you edit to change
the agent's behaviour. You don't need to change any code.

## What happens, in order

| Step | What it does | Who |
|---|---|---|
| 1. Intake | Lists the folder: PDFs, Word checklists, unsupported files | code |
| 2. Read | Text layer if present, else OCR (rotation found automatically) | code |
| 3. Locate the parts | Boxes the title block, BOM, revision table, every note block, callouts, view labels, legends and balloons. **Layout check:** the model looks at a small image of the sheet with the boxes numbered. It names each box and reports any text block that has no box; the code then boxes it from the words in that area, so the edges stay exact. **± values:** SolidWorks draws the ± of '135 ± 10 g' as a graphic, so the text reads '135 10 g'. The model looks at an image of each such value; with a ±, the text becomes '135±10 g'; without one, it is reported (D6). | code proposes, model judges |
| 4. Map the set | Learns the numbering (one family per product) and resolves every reference in any text: BOM, NEXT ASSY., notes, USE FILE, jigs/moulds | code |
| 5. QC checks | The catalogue below produces facts. Spelling, "does this note fit its item" and "are the notes clear" are asked as short questions | code + model |
| 6. Doubt loop | For facts the code can't settle: the model picks one tool (re-read the box, read an image of the box, search the set, open the linked drawing, hand to you), then the code checks again; at most 2 passes | model chooses, code acts |
| 7. Decisions | Under your QC policy, the model decides for every fact: must / look / drop, plus the wording. It then does an **open review** of each sheet's text for problems no rule covers, and **drafts the review summary** | model |
| 8. Report | `report.html` with a crop for every item, `findings.csv` for your verdicts | code |

**Guardrails** (generic, not QC rules):
- Every fact must get a decision. A fact the model doesn't answer keeps the rule's decision, and the report says so.
- Every dropped fact is listed with the model's reason.
- An open-review observation must quote text that is really on the sheet; any other observation is discarded.
- Answers must follow a fixed format.
- Some checks are never dropped by the model (missing title-block fields,
  wrong references, spelling two ways, ...; `never_drop` in the settings).
  If the model wants to drop one, it becomes **please look** and the report
  says so.
- An open-review observation that compares BOM rows referring to different
  drawings is discarded: they are different parts.
- The model gets each sheet's context: what each BOM row refers to and
  whether its name matches that drawing's title, the parts of each
  sub-assembly, what SEE TABLE points to, and today's date.

## The checks

| | Check | Default |
|---|---|---|
| **A** | A1 BOM row complete (item, name, qty; no gaps/duplicates) · A2 balloons match BOM · A3 BOM name vs child title · A4 view label vs drawing · A5 title block complete (exemptions by drawing type) · A6 number and revision vs file name | must / look |
| **B** | B1 known misspellings · B2 unknown words (model decides) · B3 same term spelt two ways in the set · B4 standard callouts written two ways · B5 unsure OCR words | must / look |
| **C** | C1 every drawing reference exists and is written in the set's style · C2 NEXT ASSY. vs parent BOM · C3 item numbers in notes exist · C4 note fits its item (model) · C5 SEE NOTE / TABLE / DETAIL target exists · C6 USE FILE names this drawing · C7 jig, mould and template numbers listed · C8 checklist vs drawing (fills empty tolerance cells in a copy) | must / look |
| **D** | D1 note numbering · D2 note values vs title block · D3 same note worded differently · D4 note complete and clear (model) · D5 legend vs symbol · D6 value written without ± (checked on the image) | must / look |
| **E** | E1 general tolerances within a product · E2 dates (DD/MM/YYYY): none after today, title block agrees with the current revision in the revision table, dates agree within a product | must / look |

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
python -m qc review "C:\drawings\project-set" --out "C:\qc-out" --decide rules   # the rules decide (comparison)
python -m qc review "C:\drawings\project-set" --out "C:\qc-out" --no-model       # no model at all, about 2 min
python -m qc review "C:\drawings\project-set" --out "C:\qc-out" --verdicts "C:\qc-verdicts"   # learn from your verdicts
python -m qc learn --verdicts "C:\qc-verdicts"     # turn your false verdicts into house rules (you approve each)
python -m qc score --verdicts "C:\qc-verdicts"     # how many reported items were real, run by run
python -m qc review "C:\drawings\project-set" --out "C:\qc-out" --skip layout,open_review
python -m qc layout-test "C:\drawings\project-set\<one sheet>.pdf" --out "C:\qc-out"
```

- `--verdicts`: your verdicts folder (or `"verdicts_dir"` in your settings
  file). See "Teaching the agent" below. `--examples FILE ...` still works
  for single files.
- `--skip`: turns off model steps (`layout`, `judge`, `doubt`, `decide`,
  `open_review`, `summary`) when you need a faster run.
- `layout-test`: tries both layout approaches on one sheet: (A) the model
  draws its own boxes on the plain image, (B) the model judges the program's
  numbered boxes. You get one page with both images and the time each took.

Put all drawings of the set, and their dimension checklists, in one folder.
A checklist is matched to its drawing by the longest shared file-name ending.

Output, in `<out>\<date-time>\`:

- `report.html`: **must fix**, **please look**, **check by hand**, text not
  placed in a region, the set map (parents, children, references not in the
  folder, jigs/moulds), checklists, what was cleared and how, what was not
  checked
- `findings.csv`: one row per item, including the items the model dropped.
  `rule_said` and `model_said` show both decisions side by side. Mark each row
  real / false / don't care in `your_verdict`, or use the buttons in the
  report (below).
- `checklists\*_completed.docx`: checklist copies with empty tolerance cells
  filled (yellow = read from the drawing, orange = please confirm). Rows are
  never added and filled cells are never changed.
- `<out>\latest.txt`: the path of the newest report.

Exit code: 0 = no must-fix item, 1 = must-fix items, 3 = setup problem.

## Teaching the agent (the lessons library)

The model is not retrained. It learns from your verdicts, which stay on your
PC in one folder, e.g. `C:\qc-verdicts`:

1. **Mark the report.** Every item in `report.html` has *Real / False /
   Don't care* buttons and a comment box ("why?"). The marks are kept in the
   browser while you work. **Save verdicts** downloads
   `verdicts_<run>.csv`; move it into your verdicts folder. Marking the CSV by
   hand works too.
2. **Silence list (exact, no model).** On the next run, a finding you marked
   false or don't care is not reported again when the same check finds the
   same text in the **same product family**. It is listed under "Silenced by
   your earlier verdicts" with your date and comment. Mark it Real to bring it
   back. Checks that are never dropped (`never_drop`: missing title-block
   fields, wrong references, spelling two ways, ...) are not silenced; they
   become please look and say why.
3. **Relevant examples (model).** Each batch of facts is shown the 10 earlier
   verdicts most like it: same check, similar words, same product family.
   All your verdicts are kept, not only the first few.
4. **House rules (model, approved by you).** `python -m qc learn` asks the
   model to turn your new false verdicts into general rules, e.g. "A BOM row
   may name a multi-coat paint system". For each one you answer
   accept / edit / reject / skip. Accepted rules go into
   `house_rules.md` in your verdicts folder and are read next to the QC
   policy on every run. Edit or delete lines in that file freely. A verdict is
   proposed only once (`learnt.json` remembers).
5. **Score.** `python -m qc score` prints, per marked run, how many reported
   items were real and how many drops were wrong, and per check over all
   runs. The report shows the last marked run's figure.

Comments teach the most: "PCB-A is a different part (other drawing)" lets
`learn` write a rule; a bare "false" only silences that one finding.

## Your QC policy and settings

`qc/policy.md` is the policy the model follows: what counts as must / look /
drop, standard parts, spelling, OCR caution, wording. Edit it, or pass your
own with `--policy`.

The report's "Time per step" line shows where the minutes go. On a CPU most
of the time is in steps 3 (layout check: one image per sheet) and 7
(decisions and open review). Expect several minutes per sheet with the 26B
model; measure it on your PC first.

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
- one finish spelt two ways (CHROMATISATION / CHROMATIZATION), must
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
- The model's quality on your drawings is not yet measured, and the model
  steps have so far been tested only with a fake model. Run `--decide model`
  and `--decide rules` on the same set and mark `findings.csv`; the
  `rule_said` / `model_said` columns show where each is better.
