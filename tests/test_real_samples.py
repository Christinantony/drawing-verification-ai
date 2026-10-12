"""Regression tests against your own drawings, kept OUT of the repository.

Put the drawings, checklists and an expected.json in a local folder and run:

    set DVA_SAMPLES_DIR=C:\\path\\to\\samples        (Windows)
    export DVA_SAMPLES_DIR=/path/to/samples           (Linux/macOS)
    python -m pytest tests/test_real_samples.py -v

expected.json:
{
  "drawings": {
    "<file>.pdf": {
      "text_source": "embedded" | "ocr",
      "title_block": {"drawing_number": "...", "revision": "...", ...},   # OCR'd values are compared allowing look-alikes
      "bom_items": ["1", "2", ...],
      "general_tolerance": {"one_decimal": "±0.1", ...},
      "findings": [["high", "title_block.scale"], ...]                       # (severity, location) that must be raised
    }
  },
  "checklists": {"<checklist>.doc": {"drawing": "<file>.pdf", "rows": 20, "all_match": true}}
}

Skipped entirely when DVA_SAMPLES_DIR isn't set, so the public test run
never needs the confidential drawings.
"""
import json
import os
from pathlib import Path

import pytest

SAMPLES = os.environ.get("DVA_SAMPLES_DIR")
pytestmark = pytest.mark.skipif(not SAMPLES or not (Path(SAMPLES or ".") / "expected.json").exists(),
                                reason="DVA_SAMPLES_DIR with expected.json not set (real drawings are kept local)")


def _expected():
    return json.loads((Path(SAMPLES) / "expected.json").read_text(encoding="utf-8"))


def _drawing_cases():
    if not SAMPLES or not (Path(SAMPLES) / "expected.json").exists():
        return []
    return sorted(_expected().get("drawings", {}).items())


def _checklist_cases():
    if not SAMPLES or not (Path(SAMPLES) / "expected.json").exists():
        return []
    return sorted(_expected().get("checklists", {}).items())


@pytest.mark.parametrize("name,exp", _drawing_cases(), ids=[c[0] for c in _drawing_cases()])
def test_real_drawing(name, exp):
    import rules
    from sheetreader.normalize import alnum, ocr_key
    from sheetreader.record import read_drawing

    rec = read_drawing(Path(SAMPLES) / name)[0].to_dict()
    ocr = rec["text_source"] == "ocr"
    if "text_source" in exp:
        assert rec["text_source"] == exp["text_source"]
    for k, v in exp.get("title_block", {}).items():
        got = rec["title_block"].get(k)
        if ocr:
            assert got and ocr_key(got) == ocr_key(v), f"{k}: read {got!r}, expected {v!r}"
        else:
            assert alnum(got) == alnum(v), f"{k}: read {got!r}, expected {v!r}"
    if "bom_items" in exp:
        assert [r.get("item") for r in rec["bom"]] == exp["bom_items"]
    for k, v in exp.get("general_tolerance", {}).items():
        assert rec["general_tolerance"].get(k) == v
    found = {(f["severity"], f["location"]) for f in rules.run_rules(rec)}
    for sev, loc in exp.get("findings", []):
        assert (sev, loc) in found, f"expected a {sev} finding at {loc}; got {sorted(found)}"


@pytest.mark.parametrize("name,exp", _checklist_cases(), ids=[c[0] for c in _checklist_cases()])
def test_real_checklist(name, exp):
    from dimcheck.pipeline import process
    reports = process(str(Path(SAMPLES) / exp["drawing"]), str(Path(SAMPLES) / name), None)
    assert len(reports) == exp["rows"]
    if exp.get("all_match"):
        bad = [(r.zone, r.dimension, r.existing_tolerance, r.resolved_tolerance, r.verdict) for r in reports
               if r.verdict != "match"]
        assert not bad, bad
