"""Reproduce live E4B citation and evidence-class errors with synthetic sources."""
import copy

import pytest
from jsonschema import ValidationError, validate

from supervisor.protocol import validate_assessment
from supervisor.schema import review_schema, source_quotes


def sources():
    return {"drawing": {"id": "drawing", "kind": "drawing", "text": '{\n  "scale": "10:1",\n  "material": "SEE TABLE"\n}'},
            "other": {"id": "other", "kind": "document", "text": "P-100 Bracket Revision 01"},
            "req": {"id": "req", "kind": "requirement", "text": "The finish must be specified."}}


def assessment():
    return {"kind": "assessment", "reviewed_ids": ["drawing"], "findings": [
        {"claim": "Material table is required for review.", "severity": "low",
         "evidence_ids": ["drawing"], "quotes": [{"evidence_id": "drawing", "text": "SEE TABLE"}],
         "action": "Supply the table.", "basis": "text"}], "entities": [], "unresolved": []}


def test_source_constraints_keep_valid_text_findings():
    data = sources(); answer = assessment()
    schema = review_schema(["drawing"], evidence=data)
    validate(answer, schema)
    assert validate_assessment(answer, ["drawing"], data, {"drawing"}) == answer


@pytest.mark.parametrize("change", ["wrong_basis", "retyped_quote", "drawing_entity", "unseen_source"])
def test_live_failure_classes_are_excluded_from_output_schema(change):
    answer = assessment()
    if change == "wrong_basis":
        answer["findings"][0]["basis"] = "requirement"
    elif change == "retyped_quote":
        answer["findings"][0]["quotes"][0]["text"] = '"scale": "10:1", "material": "SEE TABLE"'
    elif change == "drawing_entity":
        answer["entities"] = [{"evidence_id": "drawing", "part_number": "P-100",
                               "part_name": None, "revision": None, "date": None}]
    else:
        answer["findings"][0]["evidence_ids"] = ["other"]
    with pytest.raises(ValidationError):
        validate(answer, review_schema(["drawing"], evidence=sources()))


def test_retrieved_requirement_is_allowed_and_still_needs_its_source():
    answer = assessment(); answer["findings"][0].update(
        basis="requirement", evidence_ids=["drawing", "req"],
        quotes=[{"evidence_id": "req", "text": "The finish must be specified."}])
    data = sources(); allowed = {"drawing", "req"}
    validate(answer, review_schema(["drawing"], evidence=data, allowed=allowed))
    validate_assessment(answer, ["drawing"], data, allowed)
    bad = copy.deepcopy(answer); bad["findings"][0]["evidence_ids"] = ["drawing"]
    bad["findings"][0]["quotes"] = [{"evidence_id": "drawing", "text": "SEE TABLE"}]
    with pytest.raises(ValueError, match="claimed evidence type"):
        validate_assessment(bad, ["drawing"], data, allowed)


def test_quote_options_always_occur_in_original_source():
    for text in ('{\n  "note": "TAP FOR M6"\n}', "SCALE 10:1\n108 +0.1 -0.2", "λ" * 700):
        assert source_quotes(text)
        assert all(q in text and len(q) <= 240 for q in source_quotes(text))
