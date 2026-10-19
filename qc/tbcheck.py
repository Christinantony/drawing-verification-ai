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


def _on_sheet(q, value) -> bool:
    if q.ocr:
        return True                      # OCR text is itself uncertain: take the image reading
    words = q.sheet.words
    blob = alnum(" ".join(w.text for w in words))
    return bool(alnum(value)) and alnum(value) in blob


def recheck(sheets, client, cfg, log) -> int:
    """Re-read title blocks that do not make sense. Returns how many sheets
    were re-checked."""
    from .checks import locate
    from .sheets import Block, union
    n = 0
    for q in sheets:
        why = reasons(q, cfg)
        q.tb_check = {"reasons": why, "changes": [], "kind": "drawing", "no_cell_for": [], "note": ""}
        if not why:
            continue
        n += 1
        try:
            loc = _locate_call(q, client)
            kind = loc.get("kind", "drawing")
            box = q.parts["title_block"].region
            box_wrong = not (box and loc.get("box_1_is_title_block"))
            region = _frame_area(q, loc.get("area", "bottom-right")) if box_wrong else box
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
            if not _on_sheet(q, v):
                q.tb_check["changes"].append(f"{f}: model read '{v[:40]}', not found in the sheet text - ignored")
                continue
            b = locate(q, v)
            if b:
                boxes.append(b)
            old = (tb.get(f) or "").strip()
            # keep what the code read unless it was empty, did not make sense,
            # or came from a box that was not the title block
            if (not old or _odd(f, old, q) or box_wrong) and alnum(old) != alnum(v):
                tb[f] = v
                q.tb_check["changes"].append(f"{f}: '{old[:30]}' -> '{v[:40]}'" if old else f"{f}: '{v[:40]}' (was empty)")
        read = {f for f, v in (ans.get("fields") or {}).items() if f in FIELDS and (v or "").strip()}
        if box_wrong and len(read) >= 3:
            # what the code read came from the wrong box: keep only what the
            # model confirmed
            for f in FIELDS:
                if f not in read and (tb.get(f) or "").strip():
                    q.tb_check["changes"].append(f"{f}: '{tb[f][:30]}' removed (read from the wrong box)")
                    tb[f] = ""
        q.tb_check["no_cell_for"] = [f for f in ans.get("no_cell_for") or [] if f in FIELDS and not (tb.get(f) or "").strip()]
        q.tb_check["note"] = (ans.get("makes_sense") or "")[:160]
        if len(boxes) >= 2:
            new = union(boxes)
            new = (new[0] - 6, new[1] - 6, new[2] + 6, new[3] + 6)
            if new != q.parts["title_block"].region:
                q.parts["title_block"].region = new
                q.blocks = [b for b in q.blocks if b.kind != "title_block"]
                q.blocks.append(Block("title_block", new, [], {"source": "title-block re-check"}))
                q.tb_check["changes"].append("title-block box redrawn around the values the model read")
        log(f"    {q.number or q.file}: title block re-checked ({'; '.join(why)[:110]}) -> {kind}, "
            f"{len([c for c in q.tb_check['changes'] if '->' in c or 'was empty' in c])} field(s) corrected"
            + (f", no cell for {', '.join(q.tb_check['no_cell_for'])}" if q.tb_check["no_cell_for"] else ""))
    return n
