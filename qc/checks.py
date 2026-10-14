"""Station 5: the QC check catalogue (A-E).

Every check is a plain rule ("gauge") that returns findings with a status:

    fail    the rule is sure: a mistake to show you
    doubt   the rule can't be sure (usually an OCR read): goes to the doubt loop
    ask     a judgement call for the model (spelling, does a note make sense)

Each finding carries the exact text, the sheet and where on the sheet, so the
report can show a crop of it.
"""
from __future__ import annotations

import difflib
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "coherence", "drawing-audit"):
    p = str(ROOT / sub) if sub else str(ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)

from sheetreader.cells import reading_order  # noqa: E402
from sheetreader.normalize import alnum, ids_from_filename, rev_key  # noqa: E402

from .setmap import SEE_RX, item_refs  # noqa: E402

CHECK_NAMES = {
    "A1": "BOM row complete", "A2": "Balloons match BOM", "A3": "BOM name matches drawing title",
    "A4": "View label matches drawing", "A5": "Title block complete", "A6": "Drawing number and revision",
    "B1": "Known misspelling", "B2": "Unknown word", "B3": "Spelt two ways in the set",
    "B4": "Standard callout written two ways", "B5": "Unsure OCR reading",
    "C1": "Reference to a drawing", "C2": "NEXT ASSY. and parent BOM", "C3": "Item number in a note",
    "C4": "Note fits its item", "C5": "SEE NOTE / TABLE / DETAIL target", "C6": "USE FILE names this drawing",
    "C7": "Jig, mould and template numbers", "C8": "Checklist vs drawing",
    "D1": "Note numbering", "D2": "Note vs title block", "D3": "Same note worded differently",
    "D4": "Note complete and clear", "D5": "Legend and symbol", "M1": "Model observation",
    "E1": "General tolerances across the set", "E2": "Dates across the set",
}


@dataclass
class Finding:
    code: str
    severity: str
    status: str                     # fail | doubt | ask | ok | cleared
    message: str
    sheet: object = None            # QCSheet, or None for a set-level finding
    quote: str = ""
    where: str = ""
    bbox: Optional[tuple] = None
    related: list = field(default_factory=list)
    doubt: dict = field(default_factory=dict)      # for the doubt loop
    ask: dict = field(default_factory=dict)        # for a model judgement
    history: list = field(default_factory=list)    # what the doubt loop did

    @property
    def name(self):
        return CHECK_NAMES.get(self.code, self.code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def locate(sheet, text: str) -> Optional[tuple]:
    """Where `text` is written on the sheet (bbox of the words), by matching
    letters and digits only, line by line."""
    want = alnum(text)
    if not want or sheet is None:
        return None
    from .sheets import union
    best = None
    for ln in reading_order(sheet.sheet.words):
        keys = [alnum(w.text) for w in ln]
        joined = "".join(keys)
        pos = joined.find(want)
        if pos < 0:
            pos = joined.find(want[: max(6, len(want) // 2)]) if len(want) > 12 else -1
            if pos < 0:
                continue
        # map character span back to words
        start, acc, picked = pos, 0, []
        for w, k in zip(ln, keys):
            if acc + len(k) > start and acc < start + len(want):
                picked.append(w)
            acc += len(k)
        if picked:
            b = union(w.bbox for w in picked)
            if best is None or (b[2] - b[0]) < (best[2] - best[0]):
                best = b
    return best


def drawing_type(sheet, cfg) -> str:
    title = (sheet.record["title_block"].get("title") or "").upper()
    for name, spec in cfg["drawing_types"].items():
        if any(w in title for w in spec["title_words"]):
            return name
    return "assembly" if sheet.record.get("bom") else "part"


def text_lines(sheet) -> List[tuple]:
    """(where, text, bbox) for every literal text on the sheet worth reading."""
    out = []
    tb = sheet.record["title_block"]
    for k in ("title", "material", "finish"):
        if tb.get(k):
            out.append((f"title block {k.upper()}", tb[k], None))
    for row in sheet.record.get("bom") or []:
        for k in ("description", "ref"):
            if row.get(k):
                out.append((f"BOM item {row.get('item') or '?'}", row[k], None))
    for i, n in enumerate(sheet.notes, 1):
        out.append((f"note {n.number or i}", n.text, n.bbox))
    for b in sheet.blocks:
        if b.kind in ("view_label", "legend", "unplaced", "callout", "table", "annotation"):
            for l in b.lines:
                out.append((b.kind.replace("_", " "), l.text, l.bbox))
    return out


def _sev(cfg, key):
    return cfg["severity"].get(key, "look")


# ---------------------------------------------------------------------------
# A. Component names and identity
# ---------------------------------------------------------------------------
def check_A(sheets, smap, cfg) -> List[Finding]:
    from check_coherence import _titles_agree
    out = []
    for s in sheets:
        bom = s.record.get("bom") or []
        # A1 BOM rows complete; item numbers unique and in sequence
        nums = []
        for row in bom:
            item = (row.get("item") or "").strip()
            desc = (row.get("description") or "").strip()
            label = f"BOM item {item or '?'}"
            st = "doubt" if s.ocr else "fail"
            if not item:
                out.append(Finding("A1", _sev(cfg, "A1"), st, f"BOM row '{desc or row.get('ref')}' has no item number.",
                                   s, desc, label, locate(s, desc), doubt={"kind": "field", "text": desc, "item_cell": True}))
            else:
                nums.append(item)
            if not desc:
                out.append(Finding("A1", _sev(cfg, "A1"), st, f"{label} has no component name (DESCRIPTION is empty).",
                                   s, row.get("ref") or "", label, locate(s, row.get("ref") or "")))
            if not (row.get("qty") or "").strip():
                out.append(Finding("A1", _sev(cfg, "A1"), st, f"{label} '{desc}' has no quantity.",
                                   s, desc, label, locate(s, desc), doubt={"kind": "field", "text": desc}))
        dup = [n for n, c in Counter(nums).items() if c > 1]
        if dup:
            out.append(Finding("A1", _sev(cfg, "A1"), "fail", f"BOM item number(s) used twice: {', '.join(dup)}.", s,
                               where="BOM"))
        ints = sorted(int(re.sub(r"\D", "", n)) for n in nums if re.sub(r"\D", "", n))
        if ints and not s.ocr:
            gaps = sorted(set(range(1, ints[-1] + 1)) - set(ints))
            if gaps:
                out.append(Finding("A1", _sev(cfg, "A1"), "fail",
                                   f"BOM item number(s) missing from the sequence: {', '.join(map(str, gaps))}.", s, where="BOM"))
        # A2 balloons vs BOM (text-layer sheets: circles are exact)
        if bom and not s.ocr:
            items = {re.sub(r"[.\s]", "", r.get("item") or "") for r in bom if r.get("item")}
            balloons = set(s.balloons)
            for it in sorted(items - balloons, key=lambda x: int(re.sub(r"\D", "", x) or 0)):
                desc = next((r.get("description") for r in bom if (r.get("item") or "").strip(" .") == it), "")
                out.append(Finding("A2", _sev(cfg, "A2_missing_balloon"), "fail",
                                   f"BOM item {it} '{desc}' has no balloon on the drawing.", s, desc, f"BOM item {it}",
                                   locate(s, desc)))
            for it in sorted(balloons - items, key=lambda x: int(re.sub(r"\D", "", x) or 0)):
                out.append(Finding("A2", _sev(cfg, "A2_extra_balloon"), "fail",
                                   f"Balloon {it} has no row in the BOM.", s, it, "balloon", s.balloons[it][0]))
        # A3 BOM name vs child title
        for r in smap.of(s):
            if not r.where.startswith("BOM item") or not r.targets:
                continue
            row = next((x for x in bom if f"BOM item {x.get('item') or '?'}" == r.where), None)
            if not row or not row.get("description"):
                continue
            for t in r.targets:
                title = t.record["title_block"].get("title")
                if t is not s and title and not _titles_agree(row["description"], title):
                    out.append(Finding("A3", _sev(cfg, "A3"), "fail",
                                       f"{r.where} calls {r.number} '{row['description']}', but that drawing's title is '{title}'.",
                                       s, row["description"], r.where, locate(s, row["description"]), related=[t]))
        # A4 view labels "NUMBER (NAME)"
        for b in s.blocks_of("view_label"):
            for l in b.lines:
                m = re.search(r"\(\s*([^()]{2,40})\s*\)\s*$", l.text)
                refs = [r for r in smap.of(s) if r.where == "view label" and r.text in l.text]
                if not m or not refs:
                    continue
                ref = refs[0]
                name = m.group(1)
                if alnum(ref.number) in alnum(name):
                    # "NAME (NUMBER)": the name is outside the brackets
                    name = l.text[:m.start()].strip(" -:")
                    if not name:
                        continue
                if ref.targets and s not in ref.targets:
                    out.append(Finding("A4", _sev(cfg, "A4"), "fail",
                                       f"View label '{l.text}' names {ref.number}, which is a different drawing.", s, l.text,
                                       "view label", l.bbox, related=ref.targets))
                used = [(x.sheet, row.get("description")) for x in smap.refs if x.where.startswith("BOM item")
                        and alnum(x.number) == alnum(ref.number)
                        for row in (x.sheet.record.get("bom") or []) if f"BOM item {row.get('item') or '?'}" == x.where]
                for psheet, desc in used:
                    if desc and not _titles_agree(desc, name):
                        out.append(Finding("A4", _sev(cfg, "A4"), "fail",
                                           f"View label calls {ref.number} '{name}', but {psheet.number}'s BOM calls it '{desc}'.",
                                           s, l.text, "view label", l.bbox, related=[psheet]))
        # A5 title block complete
        dtype = drawing_type(s, cfg)
        exempt = set(cfg["drawing_types"].get(dtype, {}).get("exempt", []))
        tb = s.record["title_block"]
        for f_ in cfg["required_fields"]:
            if f_ in exempt or (tb.get(f_) or "").strip():
                continue
            label = f_.replace("_by", "").replace("_", " ").upper()
            out.append(Finding("A5", _sev(cfg, "A5"), "doubt" if s.ocr else "fail",
                               f"Title block has no {label}" + (" (read by OCR; may be a misread)" if s.ocr else "") + ".",
                               s, label, "title block", s.parts["title_block"].region,
                               doubt={"kind": "tb_field", "field": f_, "label": label}))
        if not any((tb.get(f_) or "").strip() for f_ in cfg["approval_fields"]):
            out.append(Finding("A5", _sev(cfg, "A5"), "doubt" if s.ocr else "fail",
                               "Title block has no APPROVED / VERIFIED signature.", s, "APPROVED", "title block",
                               s.parts["title_block"].region))
        # A6 drawing number / revision vs file name
        fn_no, fn_rev = s.record.get("filename_drawing_number"), s.record.get("filename_revision")
        tb_no, tb_rev = tb.get("drawing_number"), tb.get("revision")
        if fn_no and tb_no and alnum(fn_no).replace("AND", "") != alnum(tb_no).replace("AND", ""):
            ocr_same = s.ocr and smap.by_ocr.get(__import__("sheetreader.normalize", fromlist=["ocr_key"]).ocr_key(tb_no))
            if not ocr_same:
                out.append(Finding("A6", _sev(cfg, "A6"), "fail",
                                   f"Title block DRAWING No. is '{tb_no}' but the file is '{fn_no}'.", s, tb_no,
                                   "title block DRAWING No.", locate(s, tb_no)))
        if fn_rev and tb_rev and rev_key(fn_rev) != rev_key(tb_rev):
            out.append(Finding("A6", _sev(cfg, "A6"), "doubt" if s.ocr else "fail",
                               f"Title block REV is '{tb_rev}' but the file name says '{fn_rev}'.", s, tb_rev, "title block REV"))
    return out


# ---------------------------------------------------------------------------
# B. Spelling and wording
# ---------------------------------------------------------------------------
def check_B(sheets, cfg, smap=None) -> List[Finding]:
    import language
    out = []
    shop = [w.lower() for w in cfg["shop_words"]]
    for s in sheets:
        lines = [(where, text) for where, text, _ in text_lines(s)]
        for hit in language.known_misspellings(lines):
            m = re.match(r"'([^']+)'", hit["description"])
            quote = m.group(1) if m else ""
            out.append(Finding("B1", _sev(cfg, "B1"), "fail", hit["description"], s, quote, hit.get("location", ""),
                               locate(s, quote)))
        cands = language.dictionary_candidates(lines, shop)
        for word, (where, suggestion) in sorted(cands.items()):
            line = next((t for w_, t, _ in text_lines(s) if w_ == where and word.lower() in t.lower()), "")
            st = "doubt" if s.ocr and _low_conf(s, word) else "ask"
            out.append(Finding("B2", _sev(cfg, "B2"), st,
                               f"'{word}' is not in the dictionary" + (f" (did you mean '{suggestion.upper()}'?)" if suggestion else "") + ".",
                               s, word, where, locate(s, word),
                               doubt={"kind": "word", "text": word},
                               ask={"type": "spelling", "word": word, "line": line, "suggestion": suggestion}))
    # B3 the same term spelt two ways across the set
    seen = defaultdict(lambda: defaultdict(set))
    for s in sheets:
        for where, text, _ in text_lines(s):
            for w in re.findall(r"[A-Za-z]{6,}", text):
                if s.ocr and _low_conf(s, w):
                    continue
                key = re.sub(r"(.)\1", r"\1", w.upper().replace("Z", "S"))
                seen[key][w.upper()].add(s)
    for key, spellings in seen.items():
        if len(spellings) < 2:
            continue
        forms = sorted(spellings)
        desc = "; ".join(f"'{f}' on {', '.join(sorted(x.number or x.file for x in spellings[f]))}" for f in forms)
        first = next(iter(spellings[forms[0]]))
        out.append(Finding("B3", _sev(cfg, "B3"), "fail", f"Spelt two ways in this set: {desc}. Use one spelling.",
                           first, forms[0], "", locate(first, forms[0]),
                           related=[x for f in forms[1:] for x in spellings[f]]))
    # B4 standard callouts (finish / material) written two ways for the same standards
    by_std = defaultdict(lambda: defaultdict(list))
    for s in sheets:
        tb = s.record["title_block"]
        for f_ in ("finish", "material"):
            v = tb.get(f_) or ""
            stds = tuple(sorted(re.findall(r"\b(?:ASTM|AMS|MIL|IS|IPC|DIN|ISO|BS)[-\s]?[A-Z]*[-\s]?\d[\w.\-]*", v)))
            if stds:
                by_std[(f_, stds)][re.sub(r"\s+", " ", v.upper().strip())].append(s)
    for (f_, stds), forms in by_std.items():
        if len(forms) > 1 and not _only_spelling(list(forms)):
            parts = "; ".join(f"'{t}' on {', '.join(x.number for x in ss)}" for t, ss in forms.items())
            ss0 = next(iter(forms.values()))[0]
            out.append(Finding("B4", _sev(cfg, "B4"), "fail", f"{f_.upper()} with the same standards written differently: {parts}.",
                               ss0, "", f"title block {f_.upper()}"))
    # B5 low-confidence OCR words in literal text
    for s in sheets:
        if not s.ocr:
            continue
        resolved = " ".join(r.text for r in (smap.of(s) if smap else []) if r.status in ("ok", "tool", "self"))
        for where, text, bbox in text_lines(s):
            if where in ("unplaced", "callout", "view label"):
                continue   # OCR scraps from the views: not literal parts we check
            for w in s.sheet.words:
                if w.text in resolved:
                    continue
                if re.fullmatch(r"\W*[Xx*]+\W*", w.text):
                    continue   # 'XXXX' placeholder in a legend
                if w.conf < 60 and len(w.text) >= 3 and re.search(r"[A-Za-z]", w.text) and w.text in text:
                    out.append(Finding("B5", _sev(cfg, "B5"), "doubt", f"OCR is unsure of '{w.text}' in {where}.", s,
                                       w.text, where, w.bbox, doubt={"kind": "word", "text": w.text}))
                    break
    return out


def _only_spelling(forms) -> bool:
    keys = {re.sub(r"(.)\1", r"\1", f.replace("Z", "S")) for f in forms}
    return len(keys) == 1   # already reported by B3


def _low_conf(s, word) -> bool:
    return any(w.conf < 75 for w in s.sheet.words if word.lower() in w.text.lower())


# ---------------------------------------------------------------------------
# C. References
# ---------------------------------------------------------------------------
def check_C(sheets, smap, cfg) -> List[Finding]:
    out = []
    for r in smap.refs:
        s = r.sheet
        box = r.bbox if r.bbox else locate(s, r.number)
        if r.status == "styled":
            out.append(Finding("C1", _sev(cfg, "C1_styled"), "fail", f"{r.where}: drawing number {r.detail}.",
                               s, r.text, r.where, box, related=r.targets))
        elif r.status == "missing" and r.kind in ("drawing", "file") and r.ocr:
            out.append(Finding("C1", _sev(cfg, "C1_missing"), "doubt",
                               f"{r.where} refers to {r.number}, which is not in this folder - add it so it can be checked, or correct the number.",
                               s, r.text, r.where, box, doubt={"kind": "ref", "text": r.text}))
        elif r.status == "unsure":
            out.append(Finding("C1", _sev(cfg, "C1_styled"), "doubt", f"{r.where}: {r.detail}.", s, r.text, r.where, box,
                               related=r.targets, doubt={"kind": "ref", "text": r.text}))
    # References to drawings that are not in the folder: one item per sheet.
    for s in sheets:
        miss = [r for r in smap.of(s) if r.status == "missing" and r.kind in ("drawing", "file") and not r.ocr]
        if miss:
            listed = "; ".join(f"{r.number} ({r.where})" for r in miss)
            out.append(Finding("C1", _sev(cfg, "C1_missing"), "fail",
                               f"Refers to {len(miss)} drawing(s) not in this folder: {listed}. Add them so the links "
                               "can be checked, or correct the numbers.", s, miss[0].text, miss[0].where,
                               miss[0].bbox or locate(s, miss[0].number)))
    # C2 NEXT ASSY. <-> parent BOM, both directions, once per pair
    pairs = set()
    for s in sheets:
        for p in smap.parents_of(s):
            if s not in smap.children_of(p):
                pairs.add((id(s), id(p)))
                out.append(Finding("C2", _sev(cfg, "C2"), "doubt" if (s.ocr or p.ocr) else "fail",
                                   f"{s.number} says NEXT ASSY. = {p.number}, but {p.number}'s BOM does not list {s.number}.",
                                   s, s.record["title_block"].get("next_assy") or "", "NEXT ASSY.", None, related=[p]))
        for c in smap.children_of(s):
            if (id(c), id(s)) in pairs:
                continue
            parents = smap.parents_of(c)
            if c.record["title_block"].get("next_assy") and s not in parents:
                out.append(Finding("C2", _sev(cfg, "C2"), "doubt" if (s.ocr or c.ocr) else "fail",
                                   f"{s.number}'s BOM lists {c.number}, but {c.number}'s NEXT ASSY. is "
                                   f"'{c.record['title_block'].get('next_assy')}'.", s, c.number, "BOM", locate(s, c.number), related=[c]))
    for s in sheets:
        bom = s.record.get("bom") or []
        items = {re.sub(r"[.\s]", "", r.get("item") or ""): r for r in bom}
        # C3 / C4 item numbers in notes
        for i, n in enumerate(s.notes, 1):
            for it in item_refs(n.text):
                if not bom:
                    out.append(Finding("C3", _sev(cfg, "C3"), "fail",
                                       f"Note {n.number or i} refers to ITEM {it}, but this sheet has no BOM.", s, n.text,
                                       f"note {n.number or i}", n.bbox))
                elif it not in items:
                    out.append(Finding("C3", _sev(cfg, "C3"), "doubt" if s.ocr else "fail",
                                       f"Note {n.number or i} refers to ITEM {it}, which is not in the BOM (items 1-{len(bom)}).",
                                       s, n.text, f"note {n.number or i}", n.bbox))
                else:
                    row = items[it]
                    out.append(Finding("C4", _sev(cfg, "C4"), "ask",
                                       f"Note {n.number or i} uses ITEM {it} ({row.get('description')}).", s, n.text,
                                       f"note {n.number or i}", n.bbox,
                                       ask={"type": "note_item", "note": n.text, "item": it,
                                            "item_text": f"{row.get('description') or ''} / {row.get('ref') or ''}".strip(" /")}))
        # C5 SEE targets (in notes, title block, loose text)
        all_text = [(w, t, b) for w, t, b in text_lines(s)]
        tb = s.record["title_block"]
        for f_, v in tb.items():
            if v and re.match(r"\s*SEE\b", v, re.I):
                all_text.append((f"title block {f_.upper()}", v, None))
        for where, text, bbox in all_text:
            for m in SEE_RX.finditer(text):
                what, tag = m.group(1).upper(), m.group(2).strip("'‘’\"“”")
                if not _see_target(s, what, tag, where):
                    out.append(Finding("C5", _sev(cfg, "C5"), "doubt" if s.ocr else "fail",
                                       f"{where} says '{m.group(0).strip()}', but there is no such {what.lower()} on this sheet.",
                                       s, m.group(0), where, bbox or locate(s, m.group(0))))
        # C6 USE FILE names this drawing
        for r in smap.of(s, "file"):
            if r.targets and s not in r.targets:
                out.append(Finding("C6", _sev(cfg, "C6"), "fail",
                                   f"{r.where}: '{r.text}' names {r.targets[0].number}, not this drawing ({s.number}).",
                                   s, r.text, r.where, r.bbox, related=r.targets))
        # C7 tool series
        tools = [r for r in smap.of(s, "tool")]
        if tools:
            from sheetreader.cells import normalize_ocr
            names = sorted({normalize_ocr(_ocr_prefix_fix(r.number, smap)) if r.ocr else r.number for r in tools})
            out.append(Finding("C7", _sev(cfg, "C7"), "fail",
                               "Jig / mould / template numbers on this sheet (not drawings in this folder - confirm they exist "
                               "and are current): " + ", ".join(names) + ".", s, names[0], tools[0].where, tools[0].bbox))
    return out


def _ocr_prefix_fix(num, smap):
    """'XI-JIG-…' read by OCR for the set's 'XT-' prefix: show the prefix as the set writes it."""
    from sheetreader.normalize import ocr_key
    head = re.match(r"([A-Z0-9]{1,5})(?=[-–—\s])", num)
    for pat in smap.patterns:
        comp = pat.company
        if comp and head and head.group(1) != comp and ocr_key(head.group(1)) == ocr_key(comp):
            return comp + num[head.end():]
    return num


def _see_target(s, what, tag, where) -> bool:
    if what.startswith("NOTE"):
        if not s.notes:
            return False
        field = re.match(r"title block (\w+)", where)
        if field:
            label = field.group(1).replace("_BY", "")
            return any(re.match(rf"\s*{re.escape(label)}\b", n.text, re.I) for n in s.notes) or bool(s.notes)
        return True
    words = " ".join(w.text for w in s.sheet.words).upper()
    field = re.match(r"title block (\w+)", where)
    if what == "TABLE" and field:
        label = field.group(1).replace("_BY", "")
        tb = s.parts["title_block"].region
        outside = [w for w in s.sheet.words if not (tb and tb[0] <= w.xc <= tb[2] and tb[1] <= w.yc <= tb[3])]
        if s.record.get("bom") or any(label in w.text.upper() for w in outside):
            return True
    if what == "TABLE":
        if not tag:
            return bool(s.record.get("bom")) or "TABLE" in words.replace("SEE TABLE", "")
        return bool(re.search(rf"\bTABLE\s*{re.escape(tag)}\b", re.sub(r"SEE\s+TABLE", "", words)))
    if what in ("DETAIL", "VIEW", "SECTION"):
        if not tag:
            return True
        return any(re.search(rf"\b{what}\s*['‘’\"]?{re.escape(tag)}\b", l.text, re.I)
                   for b in s.blocks_of("view_label") + s.blocks_of("unplaced") for l in b.lines
                   if not l.text.upper().startswith("SEE"))
    if what.endswith("DETAILS"):
        return any(b.meta.get("heading", "").upper() == what for b in s.blocks_of("notes")) or \
            re.sub(r"SEE\s+" + what, "", words).count(what) > 0
    return True


# ---------------------------------------------------------------------------
# D. Notes
# ---------------------------------------------------------------------------
def check_D(sheets, cfg, smap=None) -> List[Finding]:
    out = []
    for s in sheets:
        blocks = defaultdict(list)
        for n in s.notes:
            blocks[n.block].append(n)
        # D1 numbering
        for b, ns in blocks.items():
            nums = [int(n.number) for n in ns if n.number and n.number.isdigit()]
            if len(nums) >= 2:
                expect = list(range(1, len(nums) + 1))
                if nums != expect:
                    out.append(Finding("D1", _sev(cfg, "D1"), "doubt" if s.ocr else "fail",
                                       f"Notes are numbered {', '.join(map(str, nums))} (expected 1 to {len(nums)}).", s,
                                       ns[0].text, "notes", None))
        # D2 note values vs title block
        tb = s.record["title_block"]
        for f_ in ("weight", "material", "scale", "finish"):
            v = (tb.get(f_) or "").strip()
            noted = [n for n in s.notes if re.match(rf"\s*{f_}\s*[:.-]", n.text, re.I)]
            if re.match(r"SEE\s+NOTE", v, re.I) and not noted:
                out.append(Finding("D2", _sev(cfg, "D2"), "doubt" if s.ocr else "fail",
                                   f"Title block {f_.upper()} says 'SEE NOTE', but no note gives the {f_}.", s, v,
                                   f"title block {f_.upper()}"))
            if v and not re.match(r"SEE\b", v, re.I) and noted:
                for n in noted:
                    nv = re.sub(rf"^\s*{f_}\s*[:.-]\s*", "", n.text, flags=re.I)
                    if alnum(nv) != alnum(v) and not (f_ == "weight" and _same_numbers(nv, v)):
                        out.append(Finding("D2", _sev(cfg, "D2"), "doubt" if s.ocr else "fail",
                                           f"A note gives {f_.upper()} '{nv}', the title block says '{v}'.", s, n.text,
                                           "note", n.bbox))
        # D5 legend <-> symbol
        legends = [l.text for b in s.blocks_of("legend") for l in b.lines]
        star_used = any(re.match(r"^\*\s*\d", w.text) for w in s.sheet.words)
        star_legend = any(re.match(r"^['\"‘’`]?\*", t) for t in legends)
        if star_used and not star_legend:
            out.append(Finding("D5", _sev(cfg, "D5"), "fail",
                               "Dimensions are marked '*' but there is no '*' legend (e.g. '*' - KEY CHARACTERISTICS).", s, "*"))
        if star_legend and not star_used and not s.ocr:
            out.append(Finding("D5", _sev(cfg, "D5"), "fail",
                               "The '*' legend is on the sheet, but no dimension is marked '*'.", s,
                               next(t for t in legends if "*" in t), "legend"))
        # D4 note clear and complete: one judgement per notes block
        for b, ns in blocks.items():
            if ns:
                out.append(Finding("D4", _sev(cfg, "D4"), "ask", "Notes reviewed for completeness and clarity.", s,
                                   ns[0].text, "notes", None, ask={"type": "notes", "notes": [n.text for n in ns]}))
    # D3 the same standard note worded differently on different sheets
    notes = [(s, n) for s in sheets if not s.ocr for n in s.notes]
    done = set()
    for i, (s1, n1) in enumerate(notes):
        for s2, n2 in notes[i + 1:]:
            if s1 is s2:
                continue
            a, b = _norm(_mask(n1.text, smap)), _norm(_mask(n2.text, smap))
            if a == b or len(a) < 20:
                continue
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r >= 0.9 and (a, b) not in done:
                done.add((a, b))
                out.append(Finding("D3", _sev(cfg, "D3"), "fail",
                                   f"Same note worded differently: '{n1.text}' ({s1.number}) vs '{n2.text}' ({s2.number}).",
                                   s1, n1.text, "note", n1.bbox, related=[s2]))
    return out


def _mask(t, smap):
    """Replace drawing numbers with a placeholder: 'USE FILE <no>.STEP' notes
    differ only by number and are not wording differences."""
    if smap is None:
        return t
    for pat in smap.patterns:
        t = pat.regex.sub("#", t)
    return t


def _norm(t):
    return re.sub(r"[^A-Z0-9 ]", "", re.sub(r"\s+", " ", t.upper())).strip()


def _same_numbers(a, b):
    na = re.findall(r"\d+(?:\.\d+)?", a)
    nb = re.findall(r"\d+(?:\.\d+)?", b)
    return na == nb


# ---------------------------------------------------------------------------
# E. Whole set
# ---------------------------------------------------------------------------
def check_E(sheets, cfg, smap=None) -> List[Finding]:
    """Whole-set comparisons, made within one product (numbering family):
    a folder holding several products is compared product by product."""
    if smap is not None and len(smap.patterns) > 1:
        fams = defaultdict(list)
        for s in sheets:
            fams[smap.family_of(s)].append(s)
        return [f for fam in fams.values() for f in check_E(fam, cfg, None)]
    out = []
    sigs = defaultdict(list)
    for s in sheets:
        gt = s.record.get("general_tolerance") or {}
        sig = tuple(gt.get(k) for k in ("without_decimal", "one_decimal", "two_decimal"))
        if all(sig):
            sigs[sig].append(s)
    if len(sigs) > 1:
        major, majority = max(sigs.items(), key=lambda kv: len(kv[1]))
        if len(majority) >= 3:
            for sig, ss in sigs.items():
                if sig == major:
                    continue
                for s in ss:
                    out.append(Finding("E1", _sev(cfg, "E1"), "doubt" if s.ocr and len(ss) == 1 and False else "fail",
                                       f"General tolerances {', '.join(sig)} (none / one / two decimals); the other "
                                       f"{len(majority)} drawings use {', '.join(major)}. Confirm this is intended.",
                                       s, "TOLERANCES", "title block", locate(s, "TOLERANCES UNLESS")))
    dates = Counter(s.record["title_block"].get("date") for s in sheets if s.record["title_block"].get("date"))
    if len(dates) > 1:
        common, n = dates.most_common(1)[0]
        if n >= 3:
            for s in sheets:
                d = s.record["title_block"].get("date")
                if d and d != common:
                    out.append(Finding("E2", _sev(cfg, "E2"), "doubt" if s.ocr else "fail",
                                       f"Date {d}; {n} other drawings in this set are dated {common}.", s, d, "title block DATE",
                                       locate(s, d)))
    return out


def run_all(sheets, smap, cfg) -> List[Finding]:
    return check_A(sheets, smap, cfg) + check_B(sheets, cfg, smap) + check_C(sheets, smap, cfg) + \
        check_D(sheets, cfg, smap) + check_E(sheets, cfg, smap)
