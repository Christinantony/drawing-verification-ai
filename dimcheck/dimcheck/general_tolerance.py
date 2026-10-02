"""
Parse the drawing's general/default tolerance block -- the little table in
most title blocks that reads something like:

    WITHOUT DECIMAL : ±1
    ONE DECIMAL     : ±0.1
    TWO DECIMAL     : ±0.05
    ANGLE           : ±1/2°

and look up which line applies to a given dimension based on how many
decimal places it's printed with.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class GeneralToleranceTable:
    without_decimal: Optional[str] = None
    one_decimal: Optional[str] = None
    two_decimal: Optional[str] = None
    angle: Optional[str] = None
    raw_matches: Optional[dict] = None

    def lookup_by_decimals(self, decimals: int) -> Optional[str]:
        return {0: self.without_decimal, 1: self.one_decimal, 2: self.two_decimal}.get(decimals)


# "1/2" must be tried before the plain-number branch, otherwise "±1/2°"
# matches as "±1".
_VALUE_RE = re.compile(r"±\s*(1/2|½|[0-9]+(?:\.[0-9]+)?)\s*°?")


def _find_value_near(text: str, keyword_pattern: str) -> Optional[str]:
    m = re.search(keyword_pattern, text, re.IGNORECASE)
    if not m:
        return None
    window = text[m.end(): m.end() + 40]
    vm = _VALUE_RE.search(window)
    if not vm:
        return None
    val = vm.group(1)
    if val in ("½", "1/2"):
        val = "0.5"
    has_degree = "°" in window[: vm.end() + 2]
    return f"±{val}" + ("°" if has_degree else "")


def parse_general_tolerance_table(flat_text: str) -> GeneralToleranceTable:
    without_decimal = _find_value_near(flat_text, r"WITHOUT\s+DECIMAL")
    one_decimal = _find_value_near(flat_text, r"\bONE\s+DECIMAL")
    two_decimal = _find_value_near(flat_text, r"\bTWO\s+DECIMAL")
    angle = _find_value_near(flat_text, r"\bANGLE\b")
    return GeneralToleranceTable(
        without_decimal=without_decimal,
        one_decimal=one_decimal,
        two_decimal=two_decimal,
        angle=angle,
    )


def count_decimals(numeric_text: str) -> Optional[int]:
    """How many digits appear after the decimal point in a plain numeric
    string like '10.0' or '3.5'. Returns None if numeric_text isn't a plain
    number (e.g. a thread callout like 'M3')."""
    m = re.match(r"^\s*-?\d+(\.(\d+))?\s*$", numeric_text)
    if not m:
        return None
    return len(m.group(2)) if m.group(2) else 0
