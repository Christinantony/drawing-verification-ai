"""
Deterministic checks on one drawing record (see sheetreader.record).

These replace most of what the model used to be asked to "notice": they are
instant, never invent a problem, and each finding says exactly which values
disagreed. The model is only used afterwards for language (language.py).

Every check returns findings as plain dicts:
    {id, severity, category, location, description, suggested_fix, source}
severity: high | medium | low | info   (info never changes the status)
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sheetreader.normalize import alnum, ocr_key, rev_key, same_id  # noqa: E402

PAPER_LONG_MM = {"A0": 1189, "A1": 841, "A2": 594, "A3": 420, "A4": 297}

# Fields every released drawing should carry. Either APPROVED or VERIFIED
# satisfies the sign-off requirement (templates differ).
REQUIRED_FIELDS = ["drawing_number", "title", "revision", "date", "drawn_by",
                   "checked_by", "material", "scale", "size", "sheet"]
INITIAL_REVS = {"00", "0", "A", "-", "01A"}


class Findings:
    def __init__(self):
        self.items: List[dict] = []

    def add(self, severity, category, location, description, fix=None, source="rule"):
        self.items.append({
            "id": f"F{len(self.items) + 1}", "severity": severity, "category": category,
            "location": location, "description": description, "suggested_fix": fix,
            "source": source,
        })


def _ocr_note(rec) -> str:
    return " (read by OCR - check the sheet before correcting)" if rec["text_source"] == "ocr" else ""


def check_fields(rec, f: Findings):
    tb = rec["title_block"]
    for name in REQUIRED_FIELDS:
        if not tb.get(name):
            f.add("medium", "missing_field", f"title_block.{name}",
                  f"No value found for {name.replace('_', ' ').upper()} in the title block{_ocr_note(rec)}.",
                  "Fill in the field, or add its label wording to the title-block labels if it is present.")
    if not (tb.get("approved_by") or tb.get("verified_by")):
        f.add("medium", "missing_field", "title_block.approved_by",
              "Neither APPROVED nor VERIFIED has a name in the title block.")
    if not tb.get("next_assy"):
        f.add("info", "missing_field", "title_block.next_assy",
              "NEXT ASSY. is blank (expected only for a top-level assembly).")
    elif same_id(tb.get("next_assy"), tb.get("drawing_number")):
        f.add("high", "contradiction", "title_block.next_assy",
              f"NEXT ASSY. ({tb['next_assy']}) is the drawing's own number.")


def check_identity(rec, f: Findings):
    """Title block vs the file name the drawing is stored under."""
    tb = rec["title_block"]
    ocr = rec["text_source"] == "ocr"
    fdn, frev = rec.get("filename_drawing_number"), rec.get("filename_revision")
    dn = tb.get("drawing_number")
    if fdn and dn:
        if alnum(fdn) == alnum(dn):
            pass
        elif ocr and ocr_key(fdn) == ocr_key(dn):
            f.add("info", "ocr_quality", "title_block.drawing_number",
                  f"OCR read the drawing number as '{dn}'; it matches the file name '{fdn}' "
                  "once look-alike characters are allowed for.")
        else:
            f.add("high", "contradiction", "title_block.drawing_number",
                  f"Title block drawing number '{dn}' does not match the file name '{fdn}'{_ocr_note(rec)}.",
                  "Rename the file or correct the title block so they agree.")
    rev = tb.get("revision")
    if frev and rev and alnum(frev) != alnum(rev) and not (ocr and ocr_key(frev) == ocr_key(rev)):
        f.add("high", "contradiction", "title_block.revision",
              f"Title block is REV {rev} but the file name says REV {frev}.",
              "Make the file name and title block revision agree.")


def check_revision_table(rec, f: Findings):
    tb = rec["title_block"]
    rev = tb.get("revision")
    rows = [r for r in rec.get("revision_table", []) if r.get("rev")]
    if not rev:
        return
    if not rows:
        if rev.upper() not in INITIAL_REVS:
            f.add("medium", "contradiction", "revision_table",
                  f"Title block is REV {rev} but the revision table has no entries.",
                  f"Add the REV {rev} change description to the revision table.")
        return
    keyed = [(rev_key(r["rev"]), r) for r in rows if rev_key(r["rev"])]
    if not keyed:
        return
    latest = max(keyed, key=lambda t: t[0])[1]
    if alnum(latest["rev"]) != alnum(rev):
        higher = rev_key(latest["rev"]) > rev_key(rev)
        f.add("high", "contradiction", "revision_table",
              f"Revision table's latest entry is REV {latest['rev']} but the title block says REV {rev}"
              + (" (the table is ahead of the title block)." if higher else "."),
              "Update the title block revision or the revision table so they match.")
    for r in rows:
        if not (r.get("details") or "").strip():
            f.add("medium", "missing_field", f"revision_table.rev_{r['rev']}",
                  f"Revision {r['rev']} has no description of the change.")


def check_sheet(rec, f: Findings):
    tb = rec["title_block"]
    size, paper = tb.get("size"), rec.get("paper_size")
    if size and paper:
        m = re.search(r"A[0-4]", size.upper())
        if m and m.group(0) != paper:
            f.add("high", "contradiction", "title_block.size",
                  f"Title block says SIZE {m.group(0)} but the sheet is {paper}.",
                  f"Change SIZE to {paper}, or re-export on the correct sheet size.")
    sheet = tb.get("sheet")
    if sheet:
        m = re.search(r"(\d+)\s*/\s*(\d+)", sheet)
        if m:
            n, total = int(m.group(1)), int(m.group(2))
            if n > total or n == 0:
                f.add("high", "contradiction", "title_block.sheet", f"SHEET {n}/{total} is impossible.")
            elif total > rec.get("page_count", 1) and rec.get("page_count", 1) == 1:
                f.add("info", "missing_field", "title_block.sheet",
                      f"SHEET {n}/{total}: only this sheet is in the file, so the other "
                      f"{total - 1} sheet(s) were not checked.")


def _parse_date(s: str) -> Optional[_dt.date]:
    m = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s or "")
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + 2000 if y < 100 else y
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


def check_date(rec, f: Findings, today: Optional[_dt.date] = None):
    s = rec["title_block"].get("date")
    if not s:
        return
    d = _parse_date(s)
    today = today or _dt.date.today()
    if d is None:
        f.add("high", "contradiction", "title_block.date",
              f"Date '{s}' is not a valid DD/MM/YY(YY) date{_ocr_note(rec)}.")
    elif d > today:
        f.add("medium", "contradiction", "title_block.date", f"Date {s} is in the future.")


def _scale_factor(s: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", s or "")
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return a / b if b else None


def check_scale(rec, f: Findings):
    """A title-block scale that cannot fit the part on the sheet."""
    s = rec["title_block"].get("scale")
    k = _scale_factor(s)
    dims = rec.get("dimension_values") or []
    long_mm = PAPER_LONG_MM.get(rec.get("paper_size") or "")
    if not (k and dims and long_mm):
        return
    biggest = max(dims)
    on_paper = biggest * k
    if on_paper > long_mm:
        f.add("high", "contradiction", "title_block.scale",
              f"SCALE {s} cannot be right: the {biggest:g} mm dimension would be {on_paper:g} mm long "
              f"on paper, but the {rec['paper_size']} sheet is only {long_mm} mm wide.",
              "Correct the title-block scale to the scale the views are actually drawn at.")


def check_general_tolerance(rec, f: Findings):
    gt = rec.get("general_tolerance") or {}
    if not gt:
        f.add("low", "missing_field", "title_block.general_tolerance",
              "No general tolerance block (TOLERANCES UNLESS OTHERWISE SPECIFIED) was read.")
        return
    for tier in ("without_decimal", "one_decimal", "two_decimal"):
        if not gt.get(tier):
            f.add("low", "missing_field", f"general_tolerance.{tier}",
                  f"General tolerance for {tier.replace('_', ' ').upper()} could not be read{_ocr_note(rec)}.")


def check_bom(rec, f: Findings):
    bom = rec.get("bom") or []
    tb = rec["title_block"]
    ocr = rec["text_source"] == "ocr"
    if not bom:
        if (tb.get("material") or "").upper().replace(" ", "") == "SEETABLE":
            f.add("medium", "bom_mismatch", "bom",
                  "MATERIAL says SEE TABLE but no BOM / parts table was found on the sheet.")
        return
    nums = []
    for i, row in enumerate(bom):
        loc = f"bom.row_{i + 1}"
        item = (row.get("item") or "").strip()
        if not item:
            f.add("medium", "bom_mismatch", loc,
                  f"BOM row '{row.get('description', '')}' has no item number{_ocr_note(rec)}.")
        else:
            m = re.match(r"^(\d+)", item)
            if m:
                nums.append(int(m.group(1)))
            loc = f"bom.item_{item}"
        qty = (row.get("qty") or "").strip()
        if not qty:
            f.add("high", "bom_mismatch", loc, f"BOM item {item or '?'} has no quantity{_ocr_note(rec)}.")
        elif re.fullmatch(r"0+(\.0+)?\s*\w*", qty):
            f.add("high", "bom_mismatch", loc, f"BOM item {item or '?'} has quantity {qty}.")
        if not (row.get("description") or "").strip():
            f.add("medium", "bom_mismatch", loc, f"BOM item {item or '?'} has no description.")
        ref = row.get("ref") or row.get("part_number")
        if ref and tb.get("drawing_number") and same_id(ref, tb["drawing_number"], ocr):
            f.add("high", "bom_mismatch", loc,
                  f"BOM item {item or '?'} references this drawing's own number ({ref}).")
    if nums:
        dup = sorted({n for n in nums if nums.count(n) > 1})
        if dup:
            f.add("high", "bom_mismatch", "bom", f"Duplicate BOM item number(s): {', '.join(map(str, dup))}.")
        missing = [n for n in range(1, max(nums) + 1) if n not in nums]
        if missing and len(nums) == len(bom):
            f.add("medium", "bom_mismatch", "bom",
                  f"BOM item numbers skip {', '.join(map(str, missing))}.")


def check_people(rec, f: Findings):
    tb = rec["title_block"]
    drawn = alnum(tb.get("drawn_by"))
    if not drawn:
        return
    for field in ("approved_by", "verified_by"):
        if alnum(tb.get(field)) == drawn:
            f.add("medium", "contradiction", f"title_block.{field}",
                  f"{field.split('_')[0].upper()} is the same person as DRAWN ({tb['drawn_by']}).")
    checked = [alnum(x) for x in (tb.get("checked_by") or "").split(",")]
    if drawn in checked:
        f.add("low", "contradiction", "title_block.checked_by",
              f"CHECKED includes the drafter ({tb['drawn_by']}).")


def check_weight(rec, f: Findings):
    w = rec["title_block"].get("weight")
    if w and not re.search(r"\d\s*(?:±\s*\d+(?:\.\d+)?)?\s*\(?\s*(g|kg|gm|grams?)\b", w, re.I):
        f.add("low", "missing_field", "title_block.weight", f"WEIGHT '{w}' has no unit.")


def check_reading(rec, f: Findings):
    if rec["text_source"] == "ocr":
        f.add("info", "ocr_quality", "sheet",
              f"No text layer in this PDF ({rec.get('producer') or 'unknown producer'}): read by OCR after "
              f"rotating {rec.get('rotation_deg', 0)} deg. Exporting from CAD with text kept as text "
              "(e.g. TrueType fonts instead of SHX) makes every check exact.")
    for w in rec.get("warnings") or []:
        f.add("low", "ocr_quality", "sheet", w)


ALL_CHECKS = [check_reading, check_fields, check_identity, check_revision_table, check_sheet,
              check_date, check_scale, check_general_tolerance, check_bom, check_people, check_weight]


def run_rules(rec: dict, today: Optional[_dt.date] = None) -> List[dict]:
    f = Findings()
    for chk in ALL_CHECKS:
        if chk is check_date:
            chk(rec, f, today=today)
        else:
            chk(rec, f)
    return f.items


def overall_status(findings: List[dict]) -> str:
    sev = {x["severity"] for x in findings}
    if "high" in sev:
        return "FAIL"
    if sev & {"medium", "low"}:
        return "NEEDS_REVIEW"
    return "PASS"
