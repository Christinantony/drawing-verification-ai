"""
Spelling / wording check on the drawing's text, in three layers:

1. KNOWN_MISSPELLINGS - common engineering misspellings and wrong-word
   pairs ("ANEROBIC", "SILICON SEALANT"). Exact, instant, no model.
2. Dictionary - words an English dictionary doesn't know (pyspellchecker,
   bundled, offline), minus ENGINEERING_WORDS. On their own these are only
   "possible" findings: drawings are full of trade names.
3. Gemma - reviews just the short list of text lines (title, notes, BOM,
   revision details) with the dictionary candidates as hints, thinking off.
   ~300 tokens in, a short JSON list out: well under a minute at ~7 tok/s.
   Any phrase the model reports must actually occur in the input, so it
   can't invent a finding.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

# Words a general dictionary flags but that are normal on drawings.
# Add your own in config.EXTRA_ENGINEERING_WORDS.
ENGINEERING_WORDS = {
    "aluminium", "aluminum", "araldite", "loctite", "anabond", "helicoil", "chromatization",
    "chromate", "passivation", "passivate", "anodize", "anodized", "anodizing", "polyurethane",
    "countersunk", "counterbore", "csk", "cbore", "thru", "unc", "unf", "pcd", "swg", "awg", "tnc",
    "sma", "bnc", "vswr", "mhz", "ghz", "fr4", "rtv", "ptfe", "nylon", "delrin", "helicoils",
    "knurl", "knurled", "deburr", "debur", "chamfer", "fillet", "typ", "ref", "matl", "qty", "nos",
    "assy", "drg", "dwg", "rev", "otherwise", "radome", "balun", "nts", "mdi", "tinned",
    "fasteners", "sealant", "epoxy", "primer", "matt", "shrink",
}

# phrase (regex, case-insensitive) -> correction, reason
KNOWN_MISSPELLINGS: List[Tuple[str, str, str]] = [
    (r"\bANEROBIC\b", "ANAEROBIC", "'Anaerobic' (cures without air, e.g. Loctite thread-lockers) is misspelt."),
    (r"\bSILICON\s+(SEALANT|RUBBER|ADHESIVE|GASKET|COMPOUND|GREASE|OIL|POTTING)\b", "SILICONE \\1",
     "Sealants, rubbers and adhesives are silicone; silicon is the element."),
    (r"\bALLUMIN(I)?UM\b", "ALUMINIUM", "Misspelt aluminium."),
    (r"\bGUAGE\b", "GAUGE", "Misspelt gauge."),
    (r"\bSEPERATE\b", "SEPARATE", "Misspelt separate."),
    (r"\bSTAINLESS\s+STEAL\b", "STAINLESS STEEL", "Steal/steel."),
    (r"\bBRAKCET\b", "BRACKET", "Misspelt bracket."),
    (r"\bTOLERANCE\s+UNLESS\b", "TOLERANCES UNLESS", "Standard wording is 'TOLERANCES UNLESS OTHERWISE SPECIFIED'."),
    (r"\bTHREADS?\s+HOLE\b", "THREADED HOLE", "Wording: threaded hole."),
]


def text_lines(rec: dict) -> List[Tuple[str, str]]:
    """(location, text) pairs worth checking for language."""
    out: List[Tuple[str, str]] = []
    tb = rec["title_block"]
    for k in ("title", "material", "finish"):
        if tb.get(k):
            out.append((f"title_block.{k}", tb[k]))
    for i, n in enumerate(rec.get("notes") or []):
        out.append((f"notes.{i + 1}", n))
    for row in rec.get("bom") or []:
        loc = f"bom.item_{row.get('item') or '?'}"
        for k in ("description", "ref"):
            if row.get(k):
                out.append((f"{loc}.{k}", row[k]))
    for r in rec.get("revision_table") or []:
        if r.get("details"):
            out.append((f"revision_table.rev_{r.get('rev', '?')}", r["details"]))
    return out


def known_misspellings(lines: Iterable[Tuple[str, str]]) -> List[dict]:
    found = []
    for loc, text in lines:
        for rx, fix, why in KNOWN_MISSPELLINGS:
            if fix is None:
                continue
            for m in re.finditer(rx, text, re.I):
                found.append({"severity": "medium", "category": "spelling", "location": loc,
                              "description": f"'{m.group(0)}': {why}",
                              "suggested_fix": m.expand(fix).upper(), "source": "rule"})
    return found


def dictionary_candidates(lines: Iterable[Tuple[str, str]], extra_words: Iterable[str] = ()) -> Dict[str, Tuple[str, Optional[str]]]:
    """word -> (location, suggestion) for words the dictionary doesn't know."""
    try:
        from spellchecker import SpellChecker
    except ImportError:
        return {}
    sp = SpellChecker()
    allow = ENGINEERING_WORDS | {w.lower() for w in extra_words}
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    for loc, text in lines:
        for tok in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text):
            w = tok.lower().strip("'-")
            # Four letters or fewer on a drawing is almost always an
            # abbreviation (ASTM, MATT, TYP); 'XXXX' placeholders are intended.
            if len(w) <= 4 or w in allow or "-" in w or w in out or len(set(w)) == 1:
                continue
            if w in sp.unknown([w]):
                sug = sp.correction(w)
                out[tok] = (loc, sug.upper() if sug and sug != w else None)
    return out


LANGUAGE_SYSTEM = """You proofread text taken from a mechanical engineering drawing.
Report ONLY real spelling mistakes and clearly wrong technical words (for example 'SILICON' \
used for a sealant, which should be 'SILICONE'). Do NOT report: abbreviations (QTY, TYP, CSK, \
SWG, NOS), part numbers, brand or trade names, all-caps style, or missing punctuation.{ocr_hint}
Answer with ONLY a JSON array, empty if nothing is wrong:
[{{"text": "<the exact wrong word or phrase as it appears>", "correction": "<fixed text>", "reason": "<short>"}}]"""


def model_check(lines: List[Tuple[str, str]], candidates: Dict[str, tuple], client, model: str,
                ocr: bool, options: Optional[dict] = None) -> Tuple[List[dict], str]:
    """Ask Gemma about the text lines. Returns (findings, raw_response)."""
    from ollama_client import chat_with_thinking, extract_json_list

    hint = (" This text was read by OCR: ignore single characters that look like OCR "
            "slips (I for T, 0 for O, $ for S).") if ocr else ""
    body = "\n".join(f"- {t}" for _, t in lines)
    if candidates:
        body += "\n\nA dictionary did not recognise these words (they may still be fine): " + \
                ", ".join(sorted(candidates))
    _, content = chat_with_thinking(client, LANGUAGE_SYSTEM.format(ocr_hint=hint),
                                    "Text lines:\n" + body, model=model, use_thinking=False,
                                    options=options)
    items = extract_json_list(content) or []
    findings = []
    all_text = "\n".join(t for _, t in lines).upper()
    for it in items:
        if not isinstance(it, dict):
            continue
        txt = str(it.get("text") or "").strip()
        if not txt or txt.upper() not in all_text:
            continue  # the model must quote something that is really there
        loc = next((l for l, t in lines if txt.upper() in t.upper()), "text")
        corr = it.get("correction")
        if corr and str(corr).strip().upper() == txt.upper():
            continue
        findings.append({"severity": "low", "category": "spelling", "location": loc,
                         "description": f"'{txt}': {it.get('reason') or 'possible error'}",
                         "suggested_fix": corr, "source": "model"})
    return findings, content


def check_language(rec: dict, client=None, model: Optional[str] = None,
                   extra_words: Iterable[str] = (), options: Optional[dict] = None) -> Tuple[List[dict], dict]:
    """All language findings for a record, plus trace info for the report."""
    lines = text_lines(rec)
    findings = known_misspellings(lines)
    flagged = {f["description"].split("'")[1].upper() for f in findings}
    cands = dictionary_candidates(lines, extra_words)
    ocr = rec.get("text_source") == "ocr"
    trace = {"lines_checked": len(lines), "dictionary_candidates": sorted(cands), "model_response": None}

    if client is not None and model and lines:
        try:
            mf, raw = model_check(lines, cands, client, model, ocr, options)
            trace["model_response"] = raw
            for x in mf:
                quoted = x["description"].split("'")[1].upper()
                if not any(quoted in fl or fl in quoted for fl in flagged):
                    findings.append(x)
                    flagged.add(quoted)
        except Exception as e:  # model offline etc. - rules still stand
            trace["model_error"] = str(e)

    if trace["model_response"] is None and not ocr:
        # No model: dictionary findings on their own, as low-severity hints.
        for word, (loc, sug) in cands.items():
            if word.upper() in flagged:
                continue
            findings.append({"severity": "low", "category": "spelling", "location": loc,
                             "description": f"'{word}' is not in the dictionary (could be a trade name).",
                             "suggested_fix": sug, "source": "dictionary"})
    return findings, trace
