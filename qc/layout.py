"""Layout judge (part of station 3): the code proposes, the model judges.

The code has already boxed the parts of the sheet. Each box is drawn with a
number on a small image of the whole sheet, and the model answers:

  * what each numbered box is (BOM, notes, title block, callout, ...)
  * whether there is an element on the sheet with no box (and roughly where)

Unplaced text the model recognises is re-labelled (a notes block, a table,
an annotation in a view). A missing element is boxed by the code from the
free words in that area, so the edges stay exact. Disagreements about the
code's structured boxes (title block, BOM) are reported, not applied.

`python -m qc layout-test FILE.pdf` compares this with letting the model draw
its own boxes on the plain image.
"""
from __future__ import annotations

import html
import time
from pathlib import Path
from typing import List

from .model import ModelError
from .sheets import add_block, union

KINDS = ["title_block", "bom", "revision_table", "notes", "callout", "view_label", "legend", "table",
         "annotation", "other"]
AREAS = ["top-left", "top", "top-right", "left", "middle", "right", "bottom-left", "bottom", "bottom-right"]
THUMB_W = 1400
CODE_KIND = {"title_block": "title_block", "bom": "bom", "revision": "revision_table", "notes": "notes",
             "callout": "callout", "view_label": "view_label", "legend": "legend", "unplaced": "other",
             "table": "table", "annotation": "annotation"}

SYSTEM = ("You look at an engineering drawing sheet. Numbered boxes mark text elements found by a program. "
          "Classify each numbered box and report text elements that have no box. Ignore the drawing views "
          "themselves (geometry, dimensions). Answer only in the JSON format asked for.")


def _thumb(q, boxes=None, labels=None):
    from PIL import Image, ImageDraw
    img = q.image(110)
    k = 110 / 72.0
    pil = Image.fromarray(img).convert("RGB")
    scale = THUMB_W / pil.width if pil.width > THUMB_W else 1.0
    if scale != 1.0:
        pil = pil.resize((int(pil.width * scale), int(pil.height * scale)))
    d = ImageDraw.Draw(pil)
    for i, b in enumerate(boxes or [], 1):
        x0, y0, x1, y1 = [v * k * scale for v in b]
        d.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=(220, 30, 30), width=2)
        tag = str(i) if not labels else f"{i}:{labels[i - 1]}"
        tw = 8 * len(tag) + 6
        d.rectangle([x0 - 2, y0 - 16, x0 - 2 + tw, y0 - 2], fill=(220, 30, 30))
        d.text((x0 + 1, y0 - 15), tag, fill=(255, 255, 255))
    return pil, k * scale


def _candidates(q):
    return [b for b in q.blocks if b.kind != "title_block" or True]


def _area_box(q, area):
    fx0, fy0, fx1, fy1 = q.frame
    i = AREAS.index(area)
    r, c = divmod(i, 3)
    w, h = (fx1 - fx0) / 3, (fy1 - fy0) / 3
    return (fx0 + c * w, fy0 + r * h, fx0 + (c + 1) * w, fy0 + (r + 1) * h)


def ask_layout(q, client):
    blocks = _candidates(q)
    img, _ = _thumb(q, [b.bbox for b in blocks])
    lines = "\n".join(f"{i}. program says {CODE_KIND.get(b.kind, b.kind)}: \"{(b.text or b.kind).replace(chr(10), ' / ')[:70]}\""
                      for i, b in enumerate(blocks, 1))
    schema = {"type": "object", "properties": {
        "boxes": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"}, "kind": {"type": "string", "enum": KINDS}}, "required": ["n", "kind"]}},
        "missing": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": KINDS}, "area": {"type": "string", "enum": AREAS},
            "hint": {"type": "string"}}, "required": ["kind", "area"]}}},
        "required": ["boxes", "missing"]}
    user = ("Numbered boxes on this sheet, with the program's guess and the start of their text:\n" + lines +
            "\n\nFor every box give its kind. Then list any block of text (notes, a table, a legend) that has "
            "NO box, with the area of the sheet it is in. Kinds: " + ", ".join(KINDS) +
            ". 'annotation' = a label or leader text inside a view.")
    t = time.monotonic()
    ans = client.ask(SYSTEM, user, schema, images=[img], max_tokens=60 + 18 * len(blocks) + 120)
    return blocks, ans, time.monotonic() - t


def judge_layout(sheets, client, log) -> None:
    for q in sheets:
        q.layout = {"changes": [], "disagree": [], "added": [], "seconds": 0, "blocks": []}
        try:
            blocks, ans, secs = ask_layout(q, client)
        except ModelError as e:
            log(f"    layout check not done for {q.number}: {e}")
            continue
        q.layout["seconds"] = round(secs, 1)
        seen = set()
        for a in ans.get("boxes", []):
            n = a.get("n", 0) - 1
            if not 0 <= n < len(blocks) or n in seen:
                continue
            seen.add(n)
            b, kind = blocks[n], a["kind"]
            code_kind = CODE_KIND.get(b.kind, b.kind)
            if kind == code_kind:
                continue
            if b.kind == "unplaced" and kind in ("notes", "table", "legend", "view_label", "callout", "annotation"):
                if kind == "notes":
                    lines = b.lines
                    q.blocks.remove(b)
                    nb = add_block(q, union(l.bbox for l in lines), "notes", "layout judge")
                    if nb is None:
                        q.blocks.append(b)
                        continue
                else:
                    b.kind = kind
                    b.meta["source"] = "layout judge"
                q.layout["changes"].append(f"'{b.text[:50]}' is {kind.replace('_', ' ')}")
            elif b.kind != "unplaced":
                q.layout["disagree"].append(f"box {n + 1} ({code_kind}) looks like {kind} to the model: '{(b.text or '')[:50]}'")
                q.layout["blocks"].append((b, kind))
        for m in ans.get("missing", [])[:4]:
            if m["kind"] in ("other", "title_block", "bom", "revision_table"):
                if m["kind"] != "other":
                    q.layout["disagree"].append(f"model sees an unboxed {m['kind']} at {m['area']}: {m.get('hint', '')}")
                continue
            nb = add_block(q, _area_box(q, m["area"]), "notes" if m["kind"] == "notes" else m["kind"], "layout judge")
            if nb is not None:
                q.layout["added"].append(f"{m['kind']} at {m['area']}: '{nb.text[:60]}'")


# ---------------------------------------------------------------------------
def layout_test(pdf, out_dir: Path, client, log) -> Path:
    """Two ways on one sheet: (A) the model draws its own boxes on the plain
    image, (B) the model judges the program's numbered boxes."""
    import base64
    import io
    from PIL import ImageDraw
    from .sheets import read_pdf
    out_dir.mkdir(parents=True, exist_ok=True)
    q = read_pdf(pdf)[0]
    plain, k = _thumb(q)
    schema_a = {"type": "object", "properties": {"elements": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": KINDS}, "box": {"type": "array", "items": {"type": "integer"}}},
        "required": ["kind", "box"]}}}, "required": ["elements"]}
    t = time.monotonic()
    try:
        a = client.ask(SYSTEM.replace("Numbered boxes mark text elements found by a program. ", ""),
                       "Find every text element on this sheet (title block, BOM, revision table, notes, callouts, "
                       "view labels, legends, tables). For each give its kind and its box as [x0, y0, x1, y1] in "
                       "0-1000 coordinates of the image (0,0 top-left).", schema_a, images=[plain], max_tokens=900)
    except ModelError as e:
        a = {"elements": [], "error": str(e)}
    ta = time.monotonic() - t
    img_a = plain.copy()
    d = ImageDraw.Draw(img_a)
    for el in a.get("elements", []):
        b = el.get("box") or []
        if len(b) == 4:
            x0, y0, x1, y1 = [v / 1000 * (img_a.width if i % 2 == 0 else img_a.height) for i, v in enumerate(b)]
            d.rectangle([x0, y0, x1, y1], outline=(30, 90, 220), width=3)
            d.text((x0 + 3, y0 + 2), el.get("kind", ""), fill=(30, 90, 220))
    try:
        blocks, b_ans, tb = ask_layout(q, client)
        kinds = {x["n"]: x["kind"] for x in b_ans.get("boxes", [])}
        img_b, _ = _thumb(q, [b.bbox for b in blocks], [kinds.get(i, "?") for i in range(1, len(blocks) + 1)])
    except ModelError as e:
        b_ans, tb = {"error": str(e)}, 0
        img_b, _ = _thumb(q, [b.bbox for b in q.blocks])

    def b64(im):
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    e = html.escape
    page = f"""<!doctype html><meta charset="utf-8"><title>Layout test</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:20px}} img{{max-width:100%;border:1px solid #ccc}}
pre{{background:#f6f6f6;padding:8px;white-space:pre-wrap;font-size:12px}}</style>
<h1>Layout test: {e(q.file)}</h1>
<p>Model: {e(client.name)} &middot; read by {'OCR' if q.ocr else 'text layer'}</p>
<h2>A. Model draws its own boxes ({ta:.0f} s)</h2><img src="data:image/png;base64,{b64(img_a)}">
<pre>{e(str(a))}</pre>
<h2>B. Model judges the program's numbered boxes ({tb:.0f} s)</h2><img src="data:image/png;base64,{b64(img_b)}">
<pre>{e(str(b_ans))}</pre>"""
    path = out_dir / (Path(pdf).stem + "_layout_test.html")
    path.write_text(page, encoding="utf-8")
    return path
