"""Model decisions (station 7), driven by the user's QC policy (qc/policy.md).

The rules (station 5) and the doubt loop (station 6) produce *facts*. Here
the model decides, per sheet, for every fact: report it or drop it, how
serious it is (must / look), and how to word it. It then does an open review
of the sheet's literal text for anything the facts don't cover, and finally
drafts the review summary.

Guardrails stay in code and are generic, not QC rules:
  * every fact must get an answer; an unanswered fact keeps the rule's decision
  * a dropped fact is listed in the report with the model's reason
  * an open-review observation must quote text that is really on the sheet
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import List

from sheetreader.normalize import alnum

from .checks import Finding, text_lines
from .model import ModelError

SYSTEM = ("You are the QC reviewer for a set of mechanical engineering drawings. You see only text taken "
          "from the drawings and facts found by exact checking code. Follow the QC policy exactly. Answer "
          "only in the JSON format asked for.")


def load_policy(path) -> str:
    p = Path(path) if path else Path(__file__).with_name("policy.md")
    return p.read_text(encoding="utf-8")


def load_examples(paths, limit=None) -> List[dict]:
    """Earlier verdicts from findings.csv files (files or folders): the
    user's 'house standard'. All are kept; each batch is shown the ones most
    like it (lessons.pick)."""
    from .lessons import read_verdicts
    out = read_verdicts(paths)
    return out[-limit:] if limit else out


def _examples_text(examples) -> str:
    if not examples:
        return ""
    lines = [f"- {e.get('check_name') or e['check']}: \"{e['finding'][:120]}\" -> engineer said: {e['verdict']}"
             + (f" ({e['comment'][:80]})" if e.get("comment") else "") for e in examples]
    return ("\n\nEarlier verdicts by the engineer (follow the same standard; 'false' means that kind of report was "
            "wrong, not that nothing should be reported):\n" + "\n".join(lines))


def _today(cfg=None):
    import datetime as _dt
    t = (cfg or {}).get("today") or _dt.date.today()
    return t.strftime("%d/%m/%Y")


def _sheet_context(s, smap=None, cfg=None) -> str:
    """What the model needs to judge this sheet like an engineer who knows the
    set: drawing type, today's date, what each BOM line refers to (and whether
    its name matches that drawing's title), what the sub-assemblies contain,
    and what SEE TABLE points to."""
    from check_coherence import _titles_agree
    from .checks import drawing_type
    tb = s.record["title_block"]
    lines = [f"Drawing {s.number}: '{tb.get('title') or ''}' ({drawing_type(s, cfg or {'drawing_types': {}})}), "
             f"read by {'OCR' if s.ocr else 'its text layer'}, {len(s.record.get('bom') or [])} BOM rows, "
             f"{len(s.notes)} notes. Today's date is {_today(cfg)}; dates are DD/MM/YYYY."
             + ("" if s.ocr else " This sheet was NOT read by OCR: never say that OCR may have misread it.")
             + (" It is a document, not a drawing: drawing title-block fields do not apply."
                if getattr(s, "doc_kind", "") == "document" else "")]
    if smap is not None:
        refs = {r.where: r for r in smap.of(s) if r.where.startswith("BOM item") and r.targets}
        for row in s.record.get("bom") or []:
            where = f"BOM item {row.get('item') or '?'}"
            r = refs.get(where)
            if r:
                t = r.targets[0]
                title = t.record["title_block"].get("title") or ""
                ok = _titles_agree(row.get("description") or "", title)
                lines.append(f"  {where} '{row.get('description')}' refers to drawing {t.number} titled '{title}'"
                             f" ({'name matches the title' if ok else 'NAME DIFFERS from the title'}).")
            elif row.get("description"):
                lines.append(f"  {where} '{row.get('description')}': bought-out / standard part ({row.get('ref') or ''}).")
        for c in smap.children_of(s)[:6]:
            parts = ", ".join(f"{r.get('description')}" for r in (c.record.get("bom") or [])[:14] if r.get("description"))
            if parts:
                lines.append(f"  Sub-assembly {c.number} ('{c.record['title_block'].get('title')}') contains: {parts}.")
    if any("SEE TABLE" in (v or "").upper() for v in tb.values()):
        tables = (["the BOM table (its REF./MATERIAL column)"] if s.record.get("bom") else []) + \
                 (["a specification table"] if any(b.kind in ("table", "unplaced") for b in s.blocks) else [])
        lines.append("  SEE TABLE on this sheet refers to: " + (" or ".join(tables) or "no table found") + ".")
    if getattr(s, "pm", None):
        lines.append("  Values such as " + ", ".join(p["text"] for p in s.pm[:3]) + " are written with ± on the "
                     "drawing (the sign is a graphic); read them as value ± tolerance.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def decide(findings, sheets, client, policy, examples, cfg, log, smap=None) -> None:
    """Model decides report/drop, severity and wording for every fact."""
    facts = [f for f in findings if f.status in ("fail", "check")]
    by_sheet = {}
    for f in facts:
        by_sheet.setdefault(id(f.sheet), (f.sheet, []))[1].append(f)
    from .lessons import pick

    def one(sheet, batch):
        ex = _examples_text(pick(examples, batch, sheet, smap))
        lines = "\n".join(f"{k + 1}. [{f.code} {f.name}; rule says {f.severity}; "
                          f"{'unsure' if f.status == 'check' else 'certain'}] {f.message}"
                          + (f" (where: {f.where})" if f.where else "")
                          for k, f in enumerate(batch))
        ctx = _sheet_context(sheet, smap, cfg) if sheet is not None else \
            f"Facts about the whole set. Today's date is {_today(cfg)}; dates are DD/MM/YYYY."
        schema = {"type": "object", "properties": {"decisions": {"type": "array", "items": {
            "type": "object", "properties": {
                "n": {"type": "integer"},
                "decision": {"type": "string", "enum": ["must", "look", "drop"]},
                "wording": {"type": "string"},
                "reason": {"type": "string"}},
            "required": ["n", "decision", "wording"]}}}, "required": ["decisions"]}
        user = (f"QC POLICY:\n{policy}{ex}\n\n{ctx}\n\nFacts found by exact checking:\n{lines}\n\n"
                "For EVERY fact decide must, look or drop under the policy. Word it for the engineer "
                "(at most 30 words). Give a reason (at most 10 words).")
        try:
            ans = client.ask(SYSTEM, user, schema, max_tokens=120 + 140 * len(batch))
        except ModelError as e:
            if ("cut off" in str(e) or "too long" in str(e) or "filled the context" in str(e)) and len(batch) > 1:
                # the answer did not fit: ask again in two halves
                h = len(batch) // 2
                one(sheet, batch[:h])
                one(sheet, batch[h:])
                return
            log(f"    decisions not made for {sheet.number if sheet else 'the set'}: {e}")
            for f in batch:
                f.history.append("decision: rule default (model call failed)")
            return
        seen = set()
        for a in ans.get("decisions", []):
            k = a.get("n", 0) - 1
            if not 0 <= k < len(batch) or k in seen:
                continue
            seen.add(k)
            f = batch[k]
            f.rule_severity = f.severity
            if a["decision"] == "drop" and f.code in (cfg.get("never_drop") or []):
                # The policy keeps these for the engineer to decide.
                a = dict(a, decision="look")
                f.history.append(f"model wanted to drop it ({a.get('reason', '')}); kept for the engineer")
            if a["decision"] == "drop":
                f.status = "dropped"
                f.drop_reason = a.get("reason", "")
            else:
                f.severity = a["decision"]
                if a.get("wording", "").strip():
                    f.rule_message = f.message
                    f.message = a["wording"].strip()
            f.history.append(f"decision: {a['decision']} - {a.get('reason', '')}")
        for k, f in enumerate(batch):
            if k not in seen:
                f.history.append("decision: rule default (model gave no answer)")

    for sheet, fs in by_sheet.values():
        for i in range(0, len(fs), 8):
            one(sheet, fs[i:i + 8])


# ---------------------------------------------------------------------------
def open_review(sheets, findings, client, policy, examples, log, max_items=8, smap=None, cfg=None) -> List[Finding]:
    """Model reads a sheet's literal text and reports problems the facts do
    not cover. Each observation must quote text that is on the sheet."""
    from .lessons import pick
    out = []
    for s in sheets:
        mine = [f for f in findings if f.sheet is s and f.status in ("fail", "check")]
        ex = _examples_text(pick(examples, mine, s, smap, codes={"M1"}))
        parts = []
        tb = s.record["title_block"]
        parts.append("TITLE BLOCK: " + "; ".join(f"{k.upper()}={v}" for k, v in tb.items() if v))
        for r in s.record.get("bom") or []:
            parts.append(f"BOM {r.get('item') or '?'}: {r.get('description') or ''} | QTY {r.get('qty') or ''} | {r.get('ref') or ''}")
        for i, n in enumerate(s.notes, 1):
            parts.append(f"NOTE {n.number or i}: {n.text}")
        for b in s.blocks:
            if b.kind in ("view_label", "legend", "callout", "unplaced", "annotation"):
                for l in b.lines:
                    parts.append(f"{b.kind.upper().replace('_', ' ')}: {l.text}")
        text = "\n".join(parts)[:3500]
        known = "; ".join(f.message[:80] for f in findings if f.sheet is s and f.status in ("fail", "check"))[:1200]
        schema = {"type": "object", "properties": {"observations": {"type": "array", "items": {
            "type": "object", "properties": {
                "quote": {"type": "string"}, "problem": {"type": "string"},
                "severity": {"type": "string", "enum": ["must", "look"]}},
            "required": ["quote", "problem", "severity"]}}}, "required": ["observations"]}
        user = (f"QC POLICY:\n{policy}{ex}\n\n{_sheet_context(s, smap, cfg)}\nLiteral text of the drawing:\n{text}\n\n"
                f"Already reported: {known or 'nothing'}\n\n"
                f"List up to {max_items} OTHER problems a QA engineer would mark on this drawing's text "
                "(a note naming a part differently from its exact BOM name, a wrong item for a note, a "
                "misspelt word, an unclear or incomplete note, a title-block value that cannot be right). "
                "Quote the exact text. Follow the policy and house rules; empty list only if the text is clean.")
        try:
            ans = client.ask(SYSTEM, user, schema, max_tokens=100 + 80 * max_items)
        except ModelError as e:
            log(f"    open review not done for {s.number}: {e}")
            continue
        rejected = 0
        sheet_alnum = alnum(" ".join(w.text for w in s.sheet.words)) + alnum(text)
        bom_target = {}
        if smap is not None:
            for r in smap.of(s):
                if r.where.startswith("BOM item") and r.targets:
                    bom_target[r.where.split()[-1]] = r.targets[0].number
        for o in ans.get("observations", [])[:max_items]:
            q = (o.get("quote") or "").strip()
            if not q or alnum(q) not in sheet_alnum:
                rejected += 1
                continue
            # Two BOM lines that refer to different drawings are different
            # parts: an observation comparing their names is not a finding.
            said = q + " " + o.get("problem", "")
            items = set(re.findall(r"\b(?:BOM\s*)?items?\s*(?:no\.?\s*)?(\d{1,3})\b", said, re.I))
            said_alnum = alnum(said)
            for row in s.record.get("bom") or []:   # rows named by their description
                d = alnum(row.get("description") or "")
                if len(d) >= 3 and d in said_alnum:
                    items.add(str(row.get("item")))
            targets = {bom_target.get(i) for i in items if bom_target.get(i)}
            if len(items) >= 2 and len(targets) >= 2:
                rejected += 1
                continue
            from .checks import locate
            out.append(Finding("M1", o.get("severity", "look"), "fail", o.get("problem", "").strip(), s, q,
                               "model observation", locate(s, q), history=["open review by the model"]))
        if rejected:
            log(f"    {s.number}: {rejected} model observation(s) dropped (quote not on the sheet)")
    return out


# ---------------------------------------------------------------------------
def draft_summary(sheets, findings, client, log) -> dict:
    """Model-written review summary: one paragraph for the set, one or two
    sentences per sheet with must-fix items."""
    rep = [f for f in findings if f.status in ("fail", "check")]
    if not rep:
        return {}
    lines = []
    for f in rep[:80]:
        lines.append(f"- {f.sheet.number if f.sheet else 'SET'} [{f.severity if f.status == 'fail' else 'check'}] {f.message[:140]}")
    schema = {"type": "object", "properties": {
        "overall": {"type": "string"},
        "sheets": {"type": "array", "items": {"type": "object", "properties": {
            "drawing": {"type": "string"}, "summary": {"type": "string"}}, "required": ["drawing", "summary"]}}},
        "required": ["overall", "sheets"]}
    user = ("Write the review summary for the engineer who will correct these drawings before print. "
            "Overall: at most 80 words, most important first. Then, for each drawing that has must items, "
            "at most 2 sentences. Plain English, no headings.\n\nFindings:\n" + "\n".join(lines))
    try:
        ans = client.ask(SYSTEM, user, schema, max_tokens=700)
    except ModelError as e:
        log(f"    summary not drafted: {e}")
        return {}
    known = {s.number for s in sheets}
    return {"overall": ans.get("overall", "").strip(),
            "sheets": [x for x in ans.get("sheets", []) if x.get("drawing") in known]}
