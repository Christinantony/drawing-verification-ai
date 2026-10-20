"""Title-block re-check (part of step 3).

The reader finds the title block by its ruled cells and labels. Sometimes it
boxes the wrong thing - a document's page header, a notes table - and every
check after it reports nonsense ("title block has no DRAWING NUMBER, no
TITLE, no MATERIAL ..."). So, before the checks run, the code asks: does what
was read as the title block make sense?

    - required fields empty (3 or more), or no drawing number
    - values that cannot be what their label says (a scale that is not
      'n:n', a date that is not a date, a sheet that is not '1/1', ...)
    - the layout judge said the title-block box is something else, or saw a
      title block that has no box

If it does not make sense, the model looks at the sheet:

    1. on a small image of the whole sheet: is this a drawing or a document,
       and where is its title block (or document header)?
    2. on a sharp image of that area: read each field exactly as written,
       and name the fields this title block has no cell for.

Every value the model reads must be found in the sheet's own text (on a
text-layer sheet); a value that is not there is ignored. Confirmed values
replace the empty or nonsensical ones, and the title-block box is redrawn
around them. Fields the template has no cell for are reported once, as
please look, instead of as "missing".
"""
from __future__ import annotations

import re
from typing import List

from sheetreader.normalize import alnum

from .model import ModelError

FIELDS = ["drawing_number", "title", "material", "finish", "weight", "scale", "size", "sheet", "revision",
          "date", "drawn_by", "checked_by", "approved_by", "verified_by", "next_assy"]
LABEL = {"drawing_number": "DRAWING No.", "drawn_by": "DRAWN", "checked_by": "CHECKED",
         "approved_by": "APPROVED", "verified_by": "VERIFIED", "next_assy": "NEXT ASSY."}
AREAS = ["top-left", "top", "top-right", "left", "middle", "right", "bottom-left", "bottom", "bottom-right"]
SYSTEM = ("You read the title blocks of engineering drawings and the headers of engineering documents. "
          "Answer only in JSON. Copy text exactly as written; never invent a value.")


# ---------------------------------------------------------------------------
# Does the title block make sense?
# ---------------------------------------------------------------------------
_CHECKS = {
    "scale": re.compile(r"^\s*(\d+(\.\d+)?\s*[:/]\s*\d+(\.\d+)?|NTS|NONE|N\.?T\.?S\.?|AS SHOWN|SEE .*|-)\s*$", re.I),
    "size": re.compile(r"^\s*(SIZE\s*:?\s*)?A[0-4]\s*$", re.I),
    "sheet": re.compile(r"^\s*(SHEET\s*:?\s*)?\d+\s*(/|OF)\s*\d+\s*$", re.I),
    "revision": re.compile(r"^\s*(REV\.?\s*)?([0-9]{1,2}|[A-Z]{1,2})\s*$", re.I),
    "date": re.compile(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"),
    "weight": re.compile(r"\d|SEE|N/?A|-", re.I),
}


def _odd(field, value, q) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    rx = _CHECKS.get(field)
    if rx is not None and not rx.search(v):
        return True
    if field == "drawing_number":
        if not re.search(r"\d", v) or len(alnum(v)) < 5:
            return True
    if field == "title" and (len(re.sub(r"[^A-Za-z]", "", v)) < 3 or v.upper().strip(" :") in ("TITLE", "DRAWING")):
        return True
    if field in ("drawn_by", "checked_by", "approved_by", "verified_by") and len(v) > 25:
        return True
    return False


def reasons(q, cfg) -> List[str]:
    """Why the title block of this sheet does not make sense (empty = fine)."""
    tb = q.record["title_block"]
    out = []
    req = [f for f in cfg["required_fields"]]
    empty = [f for f in req if not (tb.get(f) or "").strip()]
    odd = [f for f in FIELDS if _odd(f, tb.get(f), q)]
    if not (tb.get("drawing_number") or "").strip():
        out.append("no drawing number")
    if len(empty) >= 3:
        out.append(f"{len(empty)} required fields empty ({', '.join(empty[:6])})")
    if odd:
        out.append("values that do not fit their label: " + ", ".join(f"{f}='{(tb.get(f) or '')[:30]}'" for f in odd))
    for d in (getattr(q, "layout", None) or {}).get("disagree", []):
        if "(title_block)" in d or "unboxed title_block" in d:
            out.append("layout check: " + d[:90])
    if q.parts["title_block"].region is None:
        out.append("no title block found")
    return out


# ---------------------------------------------------------------------------
# Re-check with the model
# ---------------------------------------------------------------------------
def _frame_area(q, area, grow=0.18):
    fx0, fy0, fx1, fy1 = q.frame
    i = AREAS.index(area)
    r, c = divmod(i, 3)
    w, h = (fx1 - fx0) / 3, (fy1 - fy0) / 3
    x0, y0, x1, y1 = fx0 + c * w, fy0 + r * h, fx0 + (c + 1) * w, fy0 + (r + 1) * h
    gx, gy = w * grow, h * grow
    return (max(fx0, x0 - gx), max(fy0, y0 - gy), min(fx1, x1 + gx), min(fy1, y1 + gy))


def _locate_call(q, client):
    from .layout import _thumb
    box = q.parts["title_block"].region
    img, _ = _thumb(q, [box] if box else [], ["title block?"] if box else None)
    schema = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["drawing", "document", "other"]},
        "box_1_is_title_block": {"type": "boolean"},
        "area": {"type": "string", "enum": AREAS}},
        "required": ["kind", "box_1_is_title_block", "area"]}
    user = ("This is a whole sheet. Is it an engineering drawing (views, with a title block) or a document "
            "(text pages: a list, a procedure, a manufacturing document, with a header or title block)? "
            + ("Box 1 is what the program read as the title block: is it really the title block (the block "
               "with the drawing number, title, revision, date and signatures)? " if box else
               "The program found no title block. ")
            + "In which area of the sheet is the real title block, or the document's header with its number?")
    return client.ask(SYSTEM, user, schema, images=[img], max_tokens=60)


def _read_call(q, client, region, kind):
    from PIL import Image
    crop = q.crop(region, dpi=200, pad=4)
    img = Image.fromarray(crop)
    if img.width > 1600:
        img = img.resize((1600, int(img.height * 1600 / img.width)))
    props = {f: {"type": "string"} for f in FIELDS}
    schema = {"type": "object", "properties": {
        "fields": {"type": "object", "properties": props},
        "no_cell_for": {"type": "array", "items": {"type": "string", "enum": FIELDS}},
        "makes_sense": {"type": "string"}},
        "required": ["fields", "no_cell_for"]}
    user = (f"This is the {'header of an engineering document' if kind == 'document' else 'title block of an engineering drawing'}. "
            "Read every field exactly as written in its cell (the value, not the label): drawing_number "
            "(drawing or document No.), title, material, finish, weight, scale, size (A4, A3...), sheet "
            "(e.g. 1/1), revision (e.g. 00), date, drawn_by, checked_by, approved_by, verified_by, next_assy. "
            "Leave a field empty if its cell is empty. In no_cell_for, list the fields this title block has "
            "no cell or label for at all. In makes_sense, say in a few words anything odd.")
    return client.ask(SYSTEM, user, schema, images=[img], max_tokens=420)


def compare(q, value, region=None):
    """How the sheet's own text compares with a value the model read.
    Returns (verdict, sheet_text):
        'agree'      the same text is on the sheet
        'ocr_unsure' OCR read something similar there with low confidence
        'conflict'   OCR read something different there, confidently
        'absent'     nothing like it on the sheet"""
    import difflib
    from sheetreader.cells import reading_order
    want = alnum(value)
    if not want:
        return "absent", ""
    words = [w for w in q.sheet.words
             if region is None or (region[0] - 4 <= w.xc <= region[2] + 4 and region[1] - 4 <= w.yc <= region[3] + 4)]
    if not q.ocr:
        blob = alnum(" ".join(w.text for w in q.sheet.words))
        return ("agree", value) if want in blob else ("absent", "")
    n = max(1, len(value.split()))
    best = (0.0, None)
    for line in reading_order(words):
        for i in range(len(line)):
            for k in (n - 1, n, n + 1):
                if k < 1 or i + k > len(line):
                    continue
                ws = line[i:i + k]
                r = difflib.SequenceMatcher(None, want, alnum(" ".join(w.text for w in ws))).ratio()
                if r > best[0]:
                    best = (r, ws)
    r, ws = best
    if not ws or r < 0.5:
        return "absent", ""
    text = " ".join(w.text for w in ws)
    if r >= 0.8:
        return "agree", text
    return ("ocr_unsure", text) if min(w.conf for w in ws) < 60 else ("conflict", text)


def recheck(sheets, client, cfg, log) -> int:
    """Re-read title blocks whose contents do not make sense. Returns how
    many sheets were re-checked.

    The model's reading only fills fields that are empty or nonsense; it
    never deletes what the code read, and the box only grows. A layout-check
    opinion from the small whole-sheet image alone does not start a re-check:
    at that size the model cannot read the text."""
    from .checks import locate
    from .sheets import Block, union
    n = 0
    for q in sheets:
        why = reasons(q, cfg)
        q.tb_check = {"reasons": why, "changes": [], "kind": "drawing", "no_cell_for": [], "note": "",
                      "conflicts": []}
        if not [w for w in why if not w.startswith("layout check")]:
            continue
        n += 1
        box = q.parts["title_block"].region
        try:
            loc = _locate_call(q, client)
            kind = loc.get("kind", "drawing")
            area = _frame_area(q, loc.get("area", "bottom-right"))
            # read the code's box if the model agrees it is the title block,
            # else the area it points to (always including the code's box
            # when they overlap, so nothing is cut off)
            if box and loc.get("box_1_is_title_block"):
                region = box
            elif box and not (area[2] < box[0] or box[2] < area[0] or area[3] < box[1] or box[3] < area[1]):
                region = union([box, area])
            else:
                region = area
            ans = _read_call(q, client, region, kind)
        except ModelError as e:
            log(f"    title-block re-check not done for {q.number or q.file}: {e}")
            continue
        q.tb_check["kind"] = kind
        q.doc_kind = kind
        tb = q.record["title_block"]
        boxes = []
        for f, v in (ans.get("fields") or {}).items():
            v = (v or "").strip()
            if f not in FIELDS or not v:
                continue
            old = (tb.get(f) or "").strip()
            verdict, seen = compare(q, v, region)
            if verdict == "absent" and not q.ocr:
                q.tb_check["changes"].append(f"{f}: model read '{v[:40]}', not found in the sheet text - ignored")
                continue
            if verdict == "conflict":
                if not old or _odd(f, old, q):
                    q.tb_check["conflicts"].append({"field": f, "ocr": seen, "model": v})
                continue
            b = locate(q, seen if verdict != "absent" else v)
            if b:
                boxes.append(b)
            if (not old or _odd(f, old, q)) and alnum(old) != alnum(v):
                tb[f] = v
                how = {"agree": "", "ocr_unsure": " (read on the image; OCR was unsure)",
                       "absent": " (read on the image; OCR has nothing there)"}[verdict]
                q.tb_check["changes"].append((f"{f}: '{old[:30]}' -> '{v[:40]}'" if old else
                                              f"{f}: '{v[:40]}' (was empty)") + how)
        q.tb_check["no_cell_for"] = [f for f in ans.get("no_cell_for") or [] if f in FIELDS and not (tb.get(f) or "").strip()]
        q.tb_check["note"] = (ans.get("makes_sense") or "")[:160]
        if boxes:
            # grow the box to take in what the model found; never shrink it
            new = union(boxes + ([box] if box else []))
            if box is None or new != box:
                new = (new[0] - 4, new[1] - 4, new[2] + 4, new[3] + 4) if box is None else new
                q.parts["title_block"].region = new
                q.blocks = [b for b in q.blocks if b.kind != "title_block"]
                q.blocks.append(Block("title_block", new, [], {"source": "title-block re-check"}))
                q.tb_check["changes"].append("title-block box " + ("found" if box is None else "enlarged")
                                             + " from the values the model read")
        log(f"    {q.number or q.file}: title block re-checked ({'; '.join(why)[:110]}) -> {kind}, "
            f"{len([c for c in q.tb_check['changes'] if '->' in c or 'was empty' in c])} field(s) filled"
            + (f", {len(q.tb_check['conflicts'])} for you to check" if q.tb_check["conflicts"] else "")
            + (f", no cell for {', '.join(q.tb_check['no_cell_for'])}" if q.tb_check["no_cell_for"] else ""))
    return n
