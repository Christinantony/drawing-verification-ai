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


# ---------------------------------------------------------------------------
# Rules added for real drawing sets
# ---------------------------------------------------------------------------
from check_coherence import id_keys, match_checklist  # noqa: E402


def _dwg(no, title, next_assy=None, bom=(), ocr=False, file=None, form=None, gt=None):
    return {"drawing_no": no, "title": title, "revision": "01", "next_assy": next_assy,
            "bom": [dict(b) for b in bom], "_ocr": ocr, "_file": file or f"{no}.pdf",
            "filename_drawing_no": None, "form_id": form, "general_tolerance": gt or {}}


def test_combined_a_and_b_drawing_answers_to_both_part_numbers():
    assert {"ATKDM12E03A", "ATKDM12E03B"} <= id_keys("AT-KD M12-E-03 A&B")
    parent = _dwg("AT-KD M12-E-01", "FINAL ASSEMBLY",
                  bom=[{"item_no": "2", "description": "CABLE ASSEMBLY", "ref": "AT-KD M12-E-03B"}])
    child = _dwg("AT-KD M12-E-03 A&B", "CABLE ASSEMBLY", next_assy="AT-KD M12-E-01")
    assert check_drawings([parent, child]) == []


def test_ocr_lookalikes_resolve_against_exact_numbers():
    parent = _dwg("AT-KD M12-E-01", "FINAL ASSEMBLY", ocr=True,
                  bom=[{"item_no": "1", "description": "TOP PLATE", "ref": "AI-KD M12-E-02"}])
    parent["filename_drawing_no"] = "AT-KD M12-E-01"
    child = _dwg("AI-KD M12-E-02", "TOP PLATE", next_assy="AI-KD M12-E-01", ocr=True)
    child["filename_drawing_no"] = "AT-KD M12-E-02"
    assert check_drawings([parent, child]) == []
    assert child["drawing_no"] == "AT-KD M12-E-02"  # shown corrected in the report


def test_bom_description_and_child_next_assy_disagreements():
    parent = _dwg("ASM-1", "ASSEMBLY", bom=[{"item_no": "1", "description": "BRACKET", "ref": "PRT-1"}])
    child = _dwg("PRT-1", "SUPPORT PLATE", next_assy="ASM-9")
    types = {i["type"] for i in check_drawings([parent, child])}
    assert "BOM description vs drawing title mismatch" in types
    assert "Child's NEXT ASSY. is a different drawing" in types


def test_hardware_refs_are_not_expected_to_have_drawings():
    parent = _dwg("AT-RL 05 B2-E-03", "BASE PLATE ASSEMBLY",
                  bom=[{"item_no": "10", "description": "SS FASTENERS", "ref": "M2.5x4 CSK SCREWS"},
                       {"item_no": "8", "description": "ADHESIVE", "ref": "LOCTITE 263"},
                       {"item_no": "1", "description": "BASE PLATE", "ref": "AT-RL 05 B2-E-06"}])
    issues = check_drawings([parent])
    assert [i["type"] for i in issues] == ["BOM references missing drawing"]
    assert issues[0]["severity"] == "low" and "B2-E-06" in issues[0]["detail"]


def test_same_form_with_different_general_tolerances():
    a = _dwg("A-1", "X", form="AT-F-DRG-01 REV.00",
             gt={"without_decimal": "±1", "one_decimal": "±0.5", "two_decimal": "±0.1"})
    b = _dwg("B-1", "Y", form="AT-F-DRG-01 REV.00",
             gt={"without_decimal": "±1", "one_decimal": "±0.1", "two_decimal": "±0.05"})
    c = _dwg("C-1", "Z", form="AT-F-DRG-02 REV.00",
             gt={"without_decimal": "±1", "one_decimal": "±0.1", "two_decimal": "±0.05"})
    issues = [i for i in check_drawings([a, b, c]) if i["type"].startswith("Same template form")]
    assert len(issues) == 1 and issues[0]["drawing_no"] == "AT-F-DRG-01"


def test_checklist_is_matched_to_its_drawing_by_file_name():
    files = ["AT-PX_2B_40-03_REV._00.pdf", "AT-RL_05_B2-E-03_REV._00.pdf"]
    assert match_checklist({"_file": "AT-F-DCL-PX_2B_40-03_REV._00.doc"}, files) == files[0]
    assert match_checklist({"_file": "unrelated.docx"}, files) is None
