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


def load_examples(paths, limit=12) -> List[dict]:
    """Earlier verdicts from findings.csv files: the user's 'house standard'."""
    out = []
    for p in paths or []:
        try:
            with open(p, encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    v = (row.get("your_verdict (real / false / dont care)") or row.get("your_verdict") or "").strip().lower()
                    if v in ("real", "false", "dont care", "don't care"):
                        out.append({"check": row.get("check_name") or row.get("check"), "text": row.get("text", ""),
                                    "finding": row.get("finding", ""), "verdict": v,
                                    "comment": row.get("your_comment", "")})
        except OSError:
            continue
    # keep a mix: false and don't-care verdicts teach the most
    out.sort(key=lambda e: e["verdict"] == "real")
    return out[:limit]


def _examples_text(examples) -> str:
    if not examples:
        return ""
    lines = [f"- {e['check']}: \"{e['finding'][:120]}\" -> engineer said: {e['verdict']}"
             + (f" ({e['comment'][:80]})" if e.get("comment") else "") for e in examples]
    return "\n\nEarlier verdicts by the engineer (follow the same standard):\n" + "\n".join(lines)


def _sheet_context(s) -> str:
    from .checks import drawing_type
    tb = s.record["title_block"]
    return (f"Drawing {s.number}: '{tb.get('title') or ''}', read by {'OCR' if s.ocr else 'its text layer'}, "
            f"{len(s.record.get('bom') or [])} BOM rows, {len(s.notes)} notes.")


# ---------------------------------------------------------------------------
def decide(findings, sheets, client, policy, examples, cfg, log) -> None:
    """Model decides report/drop, severity and wording for every fact."""
    facts = [f for f in findings if f.status in ("fail", "check")]
    by_sheet = {}
    for f in facts:
        by_sheet.setdefault(id(f.sheet), (f.sheet, []))[1].append(f)
    ex = _examples_text(examples)
    for sheet, fs in by_sheet.values():
        for i in range(0, len(fs), 12):
            batch = fs[i:i + 12]
            lines = "\n".join(f"{k + 1}. [{f.code} {f.name}; rule says {f.severity}; "
                              f"{'unsure' if f.status == 'check' else 'certain'}] {f.message}"
                              + (f" (where: {f.where})" if f.where else "")
                              for k, f in enumerate(batch))
            ctx = _sheet_context(sheet) if sheet is not None else "Facts about the whole set."
            schema = {"type": "object", "properties": {"decisions": {"type": "array", "items": {
                "type": "object", "properties": {
                    "n": {"type": "integer"},
                    "decision": {"type": "string", "enum": ["must", "look", "drop"]},
                    "wording": {"type": "string"},
                    "reason": {"type": "string"}},
                "required": ["n", "decision", "wording"]}}}, "required": ["decisions"]}
            user = (f"QC POLICY:\n{policy}{ex}\n\n{ctx}\n\nFacts found by exact checking:\n{lines}\n\n"
                    "For EVERY fact decide must, look or drop under the policy. Word it for the engineer "
                    "(at most 30 words). Give a reason (at most 15 words).")
            try:
                ans = client.ask(SYSTEM, user, schema, max_tokens=80 + 70 * len(batch))
            except ModelError as e:
                log(f"    decisions not made for {sheet.number if sheet else 'the set'}: {e}")
                for f in batch:
                    f.history.append("decision: rule default (model call failed)")
                continue
            seen = set()
            for a in ans.get("decisions", []):
                k = a.get("n", 0) - 1
                if not 0 <= k < len(batch) or k in seen:
                    continue
                seen.add(k)
                f = batch[k]
                f.rule_severity = f.severity
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


# ---------------------------------------------------------------------------
def open_review(sheets, findings, client, policy, examples, log, max_items=8) -> List[Finding]:
    """Model reads a sheet's literal text and reports problems the facts do
    not cover. Each observation must quote text that is on the sheet."""
    out = []
    ex = _examples_text(examples)
    for s in sheets:
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
        user = (f"QC POLICY:\n{policy}{ex}\n\n{_sheet_context(s)}\nLiteral text of the drawing:\n{text}\n\n"
                f"Already reported: {known or 'nothing'}\n\n"
                f"List up to {max_items} OTHER problems a QA engineer would mark on this drawing's text "
                "(wrong or inconsistent names, wrong item for a note, unclear or incomplete notes, a "
                "title-block value that cannot be right). Quote the exact text. Empty list if none.")
        try:
            ans = client.ask(SYSTEM, user, schema, max_tokens=100 + 80 * max_items)
        except ModelError as e:
            log(f"    open review not done for {s.number}: {e}")
            continue
        rejected = 0
        sheet_alnum = alnum(" ".join(w.text for w in s.sheet.words)) + alnum(text)
        for o in ans.get("observations", [])[:max_items]:
            q = (o.get("quote") or "").strip()
            if not q or alnum(q) not in sheet_alnum:
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
