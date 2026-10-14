"""Station 6: the doubt loop - a small review board inside the run.

For every finding the rules could not settle (status "doubt"):

    1. the model picks ONE tool from a fixed menu (no model: a fixed order)
    2. the code runs the tool
    3. the rules check again with what the tool found
    4. settled -> a finding, or cleared; still unsure after `passes` -> "check by hand"

Tools (only the ones that make sense for the item are offered):

    reread       re-OCR just that box from the PDF at high resolution (scanned sheets)
    read_image   the model reads an image of just that box (literal text only, never the views)
    search_set   find the same or similar text on the other sheets
    open_linked  look at the linked drawing (parent / child / referenced sheet)
    ask_person   stop and hand it to you
"""
from __future__ import annotations

import re
from typing import Optional

from .model import ModelError
from .sheets import REREAD_DPI, union

SYSTEM = ("You decide the next step for one unsure item from a drawing check. You see a short "
          "description and the tools you may use. Pick exactly one tool. Prefer reading the text again "
          "when the doubt is about what is written; prefer open_linked when it is about another drawing.")


def _tools_for(f, client, cfg) -> list:
    tools = []
    box = _box(f)
    if box and f.sheet is not None and f.sheet.ocr:
        tools.append("reread")
    if box and f.sheet is not None and cfg["model"].get("vision") and getattr(client, "vision", False):
        tools.append("read_image")
    if f.quote:
        tools.append("search_set")
    if f.related:
        tools.append("open_linked")
    tools.append("ask_person")
    return tools


def _box(f) -> Optional[tuple]:
    if f.doubt.get("kind") == "tb_field" and f.sheet is not None:
        lab = _label_box(f.sheet, f.doubt["label"])
        if lab:
            return (lab[0] - 2, lab[1] - 2, lab[2] + 90, lab[3] + 22)
    if f.doubt.get("kind") == "field" and f.bbox:
        bom = [b for b in f.sheet.blocks_of("bom")] if f.sheet is not None else []
        if bom:
            if f.doubt.get("item_cell"):
                # just the item-number cell, left of the description
                return (bom[0].bbox[0] + 1, f.bbox[1] - 1, f.bbox[0] - 2, f.bbox[3] + 1)
            return (bom[0].bbox[0], f.bbox[1] - 1, bom[0].bbox[2], f.bbox[3] + 1)
    return f.bbox


def _label_box(sheet, label):
    tb = sheet.parts["title_block"].region
    for w in sheet.sheet.words:
        if label.split()[0] in w.text.upper() and tb and tb[0] <= w.xc <= tb[2] and tb[1] <= w.yc <= tb[3]:
            return w.bbox
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def tool_reread(f) -> dict:
    from PIL import Image
    from sheetreader.loader import _tesseract
    import numpy as np
    box = _box(f)
    crop = f.sheet.crop(box, dpi=REREAD_DPI, pad=3)
    if crop.size == 0:
        return {"text": ""}
    pad = 30
    padded = np.full((crop.shape[0] + 2 * pad, crop.shape[1] + 2 * pad), 255, np.uint8)
    padded[pad:-pad, pad:-pad] = crop
    cfg = "--psm 7 -c tessedit_char_whitelist=0123456789" if f.doubt.get("item_cell") else "--psm 6"
    txt = _tesseract().image_to_string(Image.fromarray(padded), config=cfg)
    return {"text": " ".join(txt.split())}


def tool_read_image(f, client) -> dict:
    box = _box(f)
    crop = f.sheet.crop(box, dpi=REREAD_DPI // 2, pad=4)
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    ans = client.ask("You transcribe text from a small crop of an engineering drawing exactly as written. "
                     "Do not correct spelling. Keep symbols such as Ø ± °.",
                     "Write exactly the text in this image.", schema, images=[crop], max_tokens=120)
    return {"text": " ".join(str(ans.get("text", "")).split())}


def tool_search_set(f, sheets) -> dict:
    import difflib
    from sheetreader.normalize import alnum
    want = alnum(f.quote)
    hits = []
    for s in sheets:
        if s is f.sheet:
            continue
        for w_ in [l.text for b in s.blocks for l in b.lines] + [n.text for n in s.notes] + \
                [r.get("ref") or "" for r in s.record.get("bom") or []] + [v or "" for v in s.record["title_block"].values()]:
            r = difflib.SequenceMatcher(None, want, alnum(w_)).ratio() if want else 0
            if r >= 0.75:
                hits.append((round(r, 2), s.number, w_))
    hits.sort(reverse=True)
    return {"matches": hits[:3]}


def tool_open_linked(f) -> dict:
    out = []
    for s in f.related[:2]:
        tb = s.record["title_block"]
        out.append({"drawing": s.number, "title": tb.get("title"), "next_assy": tb.get("next_assy"),
                    "bom": [f"{r.get('item')}: {r.get('description')} / {r.get('ref')}" for r in (s.record.get("bom") or [])][:15]})
    return {"linked": out}


# ---------------------------------------------------------------------------
# Re-checks: did the tool settle it?
# ---------------------------------------------------------------------------
def recheck(f, tool, obs, smap) -> str:
    """'cleared' | 'confirmed' | 'doubt'"""
    kind = f.doubt.get("kind")
    text = obs.get("text") if tool in ("reread", "read_image") else None
    if text is not None:
        f.history.append(f"{tool}: '{text}'")
        if not text.strip():
            return "doubt"
        if kind == "ref":
            from sheetreader.cells import normalize_ocr
            from .setmap import Ref, find_numbers
            for _, _, prefix, num, ext, _p in smap.mentions(normalize_ocr(text)):
                r = Ref("drawing", num, f.sheet, f.where, ocr=bool(f.sheet is not None and f.sheet.ocr), number=num)
                smap._resolve(r)
                if r.status == "ok":
                    f.message += f" Re-read as '{num}': matches {r.targets[0].number}."
                    return "cleared"
                if r.status == "styled":
                    f.message = f"{f.where}: drawing number {r.detail} (confirmed by re-reading)."
                    f.severity = "must"
                    return "confirmed"
            return "doubt"
        if kind == "tb_field":
            lab = f.doubt["label"]
            m = re.search(rf"{lab}\W*\s*(\S.*)", text, re.I)
            if m and re.search(r"[A-Za-z0-9]", m.group(1)):
                f.message += f" Re-read shows '{m.group(0).strip()}' - the field is filled."
                return "cleared"
            return "doubt"
        if kind == "field":
            if re.match(r"\s*\(?\d{1,3}\)?([.\s]|$)", text):
                f.message += f" Re-read shows the row as '{text}' - the item number is there."
                return "cleared"
            return "doubt"
        if kind == "word":
            word = f.doubt["text"]
            if text.lower().find(word.lower()) >= 0 and len(word) > 2:
                f.message = f"OCR read '{word}' twice; please check the spelling."
                return "confirmed"
            return "doubt"
        return "doubt"
    if tool == "search_set":
        hits = obs.get("matches") or []
        f.history.append("search_set: " + ("; ".join(f"{n}: '{t}'" for _, n, t in hits) or "nothing similar"))
        return "doubt"
    if tool == "open_linked":
        f.history.append("open_linked: " + "; ".join(f"{x['drawing']} '{x['title']}'" for x in obs.get("linked", [])))
        return "doubt"
    return "doubt"


# ---------------------------------------------------------------------------
def run(findings, sheets, smap, client, cfg, log) -> None:
    passes = int(cfg["model"].get("doubt_passes", 2))
    for f in [x for x in findings if x.status == "doubt"]:
        used = []
        for p in range(passes):
            tools = [t for t in _tools_for(f, client, cfg) if t not in used or t == "ask_person"]
            tool = _choose(f, tools, used, client, log)
            used.append(tool)
            if tool == "ask_person":
                f.history.append("handed to you")
                break
            try:
                if tool == "reread":
                    obs = tool_reread(f)
                elif tool == "read_image":
                    obs = tool_read_image(f, client)
                elif tool == "search_set":
                    obs = tool_search_set(f, sheets)
                else:
                    obs = tool_open_linked(f)
            except (ModelError, Exception) as e:
                f.history.append(f"{tool} failed: {e}")
                continue
            verdict = recheck(f, tool, obs, smap)
            if verdict == "cleared":
                f.status = "cleared"
                break
            if verdict == "confirmed":
                f.status = "fail"
                break
        if f.status == "doubt":
            f.status = "check"   # check by hand


def _choose(f, tools, used, client, log) -> str:
    if len(tools) == 1:
        return tools[0]
    if client.name == "none":
        # Fixed order without a model: read again, then hand over.
        for t in ("reread", "ask_person"):
            if t in tools:
                return t
        return tools[-1]
    schema = {"type": "object", "properties": {"tool": {"type": "string", "enum": tools},
                                               "reason": {"type": "string"}}, "required": ["tool"]}
    desc = (f"Check: {f.name}. Finding: {f.message} Text: \"{f.quote}\". Sheet read by "
            f"{'OCR' if f.sheet is not None and f.sheet.ocr else 'its text layer'}."
            + (f" Already tried: {', '.join(used)}." if used else ""))
    try:
        ans = client.ask(SYSTEM, desc + "\nTools: " + ", ".join(tools), schema, max_tokens=60)
        f.history.append(f"model chose {ans['tool']}: {ans.get('reason', '')}")
        return ans["tool"]
    except (ModelError, KeyError) as e:
        log(f"tool choice failed ({e}); using the fixed order")
        return next((t for t in ("reread", "read_image", "ask_person") if t in tools), tools[-1])
