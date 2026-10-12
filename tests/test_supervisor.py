"""Supervisor acceptance and failure-path tests; no actual model is claimed here."""
import copy
import json
from pathlib import Path

import pytest

from supervisor import legacy
from supervisor.__main__ import main
from supervisor.controller import Controller
from supervisor.model import Ollama
from supervisor.protocol import parse, validate_assessment
from supervisor.report import write
from supervisor.store import Store, file_hash
from supervisor.tools import Tools
from supervisor.workspace import Workspace, chunks


def final(ids, **updates):
    return {"kind": "assessment", "reviewed_ids": ids, "findings": [], "unresolved": [], "entities": [], **updates}


class FakeModel:
    context = 32000
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []
    def preflight(self):
        return {"name": "test-only", "digest": "test-digest", "capabilities": ["vision"], "context": self.context}
    def chat(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        required = json.loads(messages[1]["content"])["required_ids"]
        obj = self.answer(messages, required) if self.answer else final(required)
        return (obj if isinstance(obj, str) else json.dumps(obj)), {"eval_count": 1}


@pytest.fixture
def workspace(cad_pdf, tmp_path):
    store = Store(tmp_path / "state" / "evidence.sqlite3")
    ws = Workspace(cad_pdf, store, tmp_path / "state")
    ws.prepare()
    yield ws
    store.close()


def test_frozen_reader_results_are_retained(workspace, cad_pdf):
    original = legacy.read(cad_pdf)[0]
    assert next(iter(workspace.records.values())) == original
    assert workspace.baseline
    assert all(e["file_hash"] == file_hash(cad_pdf) for e in workspace.evidence.values())


def test_success_covers_every_section_and_keeps_baseline(workspace):
    # Synthetic fixture deliberately references absent children. This test isolates
    # successful model coverage; the missing-child behavior is tested separately.
    workspace.incomplete.clear()
    model = FakeModel()
    result = Controller(workspace, model).review()
    assert result["coverage"] == "complete"
    assert result["issue_status"] == "issues_found"
    assert result["required_ids"] == result["reviewed_ids"]
    assert result["findings"] == workspace.baseline
    assert len(model.calls) == len(workspace.groups)
    path = write(workspace, result)
    assert "Coverage: COMPLETE" in path.read_text()
    assert (path.parent / "evidence.json").exists()


@pytest.mark.parametrize("answer", ["not JSON", "[]", '{"kind":"assessment"}',
    json.dumps(final([])), json.dumps(final(["nonexistent"]))])
def test_bad_response_never_passes(workspace, answer):
    result = Controller(workspace, FakeModel(lambda *_: answer)).review()
    assert result["coverage"] == "incomplete"
    assert result["unresolved"]
    assert result["findings"] == workspace.baseline


def test_model_offline_preserves_baseline(workspace):
    class Offline(FakeModel):
        def preflight(self):
            raise ConnectionError("Ollama unavailable")
    result = Controller(workspace, Offline()).review()
    assert result["coverage"] == "incomplete"
    assert not result["reviewed_ids"]
    assert result["findings"]


def test_no_model_cli_writes_separate_incomplete_report(cad_pdf, tmp_path):
    before = file_hash(cad_pdf)
    output = tmp_path / "output"
    assert main(["review", str(cad_pdf), "--output", str(output), "--no-model"]) == 2
    latest = json.loads((output / "latest.json").read_text())
    data = json.loads(Path(latest["report"]).with_suffix(".json").read_text())
    assert data["coverage"] == "incomplete"
    assert before == file_hash(cad_pdf)


def test_unknown_or_unseen_evidence_is_rejected(workspace):
    eid = workspace.groups[0]["id"]
    finding = {"claim": "test", "severity": "low", "evidence_ids": ["fake"],
               "quotes": [{"evidence_id": "fake", "text": "fake"}], "action": "Review", "basis": "text"}
    with pytest.raises(ValueError, match="Unknown or unseen"):
        validate_assessment(final([eid], findings=[finding]), [eid], workspace.evidence, {eid})


def test_invented_quote_is_rejected(workspace):
    eid = workspace.groups[0]["id"]
    finding = {"claim": "test", "severity": "low", "evidence_ids": [eid],
               "quotes": [{"evidence_id": eid, "text": "THIS PHRASE WAS NEVER EXTRACTED"}],
               "action": "Review", "basis": "text"}
    with pytest.raises(ValueError, match="Quote does not occur"):
        validate_assessment(final([eid], findings=[finding]), [eid], workspace.evidence, {eid})


def test_calculation_tool_result_reaches_model(workspace):
    workspace.incomplete.clear()
    def respond(messages, ids):
        if len(messages) == 2:
            return {"kind": "tools", "requests": [{"name": "calculate", "arguments":
                {"operation": "scaled_length", "values": [508, 10, 1], "unit": "mm"}}]}
        tool = json.loads(messages[-1]["content"])["tool_results"][0]["result"]
        assert "5080" in tool["evidence"][0]["text"]
        return final(ids)
    model = FakeModel(respond)
    result = Controller(workspace, model).review()
    assert result["coverage"] == "complete"
    assert any(args["thinking"] for _, args in model.calls)
    assert workspace.store.db.execute("SELECT count(*) FROM events WHERE kind='tool'").fetchone()[0] > 0


def test_failed_tool_cannot_be_hidden_by_clean_answer(workspace):
    def respond(messages, ids):
        if len(messages) == 2:
            return {"kind": "tools", "requests": [{"name": "get_evidence", "arguments": {"evidence_id": "fake"}}]}
        return final(ids)
    result = Controller(workspace, FakeModel(respond)).review()
    assert result["coverage"] == "incomplete"
    assert any("did not complete" in u["question"] for u in result["unresolved"])


def test_round_limit_cannot_be_pass(workspace):
    model = FakeModel(lambda messages, ids: {"kind": "tools", "requests":
        [{"name": "get_evidence", "arguments": {"evidence_id": ids[0]}}]})
    result = Controller(workspace, model, rounds=0).review()
    assert result["coverage"] == "incomplete"
    assert len(model.calls) == len(workspace.groups)


def test_context_limit_prevents_silent_truncation(workspace):
    model = FakeModel()
    model.context = 100
    result = Controller(workspace, model).review()
    assert not model.calls
    assert result["coverage"] == "incomplete"


def test_repeat_tool_request_rejected(workspace):
    tools = Tools(workspace)
    request = {"name": "get_evidence", "arguments": {"evidence_id": workspace.groups[0]["id"]}}
    tools.execute(request)
    with pytest.raises(ValueError, match="Repeated"):
        tools.execute(request)


def test_reread_retains_first_observation(workspace):
    before = copy.deepcopy(workspace.records)
    eid = workspace.groups[0]["id"]
    result = Tools(workspace).repeat_read(eid, False)
    assert result["original_preserved"]
    assert workspace.records == before
    assert result["evidence"][0]["kind"] == "repeat_read"


def test_crop_uses_original_pdf_and_retains_hash(workspace):
    eid = workspace.groups[0]["id"]
    tools = Tools(workspace, vision=True)
    result = tools.get_source_crop(eid, [0, 0, 100, 100])
    image_id = result["image_ids"][0]
    assert tools.images[image_id]
    assert workspace.evidence[image_id]["box"] == [0, 0, 100, 100]
    assert list((workspace.output / "runs" / workspace.run / "crops").glob("*.png"))
    with pytest.raises(ValueError):
        tools.get_source_crop(eid, [-1, 0, 100, 100])


def test_vision_disabled_is_explicit(workspace):
    with pytest.raises(ValueError, match="Vision is disabled"):
        Tools(workspace).get_source_crop(workspace.groups[0]["id"], [0, 0, 100, 100])


def test_cache_reused_only_with_same_inputs(workspace):
    Controller(workspace, FakeModel()).review()
    model = FakeModel()
    Controller(workspace, model, reuse=True).review()
    assert not model.calls
    workspace.manifest["requirements_hash"] = "changed"
    Controller(workspace, model, reuse=True).review()
    assert model.calls


def test_incomplete_assessment_is_not_cached(workspace):
    model = FakeModel(lambda messages, ids: final(ids, unresolved=[{"question": "Need source", "evidence_ids": ids}]))
    Controller(workspace, model, reuse=True).review()
    first = len(model.calls)
    Controller(workspace, model, reuse=True).review()
    assert len(model.calls) == 2 * first


def test_source_change_cannot_be_reported_complete(workspace, monkeypatch):
    import supervisor.store as storage
    monkeypatch.setattr(storage, "file_hash", lambda path: "changed")
    result = Controller(workspace, FakeModel()).review()
    assert result["coverage"] == "incomplete"


def test_text_chunks_are_complete():
    raw = "x" * 15001
    assert "".join(chunks(raw)) == raw
    assert max(map(len, chunks(raw))) == 4500


def test_invalid_calculations_rejected(workspace):
    tools = Tools(workspace)
    with pytest.raises(ValueError):
        tools.calculate("scaled_length", [10, 1, 0], "mm")
    with pytest.raises(ValueError):
        tools.calculate("unit_conversion", [float("nan")], "mm")
    with pytest.raises(ValueError):
        tools.calculate("tolerance_limits", [10, -0.2, 0.1], "mm")
    result = tools.calculate("tolerance_limits", [10, 0.1, -0.2], "mm")
    assert '9.8' in result["evidence"][0]["text"]


@pytest.mark.parametrize("host", ["https://example.com", "http://192.168.1.5:11434", "http://user:password@localhost:11434", "http://localhost/other"])
def test_model_endpoint_is_local(host):
    with pytest.raises(ValueError):
        Ollama(host)


def test_exact_tag_required(monkeypatch):
    model = Ollama()
    monkeypatch.setattr(model, "request", lambda *a, **kw: {"models": [{"name": "gemma4:31b", "digest": "wrong"}]})
    with pytest.raises(ValueError, match="Exact local model"):
        model.preflight()
    model.close()


def test_thinking_off_explicit_and_truncation_fails(monkeypatch):
    model = Ollama()
    def request(path, payload, timeout):
        assert payload["think"] is False
        assert payload["keep_alive"] == "10m"
        return {"message": {"content": "{}"}, "done_reason": "length"}
    monkeypatch.setattr(model, "request", request)
    with pytest.raises(ValueError, match="truncated"):
        model.chat([])
    model.close()


def test_malformed_tool_schema_rejected():
    with pytest.raises(ValueError):
        parse('{"kind":"tools","requests":[{"name":"shell","arguments":{}}]}')


def test_single_json_wrapper_is_accepted_but_prose_is_not():
    answer = json.dumps(final(["source"]))
    assert parse("```json\n" + answer + "\n```") == final(["source"])
    for bad in ("Here is the JSON: " + answer,
                "```json\n" + answer + "\n```\nextra",
                "```json\n{}\n```\n```json\n{}\n```", ""):
        with pytest.raises(ValueError):
            parse(bad)


def test_schema_is_sent_and_vision_uses_its_own_shape(monkeypatch):
    from supervisor.schema import COLOR_SCHEMA, PROTOCOL_SCHEMA
    model = Ollama()
    seen = []
    def request(path, payload, timeout):
        seen.append(payload)
        return {"message": {"content": '{"color":"red"}'}, "done_reason": "stop"}
    monkeypatch.setattr(model, "request", request)
    try:
        model.chat([])
        model.chat([], response_schema=COLOR_SCHEMA)
        assert seen[0]["model"] == seen[1]["model"] == "gemma4:e4b"
        assert seen[0]["format"] == PROTOCOL_SCHEMA
        assert seen[1]["format"] == COLOR_SCHEMA
    finally:
        model.close()


def test_compact_package_keeps_all_text_and_allows_realistic_context(workspace):
    # A large complete text section must reach the model. Repeated provenance
    # metadata is retained in the ledger, rather than consuming prompt space.
    eid = workspace.groups[0]["id"]
    workspace.evidence[eid]["text"] = "dimension 12 mm\n" * 280
    model = FakeModel()
    model.context = 16384
    result = Controller(workspace, model, max_tokens=2000).review()
    assert model.calls
    package = json.loads(model.calls[0][0][1]["content"])
    assert package["evidence"][0]["text"] == workspace.evidence[eid]["text"]
    assert "file_hash" not in package["evidence"][0]
    assert workspace.evidence[eid]["file_hash"]
    assert result["supervisor_status"] == "completed"
    assert result["coverage"] == "incomplete"  # Missing child evidence remains.


def test_bad_file_still_appears_as_incomplete(tmp_path):
    pdf = tmp_path / "bad.pdf"; pdf.write_bytes(b"not a pdf")
    store = Store(tmp_path / "out" / "state.db")
    try:
        ws = Workspace(pdf, store, tmp_path / "out")
        ws.prepare()
        result = Controller(ws, FakeModel()).review()
        assert result["coverage"] == "incomplete"
        assert any("Could not read" in u["question"] for u in result["unresolved"])
    finally:
        store.close()


def test_missing_child_keeps_coverage_incomplete(workspace):
    assert workspace.incomplete
    result = Controller(workspace, FakeModel()).review()
    assert result["required_ids"] == result["reviewed_ids"]
    assert result["coverage"] == "incomplete"
    assert any("not in this folder" in u["question"] for u in result["unresolved"])


def test_packing_retains_all_source_sections(workspace):
    expected = {e for e, item in workspace.evidence.items() if item["kind"] == "drawing"}
    assert {e for g in workspace.groups for e in g["required_ids"]} == expected
    assert len(workspace.groups) < len(expected)


def test_source_word_coordinates_retained(workspace):
    eid = workspace.groups[0]["id"]
    result = Tools(workspace).get_source_layout(eid, "SCALE")
    assert result["matches"]
    assert result["source_bounds"]["coordinates"] == "original displayed PDF points"
    assert all(len(w["box"]) == 4 for w in result["matches"])
    assert all(w["ocr_confidence"] is None for w in result["matches"])


def test_unreadable_document_retained_in_manifest(tmp_path):
    source = tmp_path / "bad.docx"; source.write_bytes(b"broken zip")
    store = Store(tmp_path / "out" / "evidence.db")
    try:
        ws = Workspace(source, store, tmp_path / "out")
        ws.prepare()
        assert "bad.docx" in ws.manifest["files"]
        assert ws.incomplete
    finally:
        store.close()


def test_generic_entity_joins_drawing_and_gets_supervisor_review(workspace):
    eid = workspace.add("document", "XY-100-01 BRACKET Rev 09", file="test.docx")
    workspace.groups.append({"id": eid, "required_ids": [eid], "record_key": None})
    def respond(messages, ids):
        package = json.loads(messages[1]["content"])
        if any(e["kind"] == "document" for e in package["evidence"]):
            return final(ids, entities=[{"evidence_id": eid, "part_number": "XY-100-01",
                "part_name": "BRACKET", "revision": "09", "date": None}])
        return final(ids)
    model = FakeModel(respond)
    result = Controller(workspace, model).review()
    assert any(f["source"] == "model-derived-rule" for f in result["findings"])
    assert any(e["kind"] == "relationship" for e in workspace.evidence.values())
    assert result["required_ids"] == result["reviewed_ids"]
    assert len(model.calls) >= 3


def test_reference_lookup_returns_revision_and_pagination(workspace):
    for n in range(10):
        workspace.add("requirement", f"Finish rule {n}", source="Company finish instruction", revision="02",
                      requirement_id=f"FIN-{n}")
    tools = Tools(workspace)
    result = tools.lookup_requirement("Finish")
    assert result["total"] == 10 and result["next_offset"] == 8
    second = tools.lookup_requirement("Finish", offset=8)
    assert second["next_offset"] is None
    evidence = tools.get_evidence(result["matches"][0]["id"])["evidence"][0]
    assert evidence["revision"] == "02"


def test_ocr_repeat_timeout_is_explicit(cad_pdf):
    from supervisor.tools import bounded_read
    with pytest.raises(TimeoutError, match="time budget"):
        bounded_read(cad_pdf, True, 0.0001)


def test_transport_uses_local_http_protocol():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from supervisor.__main__ import doctor
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            value = {"models": [{"name": "gemma4:e4b", "digest": "local-test"}]} if self.path == "/api/tags" else {"version": "test"}
            self.reply(value)
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(payload)
            if self.path == "/api/show":
                self.reply({"capabilities": ["completion", "vision"]})
            else:
                prompt = payload["messages"][0]["content"]
                assert isinstance(payload["format"], dict)
                answer = prompt[prompt.index('{'):]
                self.reply({"message": {"content": answer}, "done_reason": "stop"})
        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    client = Ollama(f"http://127.0.0.1:{server.server_port}")
    try:
        result = doctor(client, smoke=True)
        assert len(result["checks"]) == 3
        assert any(p.get("think") is False for p in seen)
        assert any(p.get("think") is True for p in seen)
    finally:
        client.close(); server.shutdown(); thread.join(); server.server_close()
