"""Tests for the deterministic cross-check rules in check_coherence.py.
Model extraction is not exercised; drawings are given as extracted dicts."""
from check_coherence import check_drawings, find_generic_mismatches, group_by_part_number


def _types(issues):
    return sorted(i["type"] for i in issues)


def test_consistent_assembly_has_no_issues():
    parent = {"drawing_no": "ASM-1", "revision": "01", "title": "ASSEMBLY",
              "bom": [{"description": "BRACKET", "ref_part_no": "PRT-1"}]}
    child = {"drawing_no": "PRT-1", "revision": "02", "title": "BRACKET",
             "next_assy": "ASM-1", "bom": []}
    assert check_drawings([parent, child]) == []


def test_cross_drawing_inconsistencies_are_flagged():
    parent = {"drawing_no": "ASM-1", "revision": "01", "title": "ASSEMBLY",
              "bom": [{"description": "BRACKET", "ref_part_no": "PRT-9"}]}
    child = {"drawing_no": "PRT-1", "revision": "02", "title": "BRACKET",
             "next_assy": "ASM-1", "bom": []}
    child_sheet2 = dict(child, revision="03")
    assert _types(check_drawings([parent, child, child_sheet2])) == [
        "BOM references missing drawing",
        "Drawing revision mismatch",
        "NEXT ASSY. not reciprocated in parent BOM",
        "NEXT ASSY. not reciprocated in parent BOM",
    ]


def test_generic_records_revision_mismatch():
    records = [{"part_number": "AM-1", "revision": "A", "_file": "a.docx"},
               {"part_number": "am-1", "revision": "B", "_file": "b.xlsx"}]
    assert _types(find_generic_mismatches(group_by_part_number(records))) == ["Revision mismatch"]
