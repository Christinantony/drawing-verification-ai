"""Runtime validation must not hide failures behind expected review findings."""
import copy

import pytest

from scripts.private_gemma_test import validate_execution


def review():
    return {"model": {"name": "gemma4:e4b", "digest": "test-digest"},
            "supervisor_status": "completed", "required_ids": ["a", "b"],
            "reviewed_ids": ["a", "b"], "coverage": "incomplete", "issue_status": "issues_found",
            "unresolved": [{"question": "Child drawing is not in this folder", "evidence_ids": ["a"]}]}


def test_missing_references_are_reported_separately_from_execution():
    validation = validate_execution([{"exit_code": 0}, {"exit_code": 2}], review(), "gemma4:e4b")
    assert validation["execution_passed"]
    assert validation["engineering_coverage"] == "incomplete"
    assert validation["unresolved_count"] == 1


@pytest.mark.parametrize("change", [
    {"model": {"name": "gemma4:31b", "digest": "test-digest"}},
    {"model": {"name": "gemma4:e4b", "digest": None}},
    {"supervisor_status": "incomplete"}, {"reviewed_ids": ["a"]},
    {"required_ids": [], "reviewed_ids": []},
    {"unresolved": [{"question": "Could not read input.pdf: broken", "evidence_ids": []}]},
])
def test_failed_integration_or_extraction_cannot_pass(change):
    data = copy.deepcopy(review()); data.update(change)
    assert not validate_execution([{"exit_code": 0}, {"exit_code": 2}], data, "gemma4:e4b")["execution_passed"]


def test_failed_model_smoke_test_cannot_pass():
    assert not validate_execution([{"exit_code": 3}, {"exit_code": 2}], review(), "gemma4:e4b")["execution_passed"]
