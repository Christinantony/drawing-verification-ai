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
    assert "leading zero" in styled["AB-CD 10-E-3"]
    # model files (USE FILE ... .STEP) are named without 'E-': house style, accepted
    ok = {r.number: r.detail for r in smap.refs if r.status == "ok"}
    assert "AB-CD 10-01" not in styled and "normal for model files" in ok["AB-CD 10-01"]
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
    assert any(h.startswith("your_verdict") for h in rows[0]) and "rule_said" in rows[0] and len(rows) > 5
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    assert {f["check"] for f in data["findings"]} >= {"C1", "C3", "A2"}


def test_review_cli_reports_missing_model(qcset, tmp_path):
    folder, _, _ = qcset
    assert main(["review", str(folder), "--out", str(tmp_path / "o"), "--host", "http://127.0.0.1:9"]) == 3


# ---------------------------------------------------------------------------
# Model-driven steps: decisions under the QC policy, open review, layout
# judge, drafted summary, verdict examples (fake model; no Ollama needed)
# ---------------------------------------------------------------------------
from qc.decide import decide, draft_summary, load_examples, load_policy, open_review  # noqa: E402
from qc.layout import judge_layout  # noqa: E402


class PolicyModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def ask(self, system, user, schema, images=None, max_tokens=300):
        self.calls += 1
        self.prompts.append(user)
        props = schema["properties"]
        if "decisions" in props:
            import re as _re
            n = sum(1 for line in user.splitlines() if _re.match(r"\d+\. \[", line))
            # drop the first fact, re-word the second, leave the rest unanswered
            out = [{"n": 1, "decision": "drop", "wording": "", "reason": "standard part"}]
            if n > 1:
                out.append({"n": 2, "decision": "look", "wording": "Reworded by the model.", "reason": "minor"})
            return {"decisions": out}
        if "observations" in props:
            return {"observations": [
                {"quote": "FINAL ASSEMBLY", "problem": "Title is too generic for this product.", "severity": "look"},
                {"quote": "THIS TEXT IS NOT ON THE SHEET", "problem": "invented", "severity": "must"}]}
        if "overall" in props:
            return {"overall": "Fix the drawing numbers first.",
                    "sheets": [{"drawing": "AB-CD 10-E-01", "summary": "Two references are written wrongly."},
                               {"drawing": "NOT-A-DRAWING", "summary": "dropped"}]}
        if "missing" in props:
            boxes = [{"n": i, "kind": "annotation"} for i in range(1, 40)]
            return {"boxes": boxes, "missing": [{"kind": "notes", "area": "bottom-left", "hint": "notes"}]}
        return super().ask(system, user, schema, images, max_tokens)


def test_policy_is_plain_text_and_loaded():
    p = load_policy(None)
    assert "Severity" in p and "standard or bought-out part" in p


def test_model_decides_under_policy_and_nothing_vanishes(qcset):
    _, sheets, smap = qcset
    fs = run_all(sheets, smap, config.load())
    asm = [f for f in fs if f.status == "fail" and f.sheet is not None and f.sheet.number == "AB-CD 10-E-01"]
    m = PolicyModel()
    decide(fs, sheets, m, load_policy(None), [], config.load(), lambda *_: None)
    assert "QC POLICY" in m.prompts[0] and "standard or bought-out part" in m.prompts[0]
    dropped = [f for f in asm if f.status == "dropped"]
    assert dropped and dropped[0].drop_reason == "standard part"
    reworded = [f for f in asm if f.message == "Reworded by the model."]
    assert reworded and reworded[0].severity == "look" and reworded[0].rule_message
    # facts the model did not answer keep the rule decision, and say so
    untouched = [f for f in asm if f.status == "fail" and "rule default" in " ".join(f.history)]
    assert untouched


def test_open_review_keeps_only_quotes_on_the_sheet(qcset):
    _, sheets, _ = qcset
    asm = [s for s in sheets if s.number == "AB-CD 10-E-01"]
    obs = open_review(asm, [], PolicyModel(), load_policy(None), [], lambda *_: None)
    assert [o.quote for o in obs] == ["FINAL ASSEMBLY"] and obs[0].code == "M1"


def test_drafted_summary_only_names_real_drawings(qcset):
    _, sheets, smap = qcset
    fs = run_all(sheets, smap, config.load())
    sm = draft_summary(sheets, fs, PolicyModel(), lambda *_: None)
    assert sm["overall"].startswith("Fix") and [x["drawing"] for x in sm["sheets"]] == ["AB-CD 10-E-01"]


def test_layout_judge_relabels_and_adds_from_free_words(tmp_path):
    import pymupdf
    from make_qc_set import make_part
    pdf = tmp_path / "AB-CD 10-E-05 REV.00.pdf"
    make_part(str(pdf), "AB-CD 10-E-05", "SPACER")
    doc = pymupdf.open(str(pdf))
    page = doc[0]
    # a second, unheaded notes-like block at bottom-left the code can't place
    page.insert_text((40, 520), "BONDING INSTRUCTIONS FOR THE SPACER", fontsize=7)
    page.insert_text((40, 530), "CLEAN WITH ISOPROPYL ALCOHOL BEFORE BONDING", fontsize=7)
    page.insert_text((300, 200), "SMOOTH SURFACE", fontsize=7)
    doc.saveIncr()
    doc.close()
    q = read_pdf(pdf)[0]
    before = [b.kind for b in q.blocks]
    judge_layout([q], PolicyModel(), lambda *_: None)
    after = [b.kind for b in q.blocks]
    assert "unplaced" in before and after.count("unplaced") < before.count("unplaced")
    assert q.layout["changes"]


def test_examples_from_earlier_verdicts(tmp_path):
    p = tmp_path / "findings.csv"
    p.write_text("id,check_name,text,finding,your_verdict (real / false / dont care),your_comment\n"
                 "Q001,Unknown word,KAPTON,'KAPTON' not in dictionary,false,trade name\n"
                 "Q002,Reference,E-9B,written as E-9B,real,\n"
                 "Q003,Reference,X,unmarked,,\n", encoding="utf-8-sig")
    ex = load_examples([p])
    assert [e["verdict"] for e in ex] == ["false", "real"] and ex[0]["comment"] == "trade name"


def test_full_review_with_fake_model(qcset, tmp_path, monkeypatch):
    import qc.model as qm
    folder, _, _ = qcset

    class Fake(PolicyModel):
        def __init__(self, *a, **k):
            super().__init__()
            self.name = "fake-model"
            self.metrics = []

        def installed(self):
            return ["fake-model"]

    monkeypatch.setattr(qm, "Client", Fake)
    out = tmp_path / "out"
    code = main(["review", str(folder), "--out", str(out), "--model", "fake-model"])
    assert code in (0, 1)
    run_dir = Path((out / "latest.txt").read_text(encoding="utf-8")).parent
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "Review summary" in html and "Dropped by the model" in html and "model + QC policy" in html
    rows = list(csv.DictReader(open(run_dir / "findings.csv", encoding="utf-8-sig")))
    assert any(r["group"] == "dropped" and r["model_said"].startswith("drop") for r in rows)


# ---------------------------------------------------------------------------
# Tuning from the engineer's verdicts on a real set
# ---------------------------------------------------------------------------
class PmModel(FakeModel):
    """Sees a ± on the image for 135/10, none for 3/1."""
    def ask(self, system, user, schema, images=None, max_tokens=300):
        if "pm" in schema["properties"]:
            self.calls += 1
            assert images, "the ± must be checked on an image"
            return {"pm": "yes" if "135" in user else "no"}
        return super().ask(system, user, schema, images, max_tokens)


def _part(tmp_path, **kw):
    from make_qc_set import make_part
    p = tmp_path / "AB-CD 10-E-02 REV.00.pdf"
    make_part(str(p), "AB-CD 10-E-02", "BASE PLATE", **kw)
    return read_pdf(p)


def test_pm_values_are_checked_on_the_image(tmp_path):
    from qc.pm import resolve
    sheets = _part(tmp_path, weight="135 10 g", extra=("3.  APPLY 3 1 g OF ADHESIVE",))
    fs = resolve(sheets, PmModel(), lambda *_: None, vision=True)
    q = sheets[0]
    assert q.record["title_block"]["weight"] == "135±10 g"
    assert [f.code for f in fs] == ["D6"] and "3 1 g" in fs[0].message and fs[0].severity == "must"
    assert {p["verdict"] for p in q.pm} == {"confirmed", "absent"}


def test_pm_values_without_vision_are_read_as_pm(tmp_path):
    from qc.model import NoModel
    from qc.pm import resolve
    sheets = _part(tmp_path, weight="135 10 g")
    assert resolve(sheets, NoModel(), lambda *_: None, vision=False) == []
    assert sheets[0].record["title_block"]["weight"] == "135±10 g" and sheets[0].pm[0]["verdict"] == "assumed"


def test_stray_number_is_not_a_note(tmp_path):
    q = _part(tmp_path, extra=("5",))[0]
    assert all(n.text.strip() != "5" for n in q.notes) and len(q.notes) == 2


def test_never_drop_codes_stay_for_the_engineer(qcset):
    _, sheets, _ = qcset
    f = Finding("A5", "must", "fail", "Title block: WEIGHT is empty.", sheets[0], "", "title block")
    decide([f], sheets, PolicyModel(), load_policy(None), [], config.load(), lambda *_: None)
    assert f.status == "fail" and f.severity == "look"
    assert any("kept for the engineer" in h for h in f.history)


def test_open_review_does_not_compare_parts_of_different_drawings(qcset):
    _, sheets, smap = qcset
    asm = [s for s in sheets if s.number == "AB-CD 10-E-01"]

    class Obs(PolicyModel):
        def ask(self, system, user, schema, images=None, max_tokens=300):
            if "observations" in schema["properties"]:
                assert "refers to drawing AB-CD 10-E-02 titled 'BASE PLATE'" in user
                return {"observations": [
                    {"quote": "BASE PLATE", "problem": "BASE PLATE and CLAMP are named inconsistently.", "severity": "look"},
                    {"quote": "FINAL ASSEMBLY", "problem": "Title is too generic.", "severity": "look"}]}
            return super().ask(system, user, schema, images, max_tokens)
    obs = open_review(asm, [], Obs(), load_policy(None), [], lambda *_: None, smap=smap, cfg=config.load())
    assert [o.quote for o in obs] == ["FINAL ASSEMBLY"]


def test_dates_after_today_and_revision_table_mismatch(tmp_path):
    import datetime
    q = _part(tmp_path)[0]
    from qc.checks import check_E
    cfg = config.load()
    cfg["today"] = datetime.date(2026, 6, 1)
    assert any("after today" in f.message for f in check_E([q], cfg))
    cfg["today"] = datetime.date(2026, 10, 16)
    assert not any("after today" in f.message for f in check_E([q], cfg))
    q.record["revision_table"] = [{"rev": q.record["title_block"].get("revision"), "date": "05/07/2026"}]
    assert any("Revision table gives 05/07/2026" in f.message for f in check_E([q], cfg))


def test_policy_holds_the_engineer_rules():
    p = load_policy(None)
    for s in ("without\n    the \"E-\"", "value ± tolerance", "sub-assemblies", "exact BOM name",
              "different drawings are different parts", "DD/MM/YYYY", "SEE TABLE means"):
        assert s in p, s


# ---------------------------------------------------------------------------
# Lessons library: silence list, relevant examples, house rules, score
# ---------------------------------------------------------------------------
def _run_and_mark(folder, out, marks, vdir, extra_args=()):
    """Run --no-model, mark rows by check code in findings.csv, save to vdir."""
    assert main(["review", str(folder), "--out", str(out), "--no-model", *extra_args]) in (0, 1)
    run_dir = Path((out / "latest.txt").read_text(encoding="utf-8")).parent
    rows = list(csv.DictReader(open(run_dir / "findings.csv", encoding="utf-8-sig")))
    vk = next(k for k in rows[0] if k.startswith("your_verdict"))
    for r in rows:
        if r["check"] in marks:
            r[vk], r["your_comment"] = marks[r["check"]]
    vdir.mkdir(exist_ok=True)
    with open(vdir / "set1_verdicts.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return run_dir, rows


def test_false_verdict_silences_the_same_finding_next_run(qcset, tmp_path):
    folder, _, _ = qcset
    vdir = tmp_path / "verdicts"
    _, rows1 = _run_and_mark(folder, tmp_path / "o1", {"C7": ("false", "jig is fine"), "C1": ("false", "")}, vdir)
    assert "rule_finding" in rows1[0]
    out2 = tmp_path / "o2"
    main(["review", str(folder), "--out", str(out2), "--no-model", "--verdicts", str(vdir)])
    run_dir = Path((out2 / "latest.txt").read_text(encoding="utf-8")).parent
    rows2 = {r["check"] + r["text"]: r for r in csv.DictReader(open(run_dir / "findings.csv", encoding="utf-8-sig"))}
    c7 = [r for r in rows2.values() if r["check"] == "C7"]
    assert c7 and all(r["group"] == "silenced" and "jig is fine" in r["history"] for r in c7)
    # C1 (wrong references) is never silenced: it stays, as please look, and says why
    c1 = [r for r in rows2.values() if r["check"] == "C1"]
    assert c1 and all(r["group"] == "look" and "never silenced" in r["history"] for r in c1)
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "Silenced by your earlier verdicts" in html and "Save verdicts" in html and "var ROWS=" in html
    assert "reported items were real" in html


def test_silence_respects_product_family_and_latest_verdict(qcset, tmp_path):
    from qc.lessons import read_verdicts, silence
    folder, sheets, smap = qcset
    fs = [f for f in run_all(sheets, smap, config.load()) if f.code == "C7"]
    assert fs
    f = fs[0]

    def write(name, drawing, verdict):
        p = tmp_path / name
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "group", "check", "drawing", "text", "finding", "your_verdict", "your_comment"])
            w.writerow(["Q001", "look", "C7", drawing, f.quote, f.message, verdict, ""])
        return p

    other = write("a.csv", "ZZ-QQ 99-E-01", "false")              # another product family
    assert silence([f], read_verdicts([other]), smap, config.load()) == 0 and f.status == "fail"
    import os
    import time as _t
    first = write("b.csv", "AB-CD 10-E-02", "false")
    second = write("c.csv", "AB-CD 10-E-02", "real")              # later: you changed your mind
    os.utime(first, (_t.time() - 100, _t.time() - 100))
    assert silence([f], read_verdicts([first, second]), smap, config.load()) == 0
    assert f.status == "fail" and any("marked it real" in h for h in f.history)
    assert silence([f], read_verdicts([first]), smap, config.load()) == 1 and f.status == "silenced"


def test_examples_are_the_most_similar_verdicts():
    from qc.lessons import pick
    vs = [{"check": "B2", "check_name": "Unknown word", "finding": f"word {i}", "text": "", "verdict": "real",
           "comment": "", "drawing": ""} for i in range(20)]
    vs.append({"check": "C4", "check_name": "Note fits its item", "finding": "Note 3 item 5 EPOXY ADHESIVE not for wetting",
               "text": "WETTING OF RIB USING ITEM NO.5", "verdict": "false", "comment": "resins wet", "drawing": ""})
    f = Finding("C4", "look", "fail", "Note 2: item 4 EPOXY ADHESIVE used for wetting?", None, "WETTING USING ITEM 4")
    got = pick(vs, [f], k=3)
    assert len(got) == 3 and got[0]["check"] == "C4"


class LearnModel:
    name, vision = "fake", False

    def ask(self, system, user, schema, images=None, max_tokens=300):
        return {"rules": [{"rule": "Resins and epoxy are used for wetting.", "based_on": [1]},
                          {"rule": "Ignore everything on jig notes.", "based_on": [1]}]}


def test_learn_keeps_only_rules_the_engineer_accepts(tmp_path):
    from qc.lessons import learn, load_house_rules, with_house_rules
    p = tmp_path / "v.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "group", "check", "check_name", "drawing", "text", "finding", "your_verdict", "your_comment"])
        w.writerow(["Q002", "must", "M1", "Model observation", "AB-CD 10-E-01", "WETTING OF RIB USING ITEM NO.5",
                    "Epoxy is not used for wetting.", "false", "resins are used for wetting"])
        w.writerow(["Q003", "must", "A6", "No.", "AB-CD 10-E-01", "", "x", "real", ""])
    answers = iter(["a", "r"])
    printed = []
    assert learn(tmp_path, LearnModel(), "POLICY", ask=lambda _: next(answers), out=printed.append) == 1
    rules = load_house_rules(tmp_path)
    assert "Resins and epoxy" in rules and "Ignore everything" not in rules
    assert "## House rules" in with_house_rules("POLICY", tmp_path)
    # the same verdicts are not proposed again
    printed.clear()
    assert learn(tmp_path, LearnModel(), "POLICY", ask=lambda _: "a", out=printed.append) == 0
    assert any("No new" in x for x in printed)


def test_score_counts_real_reported_items(tmp_path):
    from qc.lessons import score, score_text
    p = tmp_path / "run1.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "group", "check", "check_name", "finding", "your_verdict (real / false / dont care)"])
        for g, c, v in (("must", "C1", "real"), ("look", "M1", "false"), ("look", "M1", "dont care"),
                        ("dropped", "A5", "real")):
            w.writerow(["Q", g, c, c, "f", v])
    r = score([tmp_path])[0]
    assert (r["judged"], r["real"], r["false"], r["dont_care"], r["wrong_drops"]) == (3, 1, 1, 1, 1)
    assert "33%" in score_text([r])


def test_decisions_retry_in_halves_when_the_answer_is_cut_off(qcset):
    from qc.model import ModelError
    _, sheets, smap = qcset
    fs = [f for f in run_all(sheets, smap, config.load()) if f.status == "fail" and f.sheet is sheets[0]][:4]
    assert len(fs) >= 2

    class Cut(PolicyModel):
        def ask(self, system, user, schema, images=None, max_tokens=300):
            if "decisions" in schema["properties"]:
                import re as _re
                n = sum(1 for line in user.splitlines() if _re.match(r"\d+\. \[", line))
                if n > 1:
                    raise ModelError("answer cut off by the output limit")
                return {"decisions": [{"n": 1, "decision": "look", "wording": "ok", "reason": "r"}]}
            return super().ask(system, user, schema, images, max_tokens)
    decide(fs, sheets, Cut(), load_policy(None), [], config.load(), lambda *_: None, smap=smap)
    assert all(f.message == "ok" for f in fs)


def test_examples_keep_some_real_verdicts():
    from qc.lessons import pick
    vs = [{"check": "M1", "check_name": "Model observation", "finding": f"obs {i}", "text": "", "verdict": "false",
           "comment": "no", "drawing": ""} for i in range(15)]
    vs += [{"check": "C7", "check_name": "Jig", "finding": "jig", "text": "", "verdict": "real", "comment": "",
            "drawing": ""}, {"check": "B3", "check_name": "Spelt", "finding": "two ways", "text": "", "verdict": "real",
                             "comment": "", "drawing": ""}]
    got = pick(vs, [], codes={"M1"}, k=6)
    assert len(got) == 6 and sum(v["verdict"] == "real" for v in got) == 2


def test_prompt_longer_than_context_is_refused():
    from qc.model import Client, ModelError
    c = Client(context=1000)
    with pytest.raises(ModelError, match="too long"):
        c.ask("s", "x" * 5000, {"type": "object"}, max_tokens=100)
    assert c.too_long == 1


class TbModel(FakeModel):
    def __init__(self, kind="drawing", box_ok=True):
        super().__init__()
        self.kind, self.box_ok = kind, box_ok

    def ask(self, system, user, schema, images=None, max_tokens=300):
        props = schema["properties"]
        if "box_1_is_title_block" in props:
            assert images
            return {"kind": self.kind, "box_1_is_title_block": self.box_ok, "area": "bottom-right"}
        if "no_cell_for" in props:
            assert images
            return {"fields": {"drawing_number": "AB-CD 10-E-02", "title": "BASE PLATE", "scale": "1:1",
                               "material": "INVENTED ALLOY 123"}, "no_cell_for": ["weight"],
                    "makes_sense": "fine"}
        return super().ask(system, user, schema, images, max_tokens)


def test_title_block_that_makes_no_sense_is_reread_by_the_model(tmp_path):
    from qc.tbcheck import reasons, recheck
    q = _part(tmp_path)[0]
    cfg = config.load()
    assert reasons(q, cfg) == []
    tb = q.record["title_block"]
    tb.update(drawing_number="", title="", scale="SHEET 1", weight="")
    assert reasons(q, cfg)
    assert recheck([q], TbModel(), cfg, lambda *_: None) == 1
    assert tb["drawing_number"] == "AB-CD 10-E-02" and tb["title"] == "BASE PLATE" and tb["scale"] == "1:1"
    assert tb["material"] == "AL 6061-T6"                     # invented value is not on the sheet: ignored
    assert any("not found in the sheet text" in c for c in q.tb_check["changes"])
    a5 = [f for f in run_all([q], SetMap([q]), cfg) if f.code == "A5"]
    weight = [f for f in a5 if "WEIGHT" in f.message]
    assert weight and weight[0].severity == "look" and "no WEIGHT cell" in weight[0].message


def test_document_pages_are_not_held_to_a_drawing_title_block(tmp_path):
    from qc.tbcheck import recheck
    q = _part(tmp_path)[0]
    cfg = config.load()
    q.record["title_block"].update(drawing_number="", title="", material="", weight="", scale="")
    recheck([q], TbModel(kind="document", box_ok=False), cfg, lambda *_: None)
    assert q.doc_kind == "document"
    msgs = [f.message for f in run_all([q], SetMap([q]), cfg) if f.code == "A5"]
    assert not any(w in m for m in msgs for w in ("MATERIAL", "WEIGHT", "SCALE"))
    q.page = 2
    assert not [f for f in run_all([q], SetMap([q]), cfg) if f.code == "A5"]


# ---------------------------------------------------------------------------
# Tables, notes, legend, second look - text-layer and scanned sheets
# ---------------------------------------------------------------------------
from conftest import requires_tesseract  # noqa: E402


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    from make_qc_set import make_cases
    d = tmp_path_factory.mktemp("cases")
    make_cases(str(d / "AB-CD 10-E-05 REV.00.pdf"))
    return read_pdf(d / "AB-CD 10-E-05 REV.00.pdf")[0]


def _check_case_sheet(q, ocr=False):
    t = q.record["tables"]
    assert len(t) == 1 and len(t[0]["rows"]) == 5
    spec = t[0]["spec"]
    assert "2.5:1 FOR FULL BAND" in spec["VSWR"] and spec["VSWR"].startswith("1.8:1 FOR 90% OF BAND")
    assert spec["FINISH"] == "CLEAR ANODISED"                      # nothing from the title block glued on
    assert [n.text for n in q.notes] == [f"USE FILE {q.number}.STEP FOR MACHINING"]  # legend, view text, CL left out
    assert q.blocks_of("legend")
    cfg = config.load()
    fs = run_all([q], SetMap([q]), cfg)
    codes = {f.code for f in fs if f.status in ("fail", "check", "doubt", "ask") and f.code in ("D1", "D5")}
    assert not codes, [f.message for f in fs if f.code in codes]
    assert not [f for f in fs if f.code == "A5" and "WEIGHT" in f.message]   # WEIGHT is in the table


def test_spec_table_is_read_by_its_cells(cases):
    _check_case_sheet(cases)


@requires_tesseract
def test_spec_table_is_read_by_its_cells_on_a_scan(tmp_path):
    from make_qc_set import make_cases
    p = tmp_path / "AB-CD 10-E-06 REV.00.pdf"
    make_cases(str(p), "AB-CD 10-E-06", scanned=True)
    q = read_pdf(p)[0]
    assert q.ocr
    rows = q.record["tables"][0]["rows"]
    assert len(rows) == 5
    vswr = next(r for r in rows if any("VSWR" in c for c in r))
    assert any("2.5:1 FOR FULL BAND" in c and "1.8:1" in c for c in vswr)
    assert [n.text for n in q.notes] == ["USE FILE AB-CD 10-E-06.STEP FOR MACHINING"]
    fs = run_all([q], SetMap([q]), config.load())
    assert not [f for f in fs if f.code == "D5" and "no '*' legend" in f.message]


def test_note_numbers_are_not_values():
    from qc.sheets import NOTE_NUM, _real_words
    assert not NOTE_NUM.match("1.8:1 FOR 100% OF BAND")
    assert not NOTE_NUM.match("1.5 MM THICK")
    assert NOTE_NUM.match("1. BREAK ALL SHARP EDGES").group(1) == "1"
    assert NOTE_NUM.match("(3) APPLY ITEM 4").group(1) == "3"
    assert not _real_words("CL 5.0") and _real_words("BREAK EDGES")


def test_note_wording_ignores_drawing_and_file_numbers():
    from qc.checks import _mask, _norm
    from qc.setmap import learn_patterns

    class S:
        patterns = learn_patterns(["XY-AB 12 C-E-01", "XY-AB 12 C-E-02", "XY-AB 12 C-E-13"])
    a = _norm(_mask("USE FILE XY-AB 12 C-15.DXF FOR LASER CUTTING", S))
    b = _norm(_mask("USE FILE XY-AB 12 C-E-18.DXF FOR LASER CUTTING", S))
    c = _norm(_mask("USE TEMPLATE XY-TP-AB 12 C-13 FOR CUTTING", S))
    assert a == b and "AB" not in c


class LookModel(FakeModel):
    def __init__(self, verdict="not_a_problem", text=""):
        super().__init__()
        self.verdict, self.text, self.images = verdict, text, 0

    def ask(self, system, user, schema, images=None, max_tokens=300):
        props = schema["properties"]
        if "verdict" in props:
            assert images
            self.images += len(images)
            return {"verdict": self.verdict, "text": self.text, "reason": "the value is in the next cell"}
        if "is_table" in props:
            return {"is_table": True, "rows": [["1.", "MOUNTING"], ["2.", "SCREWS"]]}
        return super().ask(system, user, schema, images, max_tokens)


def test_second_look_clears_what_the_image_does_not_show(cases):
    from qc.look import second_look
    q = cases
    f = Finding("D4", "look", "fail", "Note 2 is incomplete: 'VSWR' value is missing.", q, "VSWR", "note 2")
    g = Finding("D4", "look", "fail", "Note 1 unclear.", q, f"USE FILE {q.number}.STEP FOR MACHINING", "note 1")
    m = LookModel()
    assert second_look([f], m, config.load(), lambda *_: None) == 1
    assert f.status == "cleared" and "next cell" in f.history[-1] and m.images >= 1
    second_look([g], LookModel("confirmed"), config.load(), lambda *_: None)
    assert g.status == "fail" and "confirmed" in g.history[-1]
    # never twice
    assert second_look([g], LookModel(), config.load(), lambda *_: None) == 0


def test_model_table_reading_is_used_only_when_it_matches_the_sheet(qcset):
    from qc.look import read_tables
    _, sheets, _ = qcset
    q = next(s for s in read_pdf(sheets[1].path))       # a fresh copy: notes 'USE FILE ...', 'BREAK ALL ...'
    b = q.blocks_of("notes")[0]
    q.layout = {"blocks": [(b, "table")], "disagree": []}
    n_before = len(q.notes)
    assert read_tables([q], LookModel(), lambda *_: None) == 0       # the model's cells are not on the sheet
    assert len(q.notes) == n_before and b in q.blocks


def test_model_table_reading_replaces_notes_when_it_matches(qcset):
    from qc.look import read_tables
    _, sheets, _ = qcset
    q = read_pdf(sheets[1].path)[0]
    b = q.blocks_of("notes")[0]
    q.layout = {"blocks": [(b, "table")], "disagree": []}

    class M(LookModel):
        def ask(self, system, user, schema, images=None, max_tokens=300):
            if "is_table" in schema["properties"]:
                return {"is_table": True, "rows": [["1.", f"USE FILE {q.number}.STEP FOR MACHINING"],
                                                   ["2.", "BREAK ALL SHARP EDGES"]]}
            return super().ask(system, user, schema, images, max_tokens)
    assert read_tables([q], M(), lambda *_: None) == 1
    assert not q.notes and q.record["tables"][-1]["rows"][1][1] == "BREAK ALL SHARP EDGES"


def test_title_block_recheck_never_deletes_what_the_code_read(tmp_path):
    from qc.tbcheck import recheck
    q = _part(tmp_path)[0]
    cfg = config.load()
    tb = q.record["title_block"]
    tb.update(drawing_number="", title="", scale="")
    before_box = q.parts["title_block"].region
    recheck([q], TbModel(box_ok=False), cfg, lambda *_: None)
    assert tb["drawn_by"] == "A.B" and tb["material"] == "AL 6061-T6"      # kept
    assert tb["drawing_number"] == "AB-CD 10-E-02"                          # filled
    nb = q.parts["title_block"].region
    assert nb[0] <= before_box[0] and nb[2] >= before_box[2]                 # never shrinks


def test_layout_opinion_alone_does_not_start_a_recheck(tmp_path):
    from qc.tbcheck import recheck
    q = _part(tmp_path)[0]
    q.layout = {"disagree": ["box 1 (title_block) looks like notes to the model: ''"], "blocks": []}
    m = TbModel()
    assert recheck([q], m, config.load(), lambda *_: None) == 0 and m.calls == 0


@requires_tesseract
def test_ocr_and_image_disagreement_goes_to_check_by_hand(tmp_path):
    from qc.tbcheck import compare
    from make_qc_set import make_cases
    p = tmp_path / "AB-CD 10-E-06 REV.00.pdf"
    make_cases(str(p), "AB-CD 10-E-06", scanned=True)
    q = read_pdf(p)[0]
    assert compare(q, "AB-CD 10-E-06")[0] == "agree"
    assert compare(q, "ZZ-QQ 99-K-77")[0] in ("absent", "conflict")
