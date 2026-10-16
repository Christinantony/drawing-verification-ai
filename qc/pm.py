"""± values whose sign was drawn as a graphic (part of step 3).

SolidWorks draws the ± of '135 ± 10 g' as a graphic, so the text layer only
holds '135 10 g'. Before any check or model judgement sees the text, every
such value is found, and the model looks at an image of exactly that spot to
confirm the ±:

    yes      -> the text becomes '135±10 g' everywhere (title block, notes, BOM)
    no       -> a finding: the value is written without ± (D6)
    unclear  -> read as ±, and the report says it was not confirmed

Without a model that can read images, the value is read as ± and the report
says so.
"""
from __future__ import annotations

import re
from typing import List

from .model import ModelError

# number, gap, smaller number, unit: '135 10 g', '3 1g', '1.5 0.5g', '95 2 g'
PM_RX = re.compile(r"(?<![\d.±])(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*(g|gm|kg|mm|ml|°)(?![A-Za-z])", re.I)


def _candidates(q):
    """(where, getter, setter, text) for every literal text of the sheet."""
    tb = q.record["title_block"]
    for k in list(tb):
        if tb.get(k):
            yield f"title block {k.upper()}", (lambda k=k: tb[k]), (lambda v, k=k: tb.__setitem__(k, v))
    for i, n in enumerate(q.notes):
        yield f"note {n.number or i + 1}", (lambda n=n: n.text), (lambda v, n=n: setattr(n, "text", v))
    for r in q.record.get("bom") or []:
        for k in ("qty", "description", "ref"):
            if r.get(k):
                yield f"BOM item {r.get('item') or '?'}", (lambda r=r, k=k: r[k]), (lambda v, r=r, k=k: r.__setitem__(k, v))


def _plausible(a: str, b: str) -> bool:
    """A tolerance is smaller than its value ('135 10' yes, '10 135' no)."""
    try:
        return 0 < float(b) < float(a)
    except ValueError:
        return False


def find(q) -> List[dict]:
    out = []
    if q.ocr:
        return out   # OCR reads the ± sign (or misreads it); handled by the doubt loop
    for where, get, put in _candidates(q):
        text = get()
        for m in PM_RX.finditer(text):
            if _plausible(m.group(1), m.group(2)):
                out.append({"where": where, "get": get, "put": put, "match": m.group(0),
                            "value": m.group(1), "tol": m.group(2), "unit": m.group(3)})
    return out


def resolve(sheets, client, log, vision: bool) -> list:
    """Confirm each ± visually; rewrite the texts. Returns findings for values
    the model sees *without* a ± sign."""
    from .checks import Finding, locate
    findings = []
    for q in sheets:
        q.pm = []
        for c in find(q):
            box = locate(q, c["match"])
            verdict = "assumed"
            if vision and box is not None:
                try:
                    crop = q.crop(box, dpi=220, pad=4)
                    ans = client.ask(
                        "You look at a small crop of an engineering drawing. Answer only in JSON.",
                        f"The text layer says '{c['match']}'. In the image, is there a plus-minus sign (±) "
                        f"between {c['value']} and {c['tol']}?",
                        {"type": "object", "properties": {"pm": {"type": "string", "enum": ["yes", "no", "unclear"]}},
                         "required": ["pm"]}, images=[crop], max_tokens=20)
                    verdict = {"yes": "confirmed", "no": "absent", "unclear": "unclear"}[ans.get("pm", "unclear")]
                except (ModelError, KeyError) as e:
                    log(f"    ± check not done for {q.number} {c['where']}: {e}")
            q.pm.append({"where": c["where"], "text": c["match"], "verdict": verdict})
            if verdict == "absent":
                findings.append(Finding("D6", "must", "fail",
                                        f"{c['where']}: '{c['match']}' has no ± sign between {c['value']} and {c['tol']} "
                                        f"(checked on the image). Should it be {c['value']}±{c['tol']} {c['unit']}?",
                                        q, c["match"], c["where"], box, history=["image check by the model: no ±"]))
                continue
            new = c["get"]().replace(c["match"], f"{c['value']}±{c['tol']} {c['unit']}".replace(" °", "°"), 1)
            c["put"](new)
        if q.pm:
            n_conf = sum(p["verdict"] == "confirmed" for p in q.pm)
            log(f"    {q.number}: {len(q.pm)} ± value(s) " + (f"({n_conf} confirmed on the image)" if vision else "(read as ±, not checked)"))
        # keep the record's notes list in step with the Note objects
        q.record["notes"] = [n.text for n in q.notes]
    return findings
