"""drawing-audit: deterministic rules, the language layers, and the CLI end to
end. The model is replaced by a stub client, so no Ollama server is needed."""
import datetime as dt
import json

import audit
import language
import main as audit_main
import rules
from ollama_client import extract_json, extract_json_list
from sheetreader.record import read_drawing


def _rec(pdf):
    return read_drawing(pdf)[0].to_dict()


def _by(findings, location_prefix):
    return [f for f in findings if f["location"].startswith(location_prefix)]


def test_rules_flag_the_planted_errors(cad_pdf):
    fs = rules.run_rules(_rec(cad_pdf), today=dt.date(2026, 10, 1))
    # SCALE 10:1 with a 180 mm dimension can't fit an A4 sheet
    scale = _by(fs, "title_block.scale")
    assert scale and scale[0]["severity"] == "high" and "1800" in scale[0]["description"]
    # zero quantity
    assert any(f["severity"] == "high" and "quantity 0" in f["description"] for f in _by(fs, "bom.item_3"))
    # consistent things are not flagged
    assert not _by(fs, "title_block.drawing_number")   # matches file name
    assert not _by(fs, "revision_table")               # REV 01 in both
    assert not _by(fs, "title_block.size")             # A4 sheet, SIZE A4
    assert rules.overall_status(fs) == "FAIL"


def test_rules_catch_identity_and_revision_contradictions(cad_pdf):
    rec = _rec(cad_pdf)
    rec["filename_drawing_number"] = "XY-100-07"
    rec["title_block"]["revision"] = "02"
    rec["title_block"]["size"] = "A3"
    rec["title_block"]["next_assy"] = "XY-100-01"
    rec["bom"].append({"item": "3", "description": "WASHER", "qty": "2", "ref": "SS"})
    fs = rules.run_rules(rec, today=dt.date(2026, 10, 1))
    descs = " | ".join(f["description"] for f in fs if f["severity"] == "high")
    assert "does not match the file name" in descs
    assert "REV 01 but the title block says REV 02" in descs
    assert "SIZE A3 but the sheet is A4" in descs
    assert "NEXT ASSY. (XY-100-01) is the drawing's own number" in descs
    assert "Duplicate BOM item number(s): 3" in descs


def test_ocr_lookalike_drawing_number_is_info_not_error():
    rec = {"text_source": "ocr", "filename_drawing_number": "AT-KD M12-E-02", "filename_revision": "01",
           "title_block": {"drawing_number": "AI-KD M12-E-02", "revision": "01"}}
    f = rules.Findings()
    rules.check_identity(rec, f)
    assert [x["severity"] for x in f.items] == ["info"]


def test_known_misspellings_need_no_model(cad_pdf):
    fs, trace = language.check_language(_rec(cad_pdf))
    fixes = {f["suggested_fix"] for f in fs if f["source"] == "rule"}
    assert "BRACKET" in fixes            # title
    assert "SILICONE SEALANT" in fixes   # BOM and note
    assert trace["model_response"] is None


class _StubClient:
    def __init__(self, content, thinking=""):
        self.content, self.thinking, self.calls = content, thinking, []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"thinking": self.thinking, "content": self.content}}


def test_model_language_findings_must_quote_real_text(cad_pdf):
    reply = json.dumps([
        {"text": "BREAK ALL SHARP EDGES", "correction": "BREAK ALL SHARP EDGES.", "reason": "style"},
        {"text": "MOUNTING FLANGE", "correction": "x", "reason": "invented"},   # not on the drawing
        {"text": "ANODIZED", "correction": "ANODISED", "reason": "UK spelling"},
    ])
    client = _StubClient(reply)
    fs, trace = language.check_language(_rec(cad_pdf), client=client, model="stub")
    model = [f for f in fs if f["source"] == "model"]
    assert [f["suggested_fix"] for f in model] == ["BREAK ALL SHARP EDGES.", "ANODISED"]
    call = client.calls[0]
    assert "think" not in call  # language pass runs with thinking off


def test_cli_end_to_end_without_model(cad_pdf, tmp_path):
    out = tmp_path / "report.md"
    results = audit_main.main([str(cad_pdf), "-o", str(out), "--no-model", "--json"])
    assert results[0]["status"] == "FAIL"
    md = out.read_text(encoding="utf-8")
    assert "SCALE 10:1 cannot be right" in md and "| drawing_number | XY-100-01 |" in md
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert data[0]["record"]["bom"][0]["ref"] == "XY-100-02"


def test_deep_audit_parses_model_json_and_keeps_reasoning_trace():
    reply = """Here is my audit.
```json
{"overall_status": "FAIL", "findings": [{"id": "F1", "severity": "high",
 "category": "spelling", "location": "title_block.title",
 "description": "BRAKCET", "suggested_fix": "BRACKET"}], "summary": "1 issue"}
```"""
    client = _StubClient(reply, thinking="checked title vs BOM")
    result = audit.audit_drawing("structured md", client, use_thinking=True)
    assert result["overall_status"] == "FAIL"
    assert result["findings"][0]["suggested_fix"] == "BRACKET"
    assert result["_thinking_trace"] == "checked title vs BOM"
    assert client.calls[0]["think"] is True


def test_deep_audit_falls_back_when_reply_is_not_json():
    result = audit.audit_drawing("md", _StubClient("sorry, no json"), use_thinking=False)
    assert result["overall_status"] == "NEEDS_REVIEW"
    assert result["findings"][0]["id"] == "F0"


def test_json_extraction_helpers():
    assert extract_json('prefix {"a": 1} suffix') == {"a": 1}
    assert extract_json_list('ok ```json\n[{"text": "x"}]\n```') == [{"text": "x"}]
    assert extract_json_list("no list here") is None
