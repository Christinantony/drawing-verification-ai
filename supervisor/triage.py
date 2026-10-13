"""Decide, from what was actually read, which sections need the supervisor.

Runs after the readers and rules (Workspace.prepare) and before any model call.
Every section either goes to Gemma with the reasons it is concerning, or is
skipped with a warning that says why. Nothing is dropped silently, and --all
restores the full review.

A drawing sheet is concerning when the rules could not settle it:
  * a medium/high rule finding on the sheet,
  * a required title-block field missing (often a misread, not a blank),
  * words the dictionary does not know (possible misspellings the known list
    does not cover),
  * low-confidence OCR words.
A checklist is concerning when a row disagrees with the drawing or could not
be compared. A drawing relationship is concerning when a cross-file rule
raised a finding on that pair. A generic Word/Excel document is reviewed when
it names a drawing number in this set.

These are deterministic decisions so they can be read, tested and repeated;
asking Gemma to choose would cost a model call per section on a CPU (minutes
each) and the choice could not be audited.
"""
from __future__ import annotations

import json
import re

from . import legacy

CONCERN_SEVERITIES = {"medium", "high"}


def _alnum(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _drawing_reasons(ws, record_key, record):
    ids = set(ws.record_ids.get(record_key, []))
    reasons = []
    hits = [f for f in ws.baseline if f["severity"] in CONCERN_SEVERITIES and ids & set(f["evidence_ids"])]
    missing = [f for f in hits if f.get("category") == "missing_field"]
    other = [f for f in hits if f.get("category") != "missing_field"]
    if other:
        reasons.append(f"{len(other)} rule finding(s) to put in context")
    if missing:
        reasons.append(f"{len(missing)} title-block field(s) not read")
    language = legacy.module("language", "drawing-audit/language.py")
    try:
        config = legacy.module("config", "drawing-audit/config.py")
        extra = getattr(config, "EXTRA_ENGINEERING_WORDS", [])
    except Exception:
        extra = []
    unknown = sorted(language.dictionary_candidates(language.text_lines(record), extra))
    if unknown:
        reasons.append("words not in the dictionary: " + ", ".join(unknown[:6]) + ("…" if len(unknown) > 6 else ""))
    low = record.get("low_confidence_words") or []
    if record.get("text_source") == "ocr" and low:
        reasons.append(f"{len(low)} low-confidence OCR word(s)")
    return reasons


def _relationship_reasons(ws, item):
    try:
        packet = json.loads(item["text"])
    except (ValueError, TypeError):
        return ["relationship record could not be parsed; reviewed to be safe"]
    if packet.get("relationship") != "drawing reference":
        return ["document relationship"]
    sides = []
    for side in ("a", "b"):
        d = packet.get(side) or {}
        sides.append(set(ws.record_ids.get(f"{d.get('_file')}::page:{d.get('_page')}", [])))
    for f in ws.baseline:
        refs = set(f["evidence_ids"])
        if f["severity"] in CONCERN_SEVERITIES and refs & sides[0] and refs & sides[1]:
            return ["cross-file rule finding on this pair"]
    return []


def _document_reasons(ws, item):
    numbers = set()
    for rec in ws.records.values():
        for value in (rec["title_block"].get("drawing_number"), rec.get("filename_drawing_number")):
            if value and len(_alnum(value)) >= 5:
                numbers.add(_alnum(value))
    text = _alnum(item["text"])
    named = sorted(n for n in numbers if n in text)
    return [f"names {len(named)} drawing number(s) in this set"] if named else []


def classify(ws, group):
    """(kind, label, reasons). Empty reasons = not concerning."""
    first = ws.evidence[group["required_ids"][0]]
    kind = first["kind"]
    if group.get("record_key"):
        rec = ws.records[group["record_key"]]
        label = f"{rec['file']} page {rec['page']}"
        return "drawing", label, _drawing_reasons(ws, group["record_key"], rec)
    if kind == "relationship":
        try:
            packet = json.loads(first["text"])
            label = f"link {packet['a']['drawing_no']} ↔ {packet['b']['drawing_no']}"
        except Exception:
            label = "drawing/document relationship"
        return "relationship", label, _relationship_reasons(ws, first)
    if kind in ("checklist", "check_result"):
        status = ws.checklist_status.get(first["file"], "concern")
        label = f"{first['file']} ({'checklist text' if kind == 'checklist' else 'tolerance rows'})"
        if status == "unmatched":
            return kind, label, []
        if status == "clean":
            return kind, label, []
        return kind, label, ["checklist rows disagree with the drawing or could not be compared"]
    if kind == "document":
        return "document", first["file"] or "document", _document_reasons(ws, first)
    return kind, first.get("file") or kind, ["reviewed by default"]


SKIP_TEXT = {
    "drawing": "all rules passed, every required field read, no unknown or low-confidence words",
    "relationship": "the cross-file rules found no disagreement between these drawings",
    "checklist": "every row matches the drawing",
    "check_result": "every row matches the drawing",
    "document": "does not name any drawing number in this set",
}


def plan(ws, review_all=False):
    """Split ws.groups into planned and skipped. Mutates ws.groups (planned only)
    and records skipped sections and their warnings. Returns the plan."""
    planned, skipped = [], []
    for group in ws.groups:
        kind, label, reasons = classify(ws, group)
        entry = {"group": group["id"], "kind": kind, "label": label, "evidence_ids": list(group["required_ids"])}
        if reasons or review_all:
            planned.append({**entry, "reasons": reasons or ["--all: review everything"]})
        else:
            status = ws.checklist_status.get(ws.evidence[group["required_ids"][0]].get("file"))
            why = ("no matching drawing in this set" if status == "unmatched" else SKIP_TEXT.get(kind, "not concerning"))
            skipped.append({**entry, "reason": why})
    keep = {p["group"] for p in planned}
    ws.groups = [g for g in ws.groups if g["id"] in keep]
    ws.skipped = skipped
    seen = set()
    for s in skipped:
        # One warning per skipped source, not per evidence chunk.
        key = (s["label"], s["reason"])
        if key in seen:
            continue
        seen.add(key)
        ws.warn("skipped_not_concerning", f"Supervisor review skipped for {s['label']}: {s['reason']}.", s["evidence_ids"])
    files = {}
    for name in ws.paths:
        files[name] = "read"
    for name in ws.unsupported:
        files[name] = "not supported"
    for item in ws.incomplete:
        for name in ws.paths:
            if item["question"].startswith(("Could not read", "No sheets read", "No text read")) and name in item["question"]:
                files[name] = "unreadable"
    ws.plan = {"files": files,
               "drawings": len(ws.records), "checklists": len(ws.checklists),
               "planned": planned, "skipped": skipped, "review_all": review_all}
    return ws.plan


def summary(ws, budget_seconds=None):
    p = ws.plan
    lines = ["Plan:"]
    counts = {}
    for state in p["files"].values():
        counts[state] = counts.get(state, 0) + 1
    lines.append("  Files: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    lines.append(f"  Drawing sheets: {p['drawings']}; checklists: {p['checklists']}")
    lines.append(f"  Supervisor (Gemma) review: {len(p['planned'])} section group(s); skipped as not concerning: {len(p['skipped'])}")
    for x in p["planned"]:
        lines.append(f"    + {x['label']}: {'; '.join(x['reasons'])}")
    for x in p["skipped"]:
        lines.append(f"    - {x['label']}: {x['reason']}")
    if budget_seconds and p["planned"]:
        lines.append(f"  Upper time bound: {len(p['planned'])} × {budget_seconds} s")
    if ws.warnings:
        lines.append(f"  Warnings: {len(ws.warnings)}")
    if ws.incomplete:
        lines.append(f"  Checks that could not run: {len(ws.incomplete)}")
    return "\n".join(lines)
