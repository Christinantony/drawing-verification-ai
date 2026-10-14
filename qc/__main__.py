"""Drawing QC agent.

    python -m qc review  FOLDER [--out DIR] [--model gemma4:26b] [--no-model] [--config FILE]
    python -m qc doctor  [--model gemma4:26b]

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

    log("1/7 Intake")
    pdfs, docs, unsupported = intake(folder)
    log(f"    {len(pdfs)} PDF(s), {len(docs)} Word file(s)" + (f", {len(unsupported)} not supported: {', '.join(unsupported)}" if unsupported else ""))
    if not pdfs:
        log("No PDF drawings in the folder.")
        return 3

    log("2/7 Read sheets   3/7 Locate parts")
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

    log("4/7 Map the set")
    smap = SetMap(sheets)
    log(f"    numbering {smap.describe()}; {len(smap.refs)} references")

    log("5/7 QC checks")
    findings = run_all(sheets, smap, cfg)
    chk_results, chk_findings = run_checklists(docs, sheets, out_dir, cfg)
    findings += chk_findings
    for c in chk_results:
        log(f"    checklist {c['file']}: " + (f"{c.get('rows')} rows vs {c['drawing']} - {c.get('match', 0)} match, "
                                             f"{c.get('mismatch', 0)} mismatch, {c.get('filled', 0)} filled, {c.get('empty', 0)} left empty"
                                             if c.get("drawing") else "no matching drawing"))
    asks = sum(f.status == "ask" for f in findings)
    if asks:
        log(f"    {asks} judgement question(s) for the model" + (" - skipped (--no-model)" if args.no_model else ""))
    judge(findings, client, log)

    log("6/7 Doubt loop")
    n = sum(f.status == "doubt" for f in findings)
    doubt_run(findings, sheets, smap, client, cfg, log)
    log(f"    {n} doubtful item(s): {sum(f.status == 'cleared' for f in findings if f.history)} cleared, "
        f"{sum(f.status == 'check' for f in findings)} left for you")

    log("7/7 Report")
    sp = client.speed()
    run = {"folder": str(folder), "started": started, "model": client.name, "version": __version__,
           "seconds": round(time.monotonic() - t0), "unsupported": unsupported,
           "speed": (f"{sp['calls']} model calls, {sp.get('output_tok_s')} tok/s out, {sp.get('prompt_tok_s')} tok/s in"
                     if sp.get("calls") else "")}
    path = write(out_dir, run, sheets, smap, findings, chk_results)
    latest = Path(args.out) / "latest.txt"
    latest.write_text(str(path), encoding="utf-8")
    must = sum(f.status == "fail" and f.severity == "must" for f in findings)
    look = sum(f.status == "fail" and f.severity == "look" for f in findings)
    check = sum(f.status in ("check", "doubt") for f in findings)
    log(f"\n{must} must fix, {look} please look, {check} check by hand  ({run['seconds']} s)")
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
    d = sub.add_parser("doctor", help="check the setup: packages, Tesseract, Ollama, the model")
    d.add_argument("--model")
    d.add_argument("--host")
    d.add_argument("--config")
    d.add_argument("--quick", action="store_true", help="skip the test calls")
    a = p.parse_args(argv)
    return review(a) if a.command == "review" else doctor(a)


if __name__ == "__main__":
    sys.exit(main())
