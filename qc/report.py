"""Station 7: the report.

report.html   open in any browser; every finding shows a crop of the sheet
              with the text boxed in red, so checking it takes seconds
findings.csv  one row per item, with an empty 'your_verdict' column for your
              review (real / false / don't care) - the feedback loop
findings.json the same, for tools
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
from collections import defaultdict
from pathlib import Path


SECTIONS = [
    ("must", "Must fix before print"),
    ("look", "Please look"),
    ("check", "Check by hand"),
]


def _crop_b64(f, max_w=640):
    if f.sheet is None or not f.bbox:
        return None
    try:
        from PIL import Image, ImageDraw
        k = 110 / 72.0
        ctx = 40.0
        b = f.bbox
        crop = f.sheet.crop((b[0] - ctx, b[1] - ctx / 2, b[2] + ctx, b[3] + ctx / 2), dpi=110, pad=0)
        img = Image.fromarray(crop).convert("RGB")
        d = ImageDraw.Draw(img)
        ox, oy = max(0, (b[0] - ctx) * k), max(0, (b[1] - ctx / 2) * k)
        d.rectangle([(b[0] * k - ox - 3, b[1] * k - oy - 3), (b[2] * k - ox + 3, b[3] * k - oy + 3)],
                    outline=(220, 40, 40), width=2)
        if img.width > max_w:
            img = img.resize((max_w, int(img.height * max_w / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _summary_html(summary, e):
    if not summary or not summary.get("overall"):
        return ""
    out = [f"<div class='card'><b>Review summary</b> <span class='pill'>written by the model</span>"
           f"<p>{e(summary['overall'])}</p>"]
    if summary.get("sheets"):
        out.append("<ul>" + "".join(f"<li><b>{e(x['drawing'])}</b>: {e(x['summary'])}</li>" for x in summary["sheets"]) + "</ul>")
    out.append("</div>")
    return "".join(out)


def _bucket(f):
    if f.status in ("check", "doubt"):
        return "check"
    if f.status == "fail":
        return f.severity
    return None


def write(out_dir: Path, run: dict, sheets, smap, findings, checklists) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    items = [f for f in findings if _bucket(f)]
    dropped = [f for f in findings if f.status == "dropped"]
    order = {"must": 0, "look": 1, "check": 2}
    items.sort(key=lambda f: (order[_bucket(f)], f.sheet.number if f.sheet else "", f.code))
    for i, f in enumerate(items + dropped, 1):
        f.id = f"Q{i:03d}"

    # CSV + JSON (the review loop)
    with open(out_dir / "findings.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "group", "check", "check_name", "drawing", "file", "where", "text", "finding",
                    "rule_said", "model_said", "history",
                    "your_verdict (real / false / dont care)", "your_comment"])
        for f in items + dropped:
            rule = getattr(f, "rule_severity", f.severity) if f.code != "M1" else "-"
            model = ("drop: " + getattr(f, "drop_reason", "")) if f.status == "dropped" else \
                (f.severity if hasattr(f, "rule_severity") or f.code == "M1" else "")
            w.writerow([f.id, _bucket(f) or "dropped", f.code, f.name, f.sheet.number if f.sheet else "",
                        f.sheet.file if f.sheet else "", f.where, f.quote, f.message, rule, model,
                        " | ".join(f.history), "", ""])
    data = {"run": run, "findings": [{"id": f.id, "group": _bucket(f), "check": f.code, "name": f.name,
                                      "drawing": f.sheet.number if f.sheet else None,
                                      "file": f.sheet.file if f.sheet else None, "where": f.where, "text": f.quote,
                                      "finding": f.message, "history": f.history, "bbox": f.bbox} for f in items]}
    (out_dir / "findings.json").write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")

    e = html.escape
    counts = {k: sum(1 for f in items if _bucket(f) == k) for k, _ in SECTIONS}
    h = [f"""<!doctype html><html><head><meta charset="utf-8"><title>Drawing QC report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px auto;max-width:1000px;color:#222;line-height:1.45}}
h1{{font-size:22px}} h2{{font-size:18px;margin-top:32px;border-bottom:1px solid #ccc;padding-bottom:4px}}
h3{{font-size:15px;margin:18px 0 6px}}
.card{{border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin:10px 0}}
.must{{border-left:5px solid #c0392b}} .look{{border-left:5px solid #d68910}} .check{{border-left:5px solid #2471a3}}
.meta{{color:#666;font-size:13px}} .id{{font-weight:600}} img{{max-width:100%;border:1px solid #eee;margin-top:6px}}
table{{border-collapse:collapse;font-size:13px}} td,th{{border:1px solid #ddd;padding:4px 6px;vertical-align:top;text-align:left}}
.pill{{display:inline-block;border-radius:10px;padding:1px 8px;font-size:12px;background:#eee;margin-left:6px}}
</style></head><body>
<h1>Drawing QC report</h1>
<p class="meta">{e(run['folder'])} &middot; {run['started']} &middot; {len(sheets)} sheet(s) &middot; model: {e(run['model'])}
{(' &middot; ' + e(run['speed'])) if run.get('speed') else ''} &middot; {run['seconds']} s</p>
<p><b>{counts['must']}</b> must fix &middot; <b>{counts['look']}</b> please look &middot; <b>{counts['check']}</b> check by hand
&middot; decisions: {e(run.get('decisions', 'rules'))}{f" &middot; {len(dropped)} dropped by the model" if dropped else ""}</p>
{_summary_html(run.get('summary') or {}, e)}
<p class="meta">A first-pass check of the literal parts of the drawings (title block, BOM, notes, references, callouts).
It does not check geometry or views. Every item needs an engineer's decision; record yours in findings.csv.</p>"""]
    for key, title in SECTIONS:
        group = [f for f in items if _bucket(f) == key]
        h.append(f"<h2>{title} ({len(group)})</h2>")
        if not group:
            h.append("<p class='meta'>None.</p>")
        by_sheet = defaultdict(list)
        for f in group:
            by_sheet[f.sheet.number if f.sheet else "Whole set"].append(f)
        for sname, fs in by_sheet.items():
            h.append(f"<h3>{e(sname)}</h3>")
            for f in fs:
                img = _crop_b64(f)
                others = [r for r in f.related if r is not f.sheet]
                rel = f" &middot; also {', '.join(e(r.number) for r in others[:3])}" if others else ""
                h.append(f"<div class='card {key}'><span class='id'>{f.id}</span><span class='pill'>{f.code} {e(f.name)}</span>"
                         f"<div>{e(f.message)}</div>"
                         f"<div class='meta'>{e(f.sheet.file) if f.sheet else ''}{' &middot; ' + e(f.where) if f.where else ''}{rel}</div>"
                         + (f"<div class='meta'>Doubt loop: {e(' → '.join(f.history))}</div>" if f.history else "")
                         + (f"<img src='data:image/png;base64,{img}'>" if img else "") + "</div>")

    # Dropped by the model
    if dropped:
        h.append(f"<h2>Dropped by the model ({len(dropped)})</h2><p class='meta'>Facts the rules found that the model "
                 "decided not to report under the QC policy, with its reason. Mark them in findings.csv too: a wrong "
                 "drop is the most important thing to correct.</p><ul>")
        for f in dropped:
            h.append(f"<li><b>{f.id}</b> {e(f.sheet.number if f.sheet else 'set')} - {f.code}: "
                     f"{e(getattr(f, 'rule_message', f.message))} <span class='meta'>(reason: {e(getattr(f, 'drop_reason', ''))})</span></li>")
        h.append("</ul>")

    # Layout check
    lay = [(q, getattr(q, "layout", None)) for q in sheets]
    lay = [(q, L) for q, L in lay if L and (L["changes"] or L["added"] or L["disagree"])]
    if lay:
        h.append("<h2>Layout check by the model</h2><p class='meta'>The model looked at each sheet with the "
                 "program's boxes numbered on it. Re-labelled and added blocks were used in the checks; "
                 "disagreements about the title block or BOM are only reported.</p>")
        for q, L in lay:
            h.append(f"<h3>{e(q.number or q.file)}</h3><ul>"
                     + "".join(f"<li>re-labelled: {e(x)}</li>" for x in L["changes"])
                     + "".join(f"<li>added: {e(x)}</li>" for x in L["added"])
                     + "".join(f"<li>disagrees: {e(x)}</li>" for x in L["disagree"]) + "</ul>")

    # Text not placed
    h.append("<h2>Text not placed in a region</h2><p class='meta'>Literal text the agent could not sort into a "
             "title block, BOM, notes, callout, view label or legend. Glance at it; spelling was still checked.</p>")
    any_unplaced = False
    for s in sheets:
        un = s.blocks_of("unplaced")
        if un:
            any_unplaced = True
            h.append(f"<h3>{e(s.number or s.file)}</h3><ul>" + "".join(f"<li>{e(b.text.replace(chr(10), ' / '))}</li>" for b in un) + "</ul>")
    if not any_unplaced:
        h.append("<p class='meta'>None.</p>")

    # Set map
    h.append("<h2>Set map</h2><table><tr><th>Drawing</th><th>Title</th><th>Read by</th><th>NEXT ASSY.</th>"
             "<th>BOM children</th><th>References not in folder</th><th>Jigs / moulds / templates</th></tr>")
    for s in sheets:
        refs = smap.of(s) if smap else []
        missing = sorted({r.number for r in refs if r.status == "missing" and r.kind in ("drawing", "file")})
        tools = sorted({r.number for r in refs if r.kind == "tool"})
        h.append(f"<tr><td>{e(s.number or '')}</td><td>{e(s.record['title_block'].get('title') or '')}</td>"
                 f"<td>{'OCR' if s.ocr else 'text'}</td>"
                 f"<td>{e(', '.join(p.number for p in smap.parents_of(s))) if smap else ''}</td>"
                 f"<td>{e(', '.join(c.number for c in smap.children_of(s))) if smap else ''}</td>"
                 f"<td>{e(', '.join(missing))}</td><td>{e(', '.join(tools))}</td></tr>")
    h.append("</table>")

    # Checklists
    if checklists:
        h.append("<h2>Checklists</h2><table><tr><th>Checklist</th><th>Drawing</th><th>Rows</th><th>Match</th>"
                 "<th>Mismatch</th><th>Filled</th><th>Left empty</th><th>Completed copy</th></tr>")
        for c in checklists:
            h.append(f"<tr><td>{e(c['file'])}</td><td>{e(c.get('drawing') or 'no match')}</td><td>{c.get('rows', '')}</td>"
                     f"<td>{c.get('match', '')}</td><td>{c.get('mismatch', '')}</td><td>{c.get('filled', '')}</td>"
                     f"<td>{c.get('empty', '')}</td><td>{e(c.get('copy') or '')}</td></tr>")
        h.append("</table>")

    # Cleared + coverage
    cleared = [f for f in findings if f.status == "cleared" and f.history]
    h.append(f"<h2>Cleared during the run ({len(cleared)})</h2><p class='meta'>Raised by a rule, then settled by "
             "re-reading or by the model. Listed so nothing disappears silently.</p><ul>")
    for f in cleared[:200]:
        h.append(f"<li>{e(f.sheet.number if f.sheet else '')} - {f.code}: {e(f.message)} <span class='meta'>({e(' → '.join(f.history))})</span></li>")
    h.append("</ul>")
    not_run = [f for f in findings if f.status == "not_run"]
    h.append("<h2>Not checked</h2><ul>")
    if not_run:
        kinds = defaultdict(int)
        for f in not_run:
            kinds[f.name] += 1
        h.append("".join(f"<li>{e(k)}: {n} question(s) for the model were not asked "
                         f"({'run without --no-model' if run['model'] == 'none' else 'model call failed'}).</li>" for k, n in kinds.items()))
    for s in sheets:
        if s.ocr and s.record.get("bom"):
            h.append(f"<li>{e(s.number)}: balloons vs BOM not checked (scanned sheet).</li>")
    for n in run.get("unsupported", []):
        h.append(f"<li>{e(n)}: file type not supported - export drawings as PDF.</li>")
    h.append("<li>Geometry, views, dimensions and GD&amp;T are outside this check.</li></ul>")
    t = {k: v for k, v in (run.get("timing") or {}).items() if isinstance(v, float)}
    if t:
        h.append("<h2>Time per step</h2><p class='meta'>" + " &middot; ".join(f"{e(k)}: {v} s" for k, v in t.items()) + "</p>")
    h.append("</body></html>")
    path = out_dir / "report.html"
    path.write_text("\n".join(h), encoding="utf-8")
    return path
