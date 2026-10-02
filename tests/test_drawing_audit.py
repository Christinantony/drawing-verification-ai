"""Offline tests for the drawing-audit pipeline on a synthetic vector PDF.
The model call is replaced by a stub client, so no Ollama server is needed."""
import extraction
import structuring
import audit
from ollama_client import extract_json
from make_synthetic_drawing import make_drawing


def _structured(tmp_path):
    pdf = tmp_path / "synthetic.pdf"
    make_drawing(str(pdf))
    return structuring.build_structured_document(extraction.load_document(pdf))


def test_vector_pdf_uses_embedded_text_and_title_block_cells(tmp_path):
    pdf = tmp_path / "synthetic.pdf"
    make_drawing(str(pdf))
    page = extraction.load_document(pdf).pages[0]
    assert page.source == "embedded"          # no OCR for a vector page
    assert page.orientation == "landscape"
    assert len(page.title_block_cells) == 10  # 5 label boxes + 5 value boxes


def test_title_block_fields_read_from_cells(tmp_path):
    s = _structured(tmp_path)
    tb = s["title_block"]
    assert s["title_block_method"] == "cells+regex"
    assert tb["drawing_number"] == "AM-10234"
    assert tb["title"] == "MOUNTING BRAKCET"
    assert tb["revision"] == "B"
    assert tb["material"] == "AL 6061-T6"
    assert tb["scale"] == "1:2"


def test_bom_parsed_from_ruled_table(tmp_path):
    bom = _structured(tmp_path)["bom"]
    assert [r["part_number"] for r in bom] == ["AM-10234", "HW-0042"]
    assert bom[1]["qty"] == "0"


def test_markdown_handed_to_model_contains_structured_data(tmp_path):
    md = structuring.to_markdown(_structured(tmp_path))
    assert "**drawing_number**: AM-10234" in md
    assert "| AM-10234 |" in md


class _StubClient:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"thinking": "checked title vs BOM", "content": self.content}}


def test_audit_parses_model_json_and_keeps_reasoning_trace():
    reply = """Here is my audit.
```json
{"overall_status": "FAIL", "findings": [{"id": "F1", "severity": "high",
 "category": "spelling", "location": "title_block.title",
 "description": "BRAKCET", "suggested_fix": "BRACKET"}], "summary": "1 issue"}
```"""
    client = _StubClient(reply)
    result = audit.audit_drawing("structured md", client, use_thinking=True)
    assert result["overall_status"] == "FAIL"
    assert result["findings"][0]["suggested_fix"] == "BRACKET"
    assert result["_thinking_trace"] == "checked title vs BOM"
    assert client.calls[0]["think"] is True


def test_audit_falls_back_when_reply_is_not_json():
    result = audit.audit_drawing("md", _StubClient("sorry, no json"), use_thinking=False)
    assert result["overall_status"] == "NEEDS_REVIEW"
    assert result["findings"][0]["id"] == "F0"


def test_extract_json_handles_bare_object():
    assert extract_json('prefix {"a": 1} suffix') == {"a": 1}
