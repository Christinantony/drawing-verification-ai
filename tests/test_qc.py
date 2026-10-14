"""QC agent: planted mistakes on a synthetic set, the model judgement step and
the doubt loop with a fake model, the report files."""
import csv
import json
from pathlib import Path

import pytest

from make_qc_set import make_set
from qc import config
from qc.__main__ import main
from qc.checks import Finding, run_all
from qc.doubt import run as doubt_run
from qc.judge import judge
from qc.setmap import SetMap, item_refs, learn_pattern
from qc.sheets import read_pdf


@pytest.fixture(scope="module")
def qcset(tmp_path_factory):
    folder = tmp_path_factory.mktemp("qcset")
    make_set(folder)
    sheets = [q for p in sorted(folder.glob("*.pdf")) for q in read_pdf(p)]
    return folder, sheets, SetMap(sheets)


def _codes(findings, status="fail"):
    return sorted({f.code for f in findings if f.status == status})


def test_parts_are_located(qcset):
    _, sheets, _ = qcset
    asm = next(s for s in sheets if s.number == "AB-CD 10-E-01")
    assert [r["item"] for r in asm.record["bom"]] == ["1", "2", "3", "4"]
    assert sorted(asm.balloons) == ["1", "2", "3", "7"]
    assert [n.number for n in asm.notes] == ["1", "2", "3", "5", "6"]
    assert asm.notes[1].text.startswith("USE FILE AB-CD 10-01.STEP")


def test_set_map_learns_numbering_and_resolves(qcset):
    _, sheets, smap = qcset
    assert smap.pattern.company == "AB" and smap.pattern.core == ["CD", "10"] and smap.pattern.category == "E"
    asm = next(s for s in sheets if s.number == "AB-CD 10-E-01")
    assert {c.number for c in smap.children_of(asm)} == {"AB-CD 10-E-02", "AB-CD 10-E-03"}
    styled = {r.number: r.detail for r in smap.refs if r.status == "styled"}
    assert "leading zero" in styled["AB-CD 10-E-3"] and "'E-' is missing" in styled["AB-CD 10-01"]
    assert [r.number for r in smap.refs if r.kind == "tool"] == ["AB-JIG-CD 10-05"]


def test_planted_mistakes_are_found(qcset):
    _, sheets, smap = qcset
    fs = run_all(sheets, smap, config.load())
    msgs = " | ".join(f.message for f in fs if f.status == "fail")
    for code in ("A2", "B1", "B3", "C1", "C3", "C5", "C7", "D1"):
        assert code in _codes(fs), code
    assert "Balloon 7 has no row" in msgs and "BOM item 4 'ANEROBIC ADHESIVE' has no balloon" in msgs
    assert "ITEM 9" in msgs and "SEE TABLE 2" in msgs and "CHROMATISATION" in msgs
    # nothing false on the clean parts
    parts = [f for f in fs if f.status == "fail" and f.sheet is not None and f.sheet.number != "AB-CD 10-E-01"]
    assert not parts, [f.message for f in parts]


def test_item_refs_forms():
    assert item_refs("USE ITEM NO. (13) TO TIE ITEM NO. (3) TO CONNECTOR") == ["13", "3"]
    assert item_refs("APPLY ITEM NO.10 IN THE GAP") == ["10"]
    assert item_refs("APPLY ITEM NO. (4) AFTER SOLDERING ITEM (3) AND ITEM (5)") == ["4", "3", "5"]


def test_learn_pattern_handles_missing_category():
    p = learn_pattern(["XY-KD 20-E-01", "XY-KD 20-E-02"])
    assert (p.company, p.core, p.category) == ("XY", ["KD", "20"], "E")


class FakeModel:
    """Answers by question type, records what it was asked."""
    name = "fake"
    vision = True

    def __init__(self, tool_choice=("search_set", "ask_person")):
        self.asked, self.tools = [], list(tool_choice)
        self.calls = 0

    def ask(self, system, user, schema, images=None, max_tokens=300):
        self.calls += 1
        self.asked.append(user)
        props = schema["properties"]
        if "words" in props:
            n = user.count("\n") + 1
            return {"words": [{"n": k, "verdict": "misspelt" if "KAPTN" in line else "correct",
                               "correction": "KAPTON"} for k, line in enumerate(user.splitlines()[1:], 1)][:n]}
        if "answers" in props:
            return {"answers": [{"n": 1, "verdict": "wrong", "reason": "a screw is not an adhesive"}]}
        if "problems" in props:
            return {"problems": [{"n": 99, "problem": "out of range, must be dropped"}]}
        if "tool" in props:
            allowed = props["tool"]["enum"]
            t = next((x for x in self.tools if x in allowed), allowed[-1])
            if t in self.tools:
                self.tools.remove(t)
            return {"tool": t, "reason": "test"}
        if "text" in props:
            return {"text": "AB-CD 10-E-02"}
        return {}

    def speed(self):
        return {"calls": self.calls}


def test_judgement_uses_short_checked_answers(qcset):
    _, sheets, _ = qcset
    s = sheets[0]
    fs = [Finding("B2", "must", "ask", "x", s, "KAPTN", "note 1", ask={"type": "spelling", "word": "KAPTN", "line": "KAPTN TAPE", "suggestion": None}),
          Finding("B2", "must", "ask", "x", s, "LOCTITE", "note 1", ask={"type": "spelling", "word": "LOCTITE", "line": "LOCTITE 263", "suggestion": None}),
          Finding("C4", "look", "ask", "x", s, "APPLY ITEM NO. 3", "note 3", ask={"type": "note_item", "note": "APPLY ITEM NO. 3 ON FACES", "item": "3", "item_text": "SS FASTENERS"}),
          Finding("D4", "look", "ask", "x", s, "", "notes", ask={"type": "notes", "notes": ["ONE", "TWO"]})]
    m = FakeModel()
    judge(fs, m, lambda *_: None)
    assert fs[0].status == "fail" and "KAPTON" in fs[0].message
    assert fs[1].status == "cleared"
    assert fs[2].status == "fail" and "not an adhesive" in fs[2].message
    assert fs[3].status == "cleared" and len(fs) == 4          # out-of-range answer dropped


def test_no_model_marks_questions_not_run(qcset):
    from qc.model import NoModel
    _, sheets, _ = qcset
    f = Finding("B2", "must", "ask", "x", sheets[0], "W", ask={"type": "spelling", "word": "W", "line": ""})
    judge([f], NoModel(), lambda *_: None)
    assert f.status == "not_run"


def test_doubt_loop_is_bounded_and_recorded(qcset):
    _, sheets, smap = qcset
    s = sheets[0]
    f = Finding("C2", "must", "doubt", "parent BOM does not list this", s, "AB-CD 10-E-02", "NEXT ASSY.", None,
                related=[sheets[1]])
    m = FakeModel(tool_choice=("search_set", "open_linked", "search_set"))
    doubt_run([f], sheets, smap, m, config.load(), lambda *_: None)
    assert f.status == "check"
    tools = [h for h in f.history if h.startswith(("search_set", "open_linked"))]
    assert len(tools) == 2                                      # at most 2 passes
    assert any("model chose" in h for h in f.history)


def test_doubt_loop_clears_when_reading_settles_it(qcset):
    _, sheets, smap = qcset
    s = sheets[0]
    f = Finding("C1", "must", "doubt", "OCR read 'A8-CD 1O-E-O2'", s, "A8-CD 1O-E-O2", "BOM item 1",
                (60, 400, 200, 410), doubt={"kind": "ref", "text": "A8-CD 1O-E-O2"})
    m = FakeModel(tool_choice=("read_image",))
    cfg = config.load()
    doubt_run([f], sheets, smap, m, cfg, lambda *_: None)
    assert f.status == "cleared" and "matches AB-CD 10-E-02" in f.message


def test_review_cli_writes_report_and_review_sheet(qcset, tmp_path):
    folder, _, _ = qcset
    out = tmp_path / "out"
    code = main(["review", str(folder), "--out", str(out), "--no-model"])
    assert code == 1                                            # must-fix items exist
    run_dir = Path((out / "latest.txt").read_text(encoding="utf-8")).parent
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "Must fix before print" in html and "data:image/png;base64" in html
    rows = list(csv.reader(open(run_dir / "findings.csv", encoding="utf-8-sig")))
    assert rows[0][10].startswith("your_verdict") and len(rows) > 5
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert {f["check"] for f in data["findings"]} >= {"C1", "C3", "A2"}


def test_review_cli_reports_missing_model(qcset, tmp_path):
    folder, _, _ = qcset
    assert main(["review", str(folder), "--out", str(tmp_path / "o"), "--host", "http://127.0.0.1:9"]) == 3
