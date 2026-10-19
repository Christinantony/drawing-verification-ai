"""Drawing QC agent.

    python -m qc review  FOLDER [--out DIR] [--model gemma4:26b] [--no-model] [--config FILE]
    python -m qc doctor  [--model gemma4:26b]
    python -m qc learn   [--verdicts DIR]     house rules from your false verdicts (you approve each)
    python -m qc score   [--verdicts DIR]     how many reported items were real, run by run

Stations, in order:
    1 intake  2 read  3 locate parts  4 map the set  5 QC checks (+ model judgement)
    6 doubt loop  7 report
"""
from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "dimcheck", "coherence", "drawing-audit"):
    p = str(ROOT / sub) if sub else str(ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)

from . import config as qc_config  # noqa: E402

__version__ = "0.1.0"
SKIP = {"thumbs.db", "desktop.ini", ".ds_store"}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Station 1: intake
# ---------------------------------------------------------------------------
def intake(folder: Path):
    pdfs, docs, unsupported = [], [], []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.lower() in SKIP or p.name.startswith("~$"):
            continue
        ext = p.suffix.lower()
        if ext == ".pdf":
            pdfs.append(p)
        elif ext in (".doc", ".docx"):
            docs.append(p)
        else:
            unsupported.append(p.name)
    return pdfs, docs, unsupported


def run_checklists(docs, sheets, out_dir: Path, cfg):
    """C8: every dimension checklist against its drawing; empty tolerance
    cells filled in a copy (yellow = read from drawing, orange = confirm)."""
    from check_coherence import match_checklist, read_checklist
    from dimcheck.pipeline import process
    from .checks import Finding
    results, findings = [], []
    by_file = {s.file: s for s in sheets}
    for d in docs:
        chk = read_checklist(d)
        if chk is None:
            continue
        pdf = match_checklist(chk, list(by_file))
        res = {"file": d.name, "rows": chk["rows"], "drawing": None}
        results.append(res)
        if not pdf:
            findings.append(Finding("C8", "look", "fail", f"Checklist {d.name}: no drawing in this folder matches its file name."))
            continue
        s = by_file[pdf]
        res["drawing"] = s.number
        copy = out_dir / "checklists" / (Path(d.name).stem + "_completed.docx")
        copy.parent.mkdir(parents=True, exist_ok=True)
        try:
            rows = process(s.path, str(d), str(copy), mark_filled=True)
        except Exception as e:
            findings.append(Finding("C8", "look", "check", f"Checklist {d.name} could not be compared: {e}", s))
            continue
        res.update(match=sum(r.verdict == "match" for r in rows), mismatch=sum(r.verdict.startswith("MISMATCH") for r in rows),
                   filled=sum(r.verdict == "filled" for r in rows), empty=sum(r.verdict == "not set" for r in rows),
                   copy=str(copy.relative_to(out_dir)) if any(r.verdict == "filled" for r in rows) else None)
        if not res["copy"] and copy.exists():
            copy.unlink()
        for r in rows:
            if r.verdict.startswith("MISMATCH"):
                findings.append(Finding("C8", cfg["severity"]["C8"], "fail",
                                        f"Checklist {d.name}, zone {r.zone} dimension {r.dimension}: checklist says "
                                        f"{r.existing_tolerance}, drawing says {r.resolved_tolerance} ({r.source}).", s,
                                        r.dimension, f"zone {r.zone}"))
            elif r.verdict == "not set":
                findings.append(Finding("C8", "look", "check",
                                        f"Checklist {d.name}, zone {r.zone} dimension {r.dimension}: tolerance not found "
                                        f"on the drawing; fill by hand. {'; '.join(r.notes)}", s, r.dimension, f"zone {r.zone}"))
    return results, findings


# ---------------------------------------------------------------------------
def review(args) -> int:
    from .checks import run_all
    from .doubt import run as doubt_run
    from .judge import judge
    from .model import Client, NoModel
    from .report import write
    from .setmap import SetMap
    from .sheets import read_pdf

    cfg = qc_config.load(args.config)
    if args.model:
        cfg["model"]["name"] = args.model
    if args.host:
        cfg["model"]["host"] = args.host
    folder = Path(args.folder)
    if not folder.is_dir():
        log(f"Not a folder: {folder}")
        return 3
    started = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    t0 = time.monotonic()
    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / run_id

    # model first, so a setup problem shows before minutes of reading
    if args.no_model:
        client = NoModel()
    else:
        m = cfg["model"]
        client = Client(m["name"], m["host"], m["context"], m["seconds_per_call"])
        try:
            names = client.installed()
        except Exception as e:
            log(f"Cannot reach Ollama at {m['host']} ({e}). Start Ollama, or use --no-model.")
            return 3
        if m["name"] not in names and m["name"] + ":latest" not in names:
            log(f"Model '{m['name']}' is not installed. Installed: {', '.join(names) or 'none'}.\n"
                f"Run: ollama pull {m['name']}   (or pass --model NAME)")
            return 3
        log(f"Model: {m['name']} ({'can' if client.vision else 'cannot'} read images)")

    steps = dict(cfg.get("model_steps", {}))
    if args.decide == "rules":
        steps.update(decide=False, open_review=False, summary=False)
    for name in (args.skip or "").split(","):
        if name.strip():
            steps[name.strip()] = False
    if args.no_model:
        steps = {k: False for k in steps}
    timing = {}

    def step(name):
        timing[name] = time.monotonic()

    def done(name):
        timing[name] = round(time.monotonic() - timing[name], 1)

    log("1/8 Intake")
    pdfs, docs, unsupported = intake(folder)
    if unsupported:
        from collections import Counter
        kinds = Counter((Path(n).suffix.lower() or "(no extension)") for n in unsupported)
        skipped = ", ".join(f"{n} {k}" for k, n in kinds.most_common())
    log(f"    {len(pdfs)} PDF(s), {len(docs)} Word file(s)" + (f"; skipped {len(unsupported)} other file(s): {skipped}" if unsupported else ""))
    if not pdfs:
        log("No PDF drawings in the folder.")
        return 3

    log("2/8 Read sheets, 3/8 locate the parts")
    step("read")
    sheets = []
    for p in pdfs:
        t = time.monotonic()
        try:
            got = read_pdf(p)
        except Exception as e:
            log(f"    {p.name}: could not read ({e})")
            continue
        sheets += got
        log(f"    {p.name}: {'OCR' if got and got[0].ocr else 'text layer'}, {len(got)} sheet(s), {time.monotonic() - t:.0f} s")
    done("read")
    if steps.get("layout") and getattr(client, "vision", False):
        from .layout import judge_layout
        log("    layout check by the model (numbered boxes)...")
        step("layout")
        judge_layout(sheets, client, log)
        done("layout")
        n_changes = sum(len(q.layout["changes"]) + len(q.layout["added"]) for q in sheets)
        log(f"    layout check: {n_changes} change(s), {sum(len(q.layout['disagree']) for q in sheets)} disagreement(s) reported")

    from .tbcheck import reasons as tb_reasons, recheck as tb_recheck
    if steps.get("tb_check") and getattr(client, "vision", False):
        step("tb_check")
        n_tb = tb_recheck(sheets, client, cfg, log)
        done("tb_check")
        log(f"    title-block check: {n_tb} sheet(s) did not make sense and were re-read by the model")
    else:
        for q in sheets:
            why = tb_reasons(q, cfg)
            q.tb_check = {"reasons": why, "changes": [], "kind": "drawing", "no_cell_for": [], "note": ""}
            if why:
                log(f"    {q.number or q.file}: title block does not make sense ({'; '.join(why)[:110]}) - "
                    "not re-checked (needs the model with images)")

    from .pm import resolve as resolve_pm
    pm_findings = resolve_pm(sheets, client if not args.no_model else NoModel(), log,
                             vision=bool(steps.get("layout") is not False and getattr(client, "vision", False)
                                         and not args.no_model))

    log("4/8 Map the set")
    smap = SetMap(sheets)
    log(f"    numbering {smap.describe()}; {len(smap.refs)} references")

    log("5/8 QC checks (facts)")
    step("checks")
    findings = run_all(sheets, smap, cfg) + pm_findings
    chk_results, chk_findings = run_checklists(docs, sheets, out_dir, cfg)
    findings += chk_findings
    for c in chk_results:
        log(f"    checklist {c['file']}: " + (f"{c.get('rows')} rows vs {c['drawing']} - {c.get('match', 0)} match, "
                                             f"{c.get('mismatch', 0)} mismatch, {c.get('filled', 0)} filled, {c.get('empty', 0)} left empty"
                                             if c.get("drawing") else "no matching drawing"))
    done("checks")
    asks = sum(f.status == "ask" for f in findings)
    if asks:
        log(f"    {asks} judgement question(s) for the model" + ("" if steps.get("judge") else " - skipped"))
    step("judge")
    judge(findings, client if steps.get("judge") else NoModel(), log)
    done("judge")

    log("6/8 Doubt loop")
    step("doubt")
    n = sum(f.status == "doubt" for f in findings)
    doubt_run(findings, sheets, smap, client if steps.get("doubt") else NoModel(), cfg, log)
    done("doubt")
    log(f"    {n} doubtful item(s): {sum(f.status == 'cleared' for f in findings if f.history)} cleared, "
        f"{sum(f.status == 'check' for f in findings)} left for you")

    # Lessons from your earlier verdicts: silence what you marked false before
    from .lessons import read_verdicts, score, silence, with_house_rules
    vdir = args.verdicts or cfg.get("verdicts_dir")
    verdicts = read_verdicts(([vdir] if vdir else []) + list(args.examples or cfg.get("examples") or []))
    if verdicts:
        log(f"    {len(verdicts)} earlier verdict(s) from your verdicts folder / files")
    silence(findings, verdicts, smap, cfg, log)

    summary = {}
    if steps.get("decide") or steps.get("open_review"):
        from .decide import decide, draft_summary, load_examples, load_policy, open_review
        policy = with_house_rules(load_policy(args.policy or cfg.get("policy_file")), vdir)
        examples = verdicts
        log(f"7/8 Model decisions under the QC policy" + (" + your house rules" if policy.count("## House rules") else "")
            + (f" (the most similar of {len(examples)} earlier verdicts as examples)" if examples else ""))
        if steps.get("decide"):
            step("decide")
            decide(findings, sheets, client, policy, examples, cfg, log, smap=smap)
            done("decide")
            log(f"    {sum(f.status == 'dropped' for f in findings)} fact(s) dropped by the model (listed in the report)")
        if steps.get("open_review"):
            step("open_review")
            extra = open_review(sheets, findings, client, policy, examples, log, smap=smap, cfg=cfg)
            findings += extra
            done("open_review")
            log(f"    open review: {len(extra)} model observation(s)")
            silence(extra, verdicts, smap, cfg, log)
        if steps.get("summary"):
            step("summary")
            summary = draft_summary(sheets, findings, client, log)
            done("summary")
    else:
        log("7/8 Decisions: rules (no model decisions)")

    log("8/8 Report")
    sp = client.speed()
    sc = score(([vdir] if vdir else []) + list(args.examples or cfg.get("examples") or []))
    run = {"folder": str(folder), "started": started, "model": client.name, "version": __version__, "run_id": run_id,
           "score": sc[-1] if sc else None,
           "seconds": round(time.monotonic() - t0), "unsupported": unsupported, "timing": timing,
           "decisions": "model + QC policy" if steps.get("decide") else "rules",
           "summary": summary,
           "speed": (f"{sp['calls']} model calls, {sp.get('output_tok_s')} tok/s out, {sp.get('prompt_tok_s')} tok/s in"
                     if sp.get("calls") else "")}
    path = write(out_dir, run, sheets, smap, findings, chk_results)
    latest = Path(args.out) / "latest.txt"
    latest.write_text(str(path), encoding="utf-8")
    must = sum(f.status == "fail" and f.severity == "must" for f in findings)
    look = sum(f.status == "fail" and f.severity == "look" for f in findings)
    check = sum(f.status in ("check", "doubt") for f in findings)
    log(f"\n{must} must fix, {look} please look, {check} check by hand  ({run['seconds']} s)")
    if timing:
        log("    time: " + ", ".join(f"{k} {v} s" for k, v in timing.items() if isinstance(v, float)))
    if sp.get("too_long"):
        log(f"    note: {sp['too_long']} model call(s) did not fit the context window ({cfg['model']['context']} tokens). "
            f"Set \"model\": {{\"context\": 16384}} in your settings file (uses about 2 GB more RAM).")
    log(f"Report: {path}")
    return 1 if must else 0


# ---------------------------------------------------------------------------
def doctor(args) -> int:
    from .model import Client, ModelError
    ok = True

    def row(good, what, hint=""):
        nonlocal ok
        ok &= bool(good) or good is None
        mark = "OK  " if good else ("--  " if good is None else "FAIL")
        log(f"  {mark} {what}" + (f"  -> {hint}" if hint and not good else ""))

    log(f"Drawing QC agent {__version__}, Python {sys.version.split()[0]}")
    for mod, pipname in (("pymupdf", "pymupdf"), ("numpy", "numpy"), ("PIL", "pillow"), ("pytesseract", "pytesseract"),
                         ("spellchecker", "pyspellchecker"), ("docx", "python-docx"), ("requests", "requests")):
        try:
            __import__(mod)
            row(True, f"python package {pipname}")
        except ImportError:
            row(False, f"python package {pipname}", f"pip install {pipname}")
    try:
        import pytesseract
        from sheetreader.loader import _tesseract
        v = _tesseract().get_tesseract_version()
        row(True, f"Tesseract {v} (for scanned / AutoCAD print-to-PDF sheets)")
    except Exception:
        row(False, "Tesseract", "install from https://github.com/UB-Mannheim/tesseract/wiki, or set TESSERACT_CMD")
    lo = shutil.which("soffice") or shutil.which("libreoffice") or \
        next((p for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",) if Path(p).exists()), None)
    row(True if lo else None, "LibreOffice (only for old .doc checklists)" + ("" if lo else " - not found; .docx still works"))

    cfg = qc_config.load(args.config)
    name = args.model or cfg["model"]["name"]
    c = Client(name, args.host or cfg["model"]["host"], cfg["model"]["context"], cfg["model"]["seconds_per_call"])
    try:
        names = c.installed()
        row(True, f"Ollama reachable at {c.host}")
    except Exception as e:
        row(False, f"Ollama at {c.host}", f"start Ollama ({e})")
        return 3
    have = name in names or name + ":latest" in names
    row(have, f"model {name} installed", f"ollama pull {name}   (installed: {', '.join(names) or 'none'})")
    if not have:
        return 3
    row(True, f"model can read images: {'yes' if c.vision else 'no (box re-reads will use OCR only)'}")
    if args.quick:
        return 0 if ok else 3
    log("  Test calls (the first one loads the model into memory; this can take a minute)...")
    schema = {"type": "object", "properties": {"words": {"type": "array", "items": {
        "type": "object", "properties": {"n": {"type": "integer"}, "verdict": {"type": "string", "enum": ["correct", "misspelt", "unsure"]},
                                         "correction": {"type": "string"}}, "required": ["n", "verdict"]}}}, "required": ["words"]}
    try:
        t = time.monotonic()
        ans = c.ask("You check spelling on engineering drawings. Answer in JSON.",
                    "1. ANEROBIC  -- in: \"ANEROBIC ADHESIVE\"\n2. BRACKET  -- in: \"SUPPORT BRACKET\"\n"
                    "3. COUNTERSUNK  -- in: \"COUNTERSUNK HOLE\"", schema, max_tokens=120)
        verdicts = {w.get("n"): w.get("verdict") for w in ans.get("words", [])}
        good = verdicts.get(1) == "misspelt" and verdicts.get(2) == "correct"
        row(good, f"JSON answer and judgement ({time.monotonic() - t:.0f} s): {verdicts}",
            "the model answered, but wrongly - results may be weak")
        if c.vision:
            from PIL import Image, ImageDraw
            img = Image.new("L", (360, 60), 255)
            ImageDraw.Draw(img).text((10, 20), "XY-AB 12 C3-E-07", fill=0)
            img = img.resize((720, 120))
            t = time.monotonic()
            a2 = c.ask("Transcribe text exactly.", "Write exactly the text in this image.",
                       {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                       images=[img], max_tokens=40)
            row("C3-E-07" in str(a2.get("text", "")).replace(" ", "").upper().replace("C3E07", "C3-E-07") or
                "E-07" in str(a2.get("text", "")), f"image reading ({time.monotonic() - t:.0f} s): '{a2.get('text')}'")
        sp = c.speed()
        log(f"  Speed: {sp['output_tok_s']} tok/s output, {sp['prompt_tok_s']} tok/s prompt")
    except ModelError as e:
        row(False, "test call", str(e))
    return 0 if ok else 3


def learn_cmd(args) -> int:
    from .decide import load_policy
    from .lessons import learn
    from .model import Client
    cfg = qc_config.load(args.config)
    vdir = args.verdicts or cfg.get("verdicts_dir")
    if not vdir:
        log("Give your verdicts folder: --verdicts DIR, or \"verdicts_dir\" in your settings file.")
        return 3
    m = cfg["model"]
    c = Client(args.model or m["name"], args.host or m["host"], m["context"], m["seconds_per_call"])
    try:
        c.installed()
    except Exception as e:
        log(f"Cannot reach Ollama ({e}). `learn` needs the model.")
        return 3
    learn(vdir, c, load_policy(cfg.get("policy_file")), log)
    return 0


def score_cmd(args) -> int:
    from .lessons import score, score_text
    cfg = qc_config.load(args.config)
    src = list(args.files or []) + ([args.verdicts or cfg.get("verdicts_dir")] if (args.verdicts or cfg.get("verdicts_dir")) else [])
    log(score_text(score(src)))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m qc", description="Drawing QC agent (literal text: title block, "
                                "BOM, notes, references). Offline, local model through Ollama.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("review", help="check a folder of drawings (and checklists)")
    r.add_argument("folder")
    r.add_argument("--out", default="qc-output")
    r.add_argument("--model", help="Ollama model name (default from config: gemma4:26b)")
    r.add_argument("--host")
    r.add_argument("--no-model", action="store_true", help="rules and OCR only, no model")
    r.add_argument("--config", help="JSON file with your settings (see qc/qc_config.example.json)")
    r.add_argument("--decide", choices=("model", "rules"), default="model",
                   help="who decides what to report: the model under qc/policy.md (default) or the rules")
    r.add_argument("--policy", help="your QC policy document (default qc/policy.md)")
    r.add_argument("--examples", nargs="*", help="findings.csv files with your verdicts, used as examples")
    r.add_argument("--verdicts", help="your verdicts folder (marked findings.csv files + house_rules.md)")
    r.add_argument("--skip", help="model steps to skip, comma separated: layout,tb_check,judge,doubt,decide,open_review,summary")
    d = sub.add_parser("doctor", help="check the setup: packages, Tesseract, Ollama, the model")
    d.add_argument("--model")
    d.add_argument("--host")
    d.add_argument("--config")
    d.add_argument("--quick", action="store_true", help="skip the test calls")
    lt = sub.add_parser("layout-test", help="one sheet: model draws its own boxes vs judges the program's boxes")
    lt.add_argument("pdf")
    lt.add_argument("--out", default="qc-output")
    lt.add_argument("--model")
    lt.add_argument("--host")
    lt.add_argument("--config")
    le = sub.add_parser("learn", help="propose house rules from your false verdicts; you approve each one")
    le.add_argument("--verdicts")
    le.add_argument("--config")
    le.add_argument("--model")
    le.add_argument("--host")
    sc = sub.add_parser("score", help="how many reported items were real, run by run")
    sc.add_argument("files", nargs="*", help="marked findings.csv files (default: your verdicts folder)")
    sc.add_argument("--verdicts")
    sc.add_argument("--config")
    a = p.parse_args(argv)
    if a.command == "layout-test":
        return layout_test_cmd(a)
    if a.command == "learn":
        return learn_cmd(a)
    if a.command == "score":
        return score_cmd(a)
    return review(a) if a.command == "review" else doctor(a)


def layout_test_cmd(args) -> int:
    from .layout import layout_test
    from .model import Client
    cfg = qc_config.load(args.config)
    m = cfg["model"]
    c = Client(args.model or m["name"], args.host or m["host"], m["context"], m["seconds_per_call"])
    try:
        c.installed()
    except Exception as e:
        log(f"Cannot reach Ollama ({e}).")
        return 3
    if not c.vision:
        log(f"Model {c.name} cannot read images; the layout test needs one that can.")
        return 3
    path = layout_test(args.pdf, Path(args.out), c, log)
    log(f"Layout test: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
