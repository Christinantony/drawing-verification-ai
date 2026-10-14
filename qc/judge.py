"""Model judgement calls (part of station 5). Three kinds of short question:

    spelling    all unknown words of the set, in batches: correct / misspelt / unsure
    note_item   does a note make sense for the BOM item it names?
    notes       is each note complete and clear? (one question per notes block)

The model only sees literal text. Every answer is checked against the
question (a word it was not asked about, or an index out of range, is
dropped), so it cannot invent a finding.
"""
from __future__ import annotations

from typing import List

from .model import ModelError

SYSTEM = ("You are a quality checker for mechanical engineering drawings. You see only text taken "
          "from the drawings. Shop abbreviations (QTY, NOS, CSK, THRU, SWG, TYP, REF), part numbers, "
          "brand and trade names, standards (ASTM, MIL, IPC) and British or American spelling are "
          "correct. Answer only in the JSON format asked for. Be brief.")


def judge(findings, client, log) -> None:
    asks = [f for f in findings if f.status == "ask"]
    if not asks:
        return
    if client.name == "none":
        for f in asks:
            f.status = "not_run"
        return
    _spelling([f for f in asks if f.ask.get("type") == "spelling"], client, log)
    _note_items([f for f in asks if f.ask.get("type") == "note_item"], client, log)
    for f in [f for f in asks if f.ask.get("type") == "notes"]:
        _notes(f, findings, client, log)


def _spelling(fs, client, log):
    for i in range(0, len(fs), 30):
        batch = fs[i:i + 30]
        lines = "\n".join(f"{k + 1}. {f.ask['word']}  -- in: \"{f.ask['line'][:120]}\"" for k, f in enumerate(batch))
        schema = {"type": "object", "properties": {"words": {"type": "array", "items": {
            "type": "object", "properties": {
                "n": {"type": "integer"}, "verdict": {"type": "string", "enum": ["correct", "misspelt", "unsure"]},
                "correction": {"type": "string"}}, "required": ["n", "verdict"]}}}, "required": ["words"]}
        try:
            ans = client.ask(SYSTEM, "For each numbered word, is it spelt correctly for a drawing? "
                             "Give a correction only if misspelt.\n" + lines, schema, max_tokens=60 + 25 * len(batch))
        except ModelError as e:
            log(f"spelling batch not judged: {e}")
            for f in batch:
                f.status = "not_run"
            continue
        seen = set()
        for a in ans.get("words", []):
            k = a.get("n", 0) - 1
            if not 0 <= k < len(batch) or k in seen:
                continue
            seen.add(k)
            f = batch[k]
            if a["verdict"] == "correct":
                f.status = "cleared"
                f.history.append("model: spelt correctly")
            elif a["verdict"] == "misspelt":
                f.status = "fail"
                corr = (a.get("correction") or "").upper()
                f.message = f"'{f.ask['word']}' looks misspelt" + (f"; should it be '{corr}'?" if corr else ".")
                f.history.append(f"model: misspelt -> {corr}")
            else:
                f.status, f.severity = "fail", "look"
                f.message = f"'{f.ask['word']}' is not in the dictionary and the model is unsure; please check."
        for k, f in enumerate(batch):
            if k not in seen:
                f.status = "not_run"


def _note_items(fs, client, log):
    by_sheet = {}
    for f in fs:
        by_sheet.setdefault(id(f.sheet), []).append(f)
    for group in by_sheet.values():
        lines = "\n".join(f"{k + 1}. NOTE: \"{f.ask['note']}\"  ITEM {f.ask['item']} is: \"{f.ask['item_text']}\""
                          for k, f in enumerate(group))
        schema = {"type": "object", "properties": {"answers": {"type": "array", "items": {
            "type": "object", "properties": {
                "n": {"type": "integer"}, "verdict": {"type": "string", "enum": ["fits", "doubtful", "wrong"]},
                "reason": {"type": "string"}}, "required": ["n", "verdict"]}}}, "required": ["answers"]}
        try:
            ans = client.ask(SYSTEM, "Each line is a drawing note and the BOM item it names. Does the note make "
                             "sense for that item (right kind of part or material for what the note says)? "
                             "Reason in at most 15 words.\n" + lines, schema, max_tokens=80 + 40 * len(group))
        except ModelError as e:
            log(f"note/item question not judged: {e}")
            for f in group:
                f.status = "not_run"
            continue
        seen = set()
        for a in ans.get("answers", []):
            k = a.get("n", 0) - 1
            if not 0 <= k < len(group) or k in seen:
                continue
            seen.add(k)
            f = group[k]
            if a["verdict"] == "fits":
                f.status = "cleared"
            else:
                f.status = "fail"
                f.message = (f"{f.where} names ITEM {f.ask['item']} ({f.ask['item_text']}); the model thinks this is "
                             f"{a['verdict']}: {a.get('reason', '').strip()}")
            f.history.append(f"model: {a['verdict']} - {a.get('reason', '')}")
        for k, f in enumerate(group):
            if k not in seen:
                f.status = "not_run"


def _notes(f, findings, client, log):
    notes = f.ask["notes"]
    lines = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(notes))
    schema = {"type": "object", "properties": {"problems": {"type": "array", "items": {
        "type": "object", "properties": {"n": {"type": "integer"}, "problem": {"type": "string"}},
        "required": ["n", "problem"]}}}, "required": ["problems"]}
    try:
        ans = client.ask(SYSTEM, "These are the notes of one drawing. List only notes that are incomplete "
                         "(cut off mid-sentence), unclear, or contradict another note. Problem in at most 15 "
                         "words. Empty list if all are fine.\n" + lines, schema, max_tokens=60 + 30 * len(notes))
    except ModelError as e:
        log(f"notes not judged: {e}")
        f.status = "not_run"
        return
    from .checks import Finding
    f.status = "cleared"
    for p in ans.get("problems", []):
        k = p.get("n", 0) - 1
        if 0 <= k < len(notes):
            sheet_note = [n for n in f.sheet.notes if n.text == notes[k]]
            label = (sheet_note[0].number if sheet_note and sheet_note[0].number else str(k + 1))
            findings.append(Finding("D4", f.severity, "fail", f"Note {label}: {p['problem'].strip()} - \"{notes[k]}\"",
                                    f.sheet, notes[k], f"note {label}", sheet_note[0].bbox if sheet_note else None,
                                    history=["model judgement"]))
