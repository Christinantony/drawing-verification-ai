"""
Comparing identifiers read off drawings.

Part / drawing numbers are compared on their letters and digits only, so
"AT-KD_M12-E-03_AB" (a file name) and "AT-KD M12-E-03 A&B" (the title block)
are the same number. Text that came from OCR is also allowed the classic
look-alike substitutions (I/T/1/L, O/0/D/Q, S/5/$, B/8, Z/2, G/6), so a sheet
whose title block OCR'd as "AI-KD M12-E-02" still matches "AT-KD M12-E-02".
Text from a PDF text layer is never given that allowance: there, a
difference is a real difference.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

_CONFUSABLE = str.maketrans({
    "I": "1", "T": "1", "L": "1", "|": "1", "!": "1",
    "O": "0", "D": "0", "Q": "0",
    "S": "5", "$": "5",
    "B": "8",
    "Z": "2",
    "G": "6",
})


def alnum(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def ocr_key(s: Optional[str]) -> str:
    """Identifier with look-alike characters collapsed. Keeps '$' long enough
    to be mapped before non-alphanumerics are dropped."""
    up = (s or "").upper().replace("&", "")
    up = up.translate(_CONFUSABLE)
    return re.sub(r"[^A-Z0-9]", "", up)


def same_id(a: Optional[str], b: Optional[str], ocr: bool = False) -> bool:
    if not a or not b:
        return False
    if alnum(a) == alnum(b):
        return True
    return ocr and ocr_key(a) == ocr_key(b)


def ids_from_filename(path: str) -> Tuple[Optional[str], Optional[str]]:
    """Drawing number and revision encoded in a file name such as
    'AT-PX_2B_40-03_REV._00.pdf' -> ('AT-PX 2B 40-03', '00').
    Checklist names like 'AT-F-DCL-PX_2B_40-03_REV._00.doc' are handled
    by the caller stripping its own prefix."""
    stem = Path(path).stem
    stem = re.sub(r"^[0-9a-f]{8}-", "", stem)  # upload prefixes
    m = re.search(r"[_\s-]*REV(?:ISION)?[._\s-]*([A-Z0-9]{1,3})(?=$|[_\s.-])", stem, re.I)
    rev = m.group(1) if m else None
    base = stem[:m.start()] if m else stem
    base = re.sub(r"_+", " ", base).strip()
    return (base or None), rev


def rev_key(rev: Optional[str]) -> Optional[Tuple[int, str]]:
    """Sortable form of a revision code: numbers numerically, letters
    alphabetically ('00' < '01' < '02'; 'A' < 'B' < 'AA')."""
    if not rev:
        return None
    r = rev.strip().upper()
    if r.isdigit():
        return (0, f"{int(r):06d}")
    if r.isalpha():
        return (1, f"{len(r):02d}{r}")
    return (2, r)


QTY_ASREQ = re.compile(r"^(A\s*[/|IJ1LM]\s*R|A\.R\.?|AS\s*REQ'?D?|AS\s*REQUIRED)$", re.I)
