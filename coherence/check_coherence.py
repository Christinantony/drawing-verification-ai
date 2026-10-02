#!/usr/bin/env python3
"""
check_coherence.py

Scans a folder of .docx / .xlsx / .pdf files (engineering drawings + docs),
uses a local Gemma model (via Ollama) to extract structured data, then
cross-checks for inconsistencies:

  - Dates / part numbers / part names / revisions across docx & xlsx files
  - For PDF drawings: title-block fields (drawing no, rev, date, title,
    material, next assy.) and BOM tables, cross-checked so that:
      * every BOM line's referenced part number exists as its own drawing,
        with matching description
      * NEXT ASSY. links are consistent in both directions (if A's
        NEXT ASSY. = B, then B's BOM should list A)
      * the same drawing number appearing on multiple sheets has a
        consistent revision and date

Usage:
    python check_coherence.py /path/to/folder [--model gemma3:4b] [--out report.md]

Requirements:
    pip install python-docx openpyxl requests pdfplumber

Assumes Ollama is running locally and the model has been pulled:
    ollama pull gemma3:4b

PDF NOTES:
  - This script extracts text directly (pdfplumber). It does NOT do OCR or
    page rotation. If a PDF's text comes out empty or garbled (scanned
    image, or a rotated drawing sheet), it will be flagged in the console
    and skipped -- you'll need an OCR pass first (see the "needing OCR"
    section in the report).
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests

try:
    import docx
except ImportError:
    docx = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "gemma3:4b"

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

GENERIC_EXTRACTION_PROMPT = """You are extracting structured data from a document excerpt (Word or Excel content).

Find every instance of:
- dates (any format)
- part numbers (alphanumeric codes identifying a part; may include dashes, dots, slashes, spaces)
- part names / descriptions
- revision codes (e.g. "Rev A", "Rev. 3", "-R2", "Revision 1.0")

Return ONLY a JSON array (no other text, no markdown fences). Each element:
{{
  "date": "<date as found, or null>",
  "part_number": "<part number as found, or null>",
  "part_name": "<part name as found, or null>",
  "revision": "<revision as found, or null>",
  "context": "<short snippet of surrounding text, max 15 words>"
}}

Only include an object if at least one field is non-null. Do not invent data.

TEXT EXCERPT:
---
{chunk}
---

JSON array:"""

DRAWING_EXTRACTION_PROMPT = """You are extracting the title block and bill-of-materials (BOM) from a
mechanical engineering drawing sheet. The raw text below was pulled from a PDF and may be jumbled
in order (PDF text extraction does not preserve visual layout), but the field labels are present.

Extract and return ONLY this JSON object (no other text, no markdown fences):
{{
  "drawing_no": "<value after 'DRAWING No.' / 'DRAWING NO.', or null>",
  "revision": "<value after 'REV.' near the title block -- just the number/code, e.g. '01', or null>",
  "date": "<date near the title block, or null>",
  "title": "<value after 'TITLE', or null>",
  "material": "<value after 'MATERIAL', or null>",
  "next_assy": "<value after 'NEXT ASSY.' -- a drawing number, or null if blank/not present>",
  "sheet": "<sheet number e.g. '1/5', or null>",
  "bom": [
    {{
      "item_no": "<SL NO / item number in the parts list, or null>",
      "description": "<part description/name, or null>",
      "qty": "<quantity, or null>",
      "ref_part_no": "<a referenced drawing/part number for this line item if one is given, e.g. 'VT-ML-JL 03 A1-01A', or null if it's just a generic fastener/material with no drawing>"
    }}
  ],
  "revision_table": [
    {{"rev_no": "<rev number>", "details": "<revision description text>"}}
  ]
}}

Rules:
- If there is no BOM table in this text, return "bom": [].
- If there is no revision table content, return "revision_table": [].
- Do not invent values. Use null where a field is not present.
- A BOM line only gets a "ref_part_no" if an actual drawing-number-looking code is associated with it
  (e.g. matches a pattern like "VT-ML-JL 03 A1-01A"). Generic hardware (bolts, o-rings, dowel pins with
  just a material spec) should have ref_part_no: null.

RAW TEXT:
---
{chunk}
---

JSON object:"""

FUZZY_MATCH_PROMPT = """You are comparing two engineering records to decide if they refer to the
SAME real-world part, despite possibly different wording/formatting.

Record A: {a}
Record B: {b}

Answer with ONLY one word: SAME, DIFFERENT, or UNSURE."""


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def call_ollama(prompt, model, timeout=180):
    resp = requests.post(
        OLLAMA_URL,
        json={"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0}},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("response", "")


def extract_json_blob(text, want_array=True):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    pattern = r"\[.*\]" if want_array else r"\{.*\}"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return [] if want_array else {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return [] if want_array else {}


def chunk_text(text, max_chars=3000):
    paras = text.split("\n")
    chunks, current = [], ""
    for p in paras:
        if len(current) + len(p) > max_chars and current:
            chunks.append(current)
            current = p
        else:
            current += "\n" + p
    if current.strip():
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def read_docx(path):
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def read_xlsx(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def read_pdf_pages(path):
    """Returns (list of (page_number, text)), (list of flagged page numbers needing OCR/rotation)."""
    pages = []
    flagged = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if len(text.strip()) < 30:
                flagged.append(i)
            pages.append((i, text))
    return pages, flagged


# ---------------------------------------------------------------------------
# Extraction per file
# ---------------------------------------------------------------------------

def extract_generic_from_text(text, filename, model):
    records = []
    for i, chunk in enumerate(chunk_text(text)):
        print(f"    chunk {i+1}")
        try:
            response = call_ollama(GENERIC_EXTRACTION_PROMPT.format(chunk=chunk), model)
        except requests.RequestException as e:
            print(f"    ! Ollama call failed: {e}", file=sys.stderr)
            continue
        for r in extract_json_blob(response, want_array=True):
            r["_file"] = filename
            records.append(r)
    return records


def extract_drawing_from_text(text, filename, page_no, model):
    try:
        response = call_ollama(DRAWING_EXTRACTION_PROMPT.format(chunk=text), model)
    except requests.RequestException as e:
        print(f"    ! Ollama call failed: {e}", file=sys.stderr)
        return None
    obj = extract_json_blob(response, want_array=False)
    if not obj:
        return None
    obj["_file"] = filename
    obj["_page"] = page_no
    return obj


def process_file(path, model, ocr_todo):
    ext = path.suffix.lower()
    generic_records = []
    drawings = []

    if ext == ".docx":
        if docx is None:
            print(f"  ! python-docx not installed, skipping {path.name}", file=sys.stderr)
            return generic_records, drawings
        text = read_docx(path)
        if text.strip():
            generic_records = extract_generic_from_text(text, path.name, model)

    elif ext == ".xlsx":
        if openpyxl is None:
            print(f"  ! openpyxl not installed, skipping {path.name}", file=sys.stderr)
            return generic_records, drawings
        text = read_xlsx(path)
        if text.strip():
            generic_records = extract_generic_from_text(text, path.name, model)

    elif ext == ".pdf":
        if pdfplumber is None:
            print(f"  ! pdfplumber not installed, skipping {path.name}", file=sys.stderr)
            return generic_records, drawings
        pages, flagged = read_pdf_pages(path)
        for pg in flagged:
            ocr_todo.append(f"{path.name} (page {pg})")
        for page_no, text in pages:
            if len(text.strip()) < 30:
                continue  # likely needs OCR/rotation, already flagged
            print(f"  - {path.name} page {page_no}")
            d = extract_drawing_from_text(text, path.name, page_no, model)
            if d:
                drawings.append(d)

    return generic_records, drawings


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def norm(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip().lower()
    return s or None


def norm_pn(s):
    """Normalize a part/drawing number for matching: collapse spaces, uppercase."""
    if s is None:
        return None
    s = str(s).strip().upper()
    s = re.sub(r"\s+", " ", s)
    return s or None


# ---------------------------------------------------------------------------
# Generic (docx/xlsx) cross-checks
# ---------------------------------------------------------------------------

def group_by_part_number(records):
    groups = defaultdict(list)
    for r in records:
        key = norm(r.get("part_number"))
        if key:
            groups[key].append(r)
    return groups


def find_generic_mismatches(groups):
    issues = []
    for pn, records in groups.items():
        names = {norm(r.get("part_name")) for r in records if r.get("part_name")}
        revs = {norm(r.get("revision")) for r in records if r.get("revision")}
        dates = {norm(r.get("date")) for r in records if r.get("date")}
        if len(names) > 1:
            issues.append({"type": "Part name mismatch", "part_number": pn, "detail": names, "records": records})
        if len(revs) > 1:
            issues.append({"type": "Revision mismatch", "part_number": pn, "detail": revs, "records": records})
        if len(dates) > 1:
            issues.append({"type": "Date mismatch", "part_number": pn, "detail": dates, "records": records})
    return issues


# ---------------------------------------------------------------------------
# Drawing (PDF) cross-checks
# ---------------------------------------------------------------------------

def check_drawings(drawings):
    issues = []

    by_dwg_no = defaultdict(list)
    for d in drawings:
        key = norm_pn(d.get("drawing_no"))
        if key:
            by_dwg_no[key].append(d)

    # 1. Same drawing number, inconsistent rev/date/title across sheets
    for dwg_no, sheets in by_dwg_no.items():
        revs = {norm(s.get("revision")) for s in sheets if s.get("revision")}
        dates = {norm(s.get("date")) for s in sheets if s.get("date")}
        titles = {norm(s.get("title")) for s in sheets if s.get("title")}
        if len(revs) > 1:
            issues.append({"type": "Drawing revision mismatch", "drawing_no": dwg_no,
                            "detail": revs, "sheets": sheets})
        if len(dates) > 1:
            issues.append({"type": "Drawing date mismatch", "drawing_no": dwg_no,
                            "detail": dates, "sheets": sheets})
        if len(titles) > 1:
            issues.append({"type": "Drawing title mismatch", "drawing_no": dwg_no,
                            "detail": titles, "sheets": sheets})

    # 2. BOM references -> do they exist as real drawings? Does description match?
    for d in drawings:
        parent_no = norm_pn(d.get("drawing_no"))
        for line in d.get("bom", []):
            ref = norm_pn(line.get("ref_part_no"))
            if not ref:
                continue
            if ref not in by_dwg_no:
                issues.append({
                    "type": "BOM references missing drawing",
                    "drawing_no": parent_no,
                    "detail": f"BOM item '{line.get('description')}' references {ref}, "
                              f"but no drawing with that number was found in the file set.",
                    "sheets": [d],
                })
            else:
                child_titles = {norm(s.get("title")) for s in by_dwg_no[ref] if s.get("title")}
                bom_desc = norm(line.get("description"))
                if bom_desc and child_titles and bom_desc not in child_titles:
                    issues.append({
                        "type": "BOM description vs drawing title mismatch",
                        "drawing_no": parent_no,
                        "detail": f"BOM calls {ref} '{line.get('description')}', "
                                  f"but its drawing title is {child_titles}.",
                        "sheets": [d] + by_dwg_no[ref],
                    })

    # 3. NEXT ASSY. bidirectional check
    for d in drawings:
        this_no = norm_pn(d.get("drawing_no"))
        next_assy = norm_pn(d.get("next_assy"))
        if not next_assy:
            continue
        if next_assy not in by_dwg_no:
            issues.append({
                "type": "NEXT ASSY. points to unknown drawing",
                "drawing_no": this_no,
                "detail": f"NEXT ASSY. = {next_assy}, but no such drawing found in file set.",
                "sheets": [d],
            })
            continue
        parents = by_dwg_no[next_assy]
        found_in_bom = False
        for parent in parents:
            refs = {norm_pn(line.get("ref_part_no")) for line in parent.get("bom", [])}
            if this_no in refs:
                found_in_bom = True
                break
        if not found_in_bom:
            issues.append({
                "type": "NEXT ASSY. not reciprocated in parent BOM",
                "drawing_no": this_no,
                "detail": f"{this_no} says NEXT ASSY. = {next_assy}, but {next_assy}'s BOM "
                          f"does not list {this_no}.",
                "sheets": [d] + parents,
            })

    return issues


# ---------------------------------------------------------------------------
# Fuzzy cross-check (generic records only)
# ---------------------------------------------------------------------------

def fuzzy_cross_check(all_records, model):
    unmatched = [r for r in all_records if r.get("part_name") and not norm(r.get("part_number"))]
    suspects = []
    seen = set()
    for i, a in enumerate(unmatched):
        for b in unmatched[i + 1:]:
            if norm(a.get("part_name")) == norm(b.get("part_name")):
                continue
            if a["_file"] == b["_file"]:
                continue
            key = tuple(sorted([norm(a.get("part_name")), norm(b.get("part_name"))]))
            if key in seen:
                continue
            seen.add(key)
            prompt = FUZZY_MATCH_PROMPT.format(a=json.dumps(a), b=json.dumps(b))
            try:
                verdict = call_ollama(prompt, model).strip().upper()
            except requests.RequestException:
                continue
            if verdict.startswith("SAME"):
                suspects.append((a, b))
    return suspects


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_report(generic_issues, drawing_issues, suspects, ocr_todo, out_path, files_scanned):
    lines = ["# Document Coherence Report\n"]
    lines.append(f"Files scanned: {len(files_scanned)}\n")
    for f in files_scanned:
        lines.append(f"- {f}")
    lines.append("")

    if ocr_todo:
        lines.append("## Pages needing OCR / rotation (skipped)\n")
        for item in ocr_todo:
            lines.append(f"- {item}")
        lines.append("")

    total_issues = len(generic_issues) + len(drawing_issues)
    if total_issues == 0 and not suspects:
        lines.append("No inconsistencies found across files.\n")

    if drawing_issues:
        lines.append(f"## Drawing (PDF) issues: {len(drawing_issues)}\n")
        for issue in drawing_issues:
            lines.append(f"### {issue['type']} — `{issue['drawing_no']}`")
            lines.append(f"{issue['detail']}\n")
            lines.append("| File | Page | Drawing No. | Rev | Date | Title | Next Assy. |")
            lines.append("|---|---|---|---|---|---|---|")
            for s in issue["sheets"]:
                lines.append(
                    f"| {s.get('_file','')} | {s.get('_page','')} | {s.get('drawing_no','')} "
                    f"| {s.get('revision','')} | {s.get('date','')} | {s.get('title','')} "
                    f"| {s.get('next_assy','')} |"
                )
            lines.append("")

    if generic_issues:
        lines.append(f"## Word/Excel mismatches: {len(generic_issues)}\n")
        for issue in generic_issues:
            lines.append(f"### {issue['type']} — part number `{issue['part_number']}`")
            lines.append(f"Conflicting values: {', '.join(str(d) for d in issue['detail'])}\n")
            lines.append("| File | Date | Part Number | Part Name | Revision | Context |")
            lines.append("|---|---|---|---|---|---|")
            for r in issue["records"]:
                lines.append(
                    f"| {r.get('_file','')} | {r.get('date','')} | {r.get('part_number','')} "
                    f"| {r.get('part_name','')} | {r.get('revision','')} | {r.get('context','')} |"
                )
            lines.append("")

    if suspects:
        lines.append(f"## Possible same-part naming inconsistencies (fuzzy match): {len(suspects)}\n")
        for a, b in suspects:
            lines.append(f"- `{a.get('part_name')}` (in {a['_file']}) may be the same part as "
                         f"`{b.get('part_name')}` (in {b['_file']}) — verify manually.")
        lines.append("")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="Folder containing .docx/.xlsx/.pdf files")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default="coherence_report.md")
    ap.add_argument("--skip-fuzzy", action="store_true")
    args = ap.parse_args()

    folder = Path(args.folder)
    files = sorted([p for p in folder.iterdir() if p.suffix.lower() in (".docx", ".xlsx", ".pdf")])
    if not files:
        print("No .docx/.xlsx/.pdf files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} files. Extracting with model={args.model}...")
    all_generic_records = []
    all_drawings = []
    ocr_todo = []

    for f in files:
        print(f"Processing {f.name}...")
        generic, drawings = process_file(f, args.model, ocr_todo)
        all_generic_records.extend(generic)
        all_drawings.extend(drawings)

    print(f"\nExtracted {len(all_generic_records)} generic records, {len(all_drawings)} drawing sheets.")

    groups = group_by_part_number(all_generic_records)
    generic_issues = find_generic_mismatches(groups)
    drawing_issues = check_drawings(all_drawings)

    suspects = []
    if not args.skip_fuzzy and all_generic_records:
        print("Running fuzzy cross-check...")
        suspects = fuzzy_cross_check(all_generic_records, args.model)

    write_report(generic_issues, drawing_issues, suspects, ocr_todo, args.out, [f.name for f in files])
    print(f"\nDone. Report written to {args.out}")
    print(f"  {len(drawing_issues)} drawing issues, {len(generic_issues)} generic mismatches, "
          f"{len(suspects)} fuzzy suspects, {len(ocr_todo)} pages need OCR/rotation.")


if __name__ == "__main__":
    main()
