"""Station 4: map the set.

Every reference written anywhere on any sheet - title block, BOM, notes,
view labels, callouts, loose text - is found and resolved against the
drawings in the folder:

    drawing   XY-AB 12 C3-E-07, NEXT ASSY., BOM refs, view labels
    file      USE FILE XY-AB 12 C3-E-07.STEP  (must name its own drawing)
    tool      XY-JIG-..., XY-ML-...           (jigs / moulds: listed for you)
    item      "ITEM NO. 10" in a note          (must be in that sheet's BOM)
    see       SEE NOTE / TABLE 2 / DETAIL A    (target must be on the sheet)

The numbering pattern (company prefix, project core, category letter) is
learnt from the drawing numbers in the set itself, so nothing about your
numbering is hard-coded.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "coherence"):
    p = str(ROOT / sub) if sub else str(ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)

from sheetreader.normalize import alnum, ocr_key  # noqa: E402

TOOL_WORDS = {"JIG", "JIGS", "ML", "MOULD", "MOLD", "FIX", "FXT", "FIXTURE", "TOOL", "GAUGE", "GA", "CT"}


@dataclass
class Ref:
    kind: str                 # drawing | file | tool | item | see
    text: str                 # exactly as written
    sheet: object             # QCSheet it was written on
    where: str                # "BOM item 3", "note 2", "NEXT ASSY.", "view label", ...
    bbox: Optional[tuple] = None
    ocr: bool = False
    targets: list = field(default_factory=list)   # QCSheets it resolves to
    status: str = ""          # ok | styled | missing | unsure | self
    detail: str = ""
    number: str = ""          # the drawing-number part (without file extension)
    extension: str = ""


@dataclass
class Pattern:
    company: str              # "VT"
    core: List[str]           # ["JH", "40", "A1"]
    category: str             # "E"
    regex: re.Pattern

    def describe(self) -> str:
        return f"{self.company + '-' if self.company else ''}{' '.join(self.core)}{'-' + self.category if self.category else ''}-…"


def _tokens(num: str) -> List[str]:
    return [t for t in re.split(r"[\s\-–—_]+", num.upper()) if t]


def learn_pattern(numbers: List[str]) -> Optional[Pattern]:
    toks = [_tokens(n) for n in numbers if n]
    if not toks:
        return None
    common = []
    for parts in zip(*toks):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    if not common:
        return None
    company = common[0] if len(common) > 1 and common[0].isalpha() and len(common[0]) <= 4 else ""
    rest = common[1:] if company else common
    category = rest[-1] if len(rest) > 1 and rest[-1].isalpha() and len(rest[-1]) <= 2 else ""
    core = rest[:-1] if category else rest
    if not core:
        core, category = rest, ""
    look = {"0": "0OQD", "O": "0OQD", "1": "1IlLT|", "I": "1IlLT|", "L": "1IlLT|", "T": "1IlLT|",
            "5": "5S$", "S": "5S$", "8": "8B", "B": "8B", "2": "2Z", "Z": "2Z", "6": "6G", "G": "6G"}

    def tok_rx(t):
        # Each character also matches its OCR look-alikes; one stray
        # character may follow a token ('Al1' read for 'A1').
        return "".join(f"[{re.escape(look[c])}]" if c in look else re.escape(c) for c in t) + r"[A-Za-z0-9]?"

    sep = r"[\s\-–—]*"
    rx = (r"(?<![A-Za-z0-9])(?P<prefix>(?:[A-Z]{1,6}\s*[-–—]\s*){0,3})"
          + sep.join(tok_rx(t) for t in core)
          + r"(?P<suffix>(?:\s*[-–—]\s*[A-Z0-9&]+)*(?:\s+[A-Z]\s*&\s*[A-Z](?![A-Za-z0-9]))?)"
          + r"(?P<ext>\.[A-Za-z0-9]{2,5}(?:/[A-Za-z0-9]{2,5})*)?")
    return Pattern(company, core, category, re.compile(rx, re.I))


def learn_patterns(numbers: List[str]) -> List[Pattern]:
    """One numbering family per product in the folder. A family key is a
    number without its last segment (the serial); keys that are prefixes of
    each other ('…A1-E' and '…A1-E-01' from '…-E-01-INS') are one family."""
    keys = []
    for n in numbers:
        t = _tokens(n)
        if len(t) >= 2:
            keys.append(t[:-1])
    keys.sort(key=len)
    fams: List[List[List[str]]] = []
    for k in keys:
        for f in fams:
            base = f[0]
            if k[:len(base)] == base:
                f.append(k)
                break
        else:
            fams.append([k])
    out = []
    for f in fams:
        # learn from the family's numbers (keys + a dummy serial keeps the
        # common-prefix logic identical to learn_pattern)
        p = learn_pattern([" ".join(k) + " 00" for k in f] + [" ".join(f[0]) + " 99"])
        if p and p.core and all(p.regex.pattern != q.regex.pattern for q in out):
            out.append(p)
    return out


def find_numbers(text: str, pat: Pattern):
    """(start, end, prefix_tokens, number_text, extension) for each mention."""
    for m in pat.regex.finditer(text or ""):
        num = text[m.start():m.start("ext")] if m.group("ext") else m.group(0)
        num = num.rstrip(" .,;:")
        yield m.start(), m.end(), _tokens(m.group("prefix") or ""), num, (m.group("ext") or "")


ITEM_RX = re.compile(r"\bITEMS?\s*(?:NO\.?|NUMBER)?\s*[.:]?\s*[(\[]?\s*(\d{1,3})\s*[)\]]?", re.I)
SEE_RX = re.compile(r"\b(?:SEE|REFER(?:\s+TO)?)\s+(NOTES?|TABLE|DETAIL|VIEW|SECTION|[A-Z]+\s+DETAILS)\b\s*([A-Z0-9'‘’\"“”]{0,4})", re.I)


def item_refs(text: str) -> List[str]:
    """Item numbers named in a note: 'ITEM NO. (13) TO TIE ITEM NO. (3)',
    'APPLY ITEM NO.10', 'ITEM (3) AND ITEM (5)', 'ITEMS 3 & 4'."""
    out = [m.group(1) for m in ITEM_RX.finditer(text or "")]
    for m in re.finditer(r"\bITEMS\s+((?:\d{1,3}\s*(?:,|&|AND)\s*)+\d{1,3})", text or "", re.I):
        out += re.findall(r"\d{1,3}", m.group(1))
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
class SetMap:
    def __init__(self, sheets):
        from check_coherence import id_keys  # noqa: E402
        self.id_keys = id_keys
        self.sheets = sheets
        nums = [s.record.get("filename_drawing_number") or s.number for s in sheets]
        self.patterns = learn_patterns([n for n in nums if n])
        self.pattern = self.patterns[0] if self.patterns else None
        self.by_key: Dict[str, list] = {}
        for s in sheets:
            for n in {s.number, s.record.get("filename_drawing_number")}:
                for k in id_keys(n):
                    self.by_key.setdefault(k, [])
                    if s not in self.by_key[k]:
                        self.by_key[k].append(s)
        self.by_ocr: Dict[str, list] = {}
        for k, ss in self.by_key.items():
            self.by_ocr.setdefault(ocr_key(k), [])
            self.by_ocr[ocr_key(k)] += [x for x in ss if x not in self.by_ocr[ocr_key(k)]]
        self.refs: List[Ref] = []
        if self.patterns:
            self._collect()
            for r in self.refs:
                self._resolve(r)

    # -- collection --------------------------------------------------------
    def mentions(self, text):
        """(start, end, prefix, number, ext, pattern) for every drawing-number
        mention in text, over all numbering families, longest match first."""
        hits = []
        for pat in self.patterns:
            for start, end, prefix, num, ext in find_numbers(text, pat):
                hits.append((start, end, prefix, num, ext, pat))
        hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
        out, last = [], -1
        for h in hits:
            if h[0] >= last:
                out.append(h)
                last = h[1]
        return out

    def _add(self, sheet, text, where, bbox):
        for start, end, prefix, num, ext, pat in self.mentions(text):
            extra = [t for t in prefix if ocr_key(t) != ocr_key(pat.company)]
            if not self._suffix(num, pat):
                continue   # the bare project number ("AB 12 C3"): a product name, not a drawing
            kind = "tool" if extra else ("file" if ext else "drawing")
            r = Ref(kind, text[start:end], sheet, where, bbox, sheet.ocr, number=num, extension=ext)
            r.pattern = pat
            self.refs.append(r)

    def _suffix(self, num: str, pat=None) -> str:
        """The part of a written number after the project core."""
        pat = pat or self.pattern
        m = re.search(r"[\s\-–—]*".join(re.escape(c) for c in pat.core), num, re.I)
        if m:
            return num[m.end():]
        # OCR look-alikes inside the core: fall back to the regex match end
        mm = pat.regex.search(num)
        return (mm.group("suffix") or "") if mm else ""

    def family_of(self, sheet) -> int:
        n = sheet.record.get("filename_drawing_number") or sheet.number or ""
        for i, pat in enumerate(self.patterns):
            if pat.regex.search(n):
                return i
        return -1

    def describe(self) -> str:
        return "; ".join(p.describe() for p in self.patterns) or "not recognised"

    def _collect(self):
        for s in self.sheets:
            tb = s.record["title_block"]
            if tb.get("drawing_number"):
                self._add(s, tb["drawing_number"], "title block DRAWING No.", None)
            if tb.get("next_assy"):
                self._add(s, tb["next_assy"], "NEXT ASSY.", None)
            for row in s.record.get("bom") or []:
                for k in ("ref", "description"):
                    if row.get(k):
                        self._add(s, row[k], f"BOM item {row.get('item') or '?'}", None)
            for i, n in enumerate(s.notes, 1):
                self._add(s, n.text, f"note {n.number or i}", n.bbox)
            for b in s.blocks:
                if b.kind in ("view_label", "callout", "legend", "unplaced") or (b.kind == "notes" and not b.meta.get("is_notes")):
                    if b.kind == "notes":
                        continue  # its lines are already in s.notes
                    for l in b.lines:
                        self._add(s, l.text, b.kind.replace("_", " "), l.bbox)
        # A drawing's own number written in its title block is the anchor,
        # not a reference.
        for r in self.refs:
            if r.where == "title block DRAWING No.":
                r.status = "self"

    # -- resolution --------------------------------------------------------
    def find(self, number: str) -> list:
        out = []
        for k in self.id_keys(number):
            for s in self.by_key.get(k, []):
                if s not in out:
                    out.append(s)
        return out

    def _variants(self, number: str, p=None) -> List[Tuple[str, str]]:
        """Ways the written number may differ from the set's style:
        (candidate, what differs)."""
        p = p or self.pattern
        out = []
        t = number.upper()
        suf = self._suffix(t, p)
        head = t[: len(t) - len(suf)] if suf else t
        padded = head + re.sub(r"(?<![0-9])(\d)(?![0-9])", r"0\1", suf)
        if padded != t:
            out.append((padded, "a leading zero is missing"))
        noz = head + re.sub(r"(?<![0-9])0+(?=[0-9])", "", suf)
        if noz != t:
            out.append((noz, "has an extra leading zero"))
        core_rx = r"[\s\-–—]*".join(re.escape(c) for c in p.core)
        if p.category and not re.search(core_rx + r"[\s\-–—]*" + re.escape(p.category) + r"[\s\-–—]", t):
            out.append((re.sub("(" + core_rx + ")", r"\1-" + p.category, t, count=1), f"'{p.category}-' is missing"))
        if p.company and not t.startswith(p.company):
            out.append((p.company + "-" + t, f"'{p.company}-' prefix is missing"))
        return out

    def _resolve(self, r: Ref):
        if r.kind in ("tool",) or r.status == "self":
            if r.kind == "tool":
                r.status = "tool"
            return
        r.targets = self.find(r.number)
        if r.targets:
            r.status = "ok"
            return
        if r.ocr:
            hit = self.by_ocr.get(ocr_key(r.number))
            if hit:
                r.targets, r.status = hit, "ok"
                r.detail = f"OCR read '{r.number}' (look-alike characters); taken as {hit[0].record.get('filename_drawing_number') or hit[0].number}"
                return
        pat = getattr(r, "pattern", None) or self.pattern
        for cand, why in self._variants(r.number, pat):
            hit = self.find(cand)
            if hit:
                r.targets, r.status = hit, "styled"
                r.detail = f"written as '{r.number}'; the drawing is {hit[0].record.get('filename_drawing_number') or hit[0].number} ({why})"
                return
        # Several variants at once (missing category and a leading zero).
        for cand, why in self._variants(r.number, pat):
            for cand2, why2 in self._variants(cand, pat):
                hit = self.find(cand2)
                if hit:
                    r.targets, r.status = hit, "styled"
                    r.detail = f"written as '{r.number}'; the drawing is {hit[0].record.get('filename_drawing_number') or hit[0].number} ({why}; {why2})"
                    return
        if r.ocr:
            want = ocr_key(r.number)
            near = {k: ss for k, ss in self.by_ocr.items() if _one_edit(k, want)}
            if len(near) == 1:
                ss = next(iter(near.values()))
                if True:
                    r.targets, r.status = ss, "unsure"
                    r.detail = f"OCR read '{r.number}'; probably {ss[0].record.get('filename_drawing_number') or ss[0].number}"
                    return
        r.status = "missing"

    # -- views -------------------------------------------------------------
    def of(self, sheet, kind=None):
        return [r for r in self.refs if r.sheet is sheet and (kind is None or r.kind == kind)]

    def parents_of(self, sheet) -> list:
        out = []
        for r in self.refs:
            if r.where == "NEXT ASSY." and r.sheet is sheet:
                out += [t for t in r.targets if t not in out]
        return out

    def children_of(self, sheet) -> list:
        out = []
        for r in self.refs:
            if r.sheet is sheet and r.where.startswith("BOM item"):
                out += [t for t in r.targets if t not in out and t is not sheet]
        return out


def _one_edit(a: str, b: str) -> bool:
    """Exactly one inserted or deleted character. Substitutions are not
    allowed: E-06 and E-01 differ by one character and are different drawings
    (OCR look-alike substitutions are handled by ocr_key)."""
    if min(len(a), len(b)) < 8 or abs(len(a) - len(b)) != 1:
        return False
    if len(a) < len(b):
        a, b = b, a
    return any(a[:i] + a[i + 1:] == b for i in range(len(a)))
