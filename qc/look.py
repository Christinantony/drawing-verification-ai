"""The model looks closely (sharp images of one spot, never the whole sheet).

Gemma reads a sharp crop of one block well; on a whole-sheet thumbnail it
cannot read the text and guesses from shapes. So the model only *looks* at
sharp crops, and what it reads is checked against the sheet's own text.

read_tables   A block the code read as notes that the layout check says is a
              table, where the code found no ruled grid (a scan with faint
              lines, a table without borders): the model reads it row by
              row, cell by cell. Its reading is used only if most cells match
              the sheet's text (exactly on a text layer, closely on OCR).

second_look   Before a note is reported as incomplete or unclear, a legend as
              missing, or a model observation is reported, the model looks at
              a sharp image of that spot (with the table row or the lines
              around it) and confirms or withdraws it. On a scanned sheet it
              also reads the text on the image, so a finding that only exists
              because of an OCR misread is cleared.
"""
from __future__ import annotations

import difflib
from typing import List

from sheetreader.normalize import alnum

from .model import ModelError

SYSTEM = ("You check engineering drawings by looking at sharp images of small parts of them. "
          "Answer only in JSON. Copy text exactly as written on the image.")
LOOK_CODES = ("D1", "D4", "D5", "M1", "C4")


def _img(q, box, dpi=170, max_w=1500):
    from PIL import Image
    im = Image.fromarray(q.crop(box, dpi=dpi, pad=4))
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    return im


def _grow(box, q, d):
    fx0, fy0, fx1, fy1 = q.frame
    return (max(fx0, box[0] - d), max(fy0, box[1] - d), min(fx1, box[2] + d), min(fy1, box[3] + d))


def _similar(a, b) -> float:
    a, b = alnum(a), alnum(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
def read_tables(sheets, client, log) -> int:
    """Tables the code could not read from ruled lines; see module doc."""
    from .sheets import Block, table_spec
    n = 0
    for q in sheets:
        for b, kind in (getattr(q, "layout", None) or {}).get("blocks", []):
            if b.kind != "notes" or kind != "table" or b not in q.blocks:
                continue
            if any(t.meta.get("rows") and _overlap(t.bbox, b.bbox) for t in q.blocks_of("table")):
                continue
            schema = {"type": "object", "properties": {
                "is_table": {"type": "boolean"},
                "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
                "required": ["is_table", "rows"]}
            try:
                ans = client.ask(SYSTEM, "Is this a table (rows and columns of cells) or a list of numbered notes? "
                                 "If it is a table, give every row as a list of its cells, left to right; a cell "
                                 "written on two lines is one cell.", schema, images=[_img(q, b.bbox, dpi=200)],
                                 max_tokens=500)
            except ModelError as e:
                log(f"    table not read for {q.number}: {e}")
                continue
            rows = [[(c or "").strip() for c in r] for r in ans.get("rows") or [] if any((c or "").strip() for c in r)]
            if not ans.get("is_table") or len(rows) < 2:
                continue
            text = b.text
            cells = [c for r in rows for c in r if c]
            ok = sum(_similar(c, text) >= (0.75 if q.ocr else 1.0) for c in cells)
            if not cells or ok < 0.7 * len(cells):
                log(f"    {q.number}: the model read a table, but only {ok} of {len(cells)} cells match the "
                    "sheet's text - kept as notes")
                continue
            # replace the notes of this block with the table
            idx = q.blocks.index(b)
            q.notes = [x for x in q.notes if not _overlap(x.bbox, b.bbox)]
            from .sheets import Line
            t = Block("table", b.bbox, [Line(" | ".join(c for c in r if c), b.bbox, [], 100.0) for r in rows],
                      {"rows": rows, "spec": table_spec(rows), "source": "read on the image by the model"})
            q.blocks[idx] = t
            q.record.setdefault("tables", []).append({"rows": rows, "spec": t.meta["spec"]})
            q.record["notes"] = [x.text for x in q.notes]
            n += 1
            log(f"    {q.number}: block read as a table on the image ({len(rows)} rows, {ok} of {len(cells)} "
                "cells match the sheet's text)")
    return n


def _overlap(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


# ---------------------------------------------------------------------------
def _spot(f):
    """The area to look at for a finding: its box with the lines around it,
    or the whole table row / table it sits in."""
    from .checks import locate
    q = f.sheet
    box = f.bbox or (locate(q, f.quote) if f.quote and f.quote != "*" else None)
    if f.code == "D5" or (f.code == "D1" and not box):
        blocks = [b.bbox for b in q.blocks if b.kind in ("notes", "legend", "table", "unplaced")]
        return blocks[:4]
    if not box:
        return []
    for t in q.blocks_of("table"):
        if _overlap(t.bbox, box):
            return [_grow(t.bbox, q, 6)]
    for b in q.blocks_of("notes"):
        if _overlap(b.bbox, box):
            return [_grow(b.bbox, q, 6)]
    return [_grow(box, q, 30)]


def second_look(findings, client, cfg, log, max_looks=40) -> int:
    """See module doc. Returns how many findings were looked at."""
    todo = [f for f in findings if f.sheet is not None and f.status in ("fail", "check")
            and f.code in (cfg.get("second_look_codes") or LOOK_CODES)
            and not any(h.startswith("second look") for h in f.history)]
    n = cleared = 0
    for f in todo[:max_looks]:
        boxes = _spot(f)
        if not boxes:
            continue
        q = f.sheet
        legend = "; ".join(f"'{k}' = {v}" for k, v in (cfg.get("legends") or {}).items())
        schema = {"type": "object", "properties": {
            "verdict": {"type": "string", "enum": ["confirmed", "not_a_problem", "cannot_tell"]},
            "text": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["verdict", "reason"]}
        user = (f"A reviewer wants to report this on the drawing: \"{f.message}\""
                + (f" (text read from the sheet: \"{f.quote[:160]}\")" if f.quote and f.quote != "*" else "")
                + ". Look at the image(s) of that part of the drawing. Is the problem really there? Look at the "
                "whole picture: the next cell of a table row, the line above or below, a legend or symbol "
                "nearby" + (f" (legends used on these drawings: {legend})" if legend else "") + ". "
                + ("This sheet was read by OCR, so the text above may be misread: write in 'text' exactly what "
                   "the image shows. " if q.ocr else "")
                + "Answer confirmed, not_a_problem or cannot_tell, with a reason of at most 15 words.")
        try:
            ans = client.ask(SYSTEM, user, schema, images=[_img(q, b) for b in boxes], max_tokens=140)
        except ModelError as e:
            log(f"    second look not done for {q.number} {f.code}: {e}")
            continue
        n += 1
        v, why = ans.get("verdict", "cannot_tell"), (ans.get("reason") or "").strip()[:120]
        seen = (ans.get("text") or "").strip()
        misread = q.ocr and seen and f.quote and _similar(seen, f.quote) < 0.9
        if v == "not_a_problem":
            f.status = "cleared"
            cleared += 1
            f.history.append(f"second look on the image: not a problem - {why}"
                             + (f" (OCR read '{f.quote[:60]}', the image shows '{seen[:60]}')" if misread else ""))
        elif v == "confirmed":
            f.history.append(f"second look on the image: confirmed - {why}")
            if misread:
                f.message += f" (the image shows: '{seen[:80]}')"
        else:
            f.history.append(f"second look on the image: cannot tell - {why}")
    if n:
        log(f"    second look: {n} item(s) looked at on the image, {cleared} cleared")
    return n
