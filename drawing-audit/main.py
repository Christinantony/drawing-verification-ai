"""
drawing-audit: check one drawing, or every drawing in a folder.

    python main.py path\\to\\drawing.pdf
    python main.py path\\to\\folder --json
    python main.py drawing.pdf --no-model      # rules + dictionary only, no Ollama
    python main.py drawing.pdf --deep          # also the slow full reasoning audit

What happens per sheet:
  1. sheetreader reads it: text layer if there is one (SolidWorks), OCR after
     finding which way up it is if not (AutoCAD via Print to PDF, scans).
  2. Deterministic rules (rules.py): fields, revision table, file name,
     sheet size, scale, BOM numbering/quantities, sign-offs. Instant.
  3. Language (language.py): known misspellings, dictionary, then one short
     Gemma call with thinking off.
  4. --deep only: the full structured sheet goes to Gemma with thinking on.

Everything runs locally; nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import config  # noqa: E402
import language  # noqa: E402
import rules  # noqa: E402
from sheetreader.loader import IMAGE_SUFFIXES  # noqa: E402
from sheetreader.record import read_drawing  # noqa: E402
from sheetreader.titleblock import DEFAULT_LABELS  # noqa: E402

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _labels():
    labels = {k: list(v) for k, v in DEFAULT_LABELS.items()}
    for k, v in config.EXTRA_TITLE_BLOCK_LABELS.items():
        labels.setdefault(k, [])
        labels[k] = list(v) + labels[k]
    return labels


def record_to_markdown(rec: dict) -> str:
    """The structured sheet as Markdown (also what --deep sends to the model)."""
    tb = rec["title_block"]
    lines = ["| Field | Value |", "|---|---|"]
    for k, v in tb.items():
        lines.append(f"| {k} | {v if v else '_(not found)_'} |")
    gt = rec.get("general_tolerance") or {}
    if gt:
        lines += ["", "**General tolerances:** " + ", ".join(
            f"{k.replace('_', ' ')} {v}" for k, v in gt.items() if v)]
    lines += ["", "**Bill of materials**", ""]
    bom = rec.get("bom") or []
    if bom:
        cols = ["item", "description", "qty", "ref"]
        cols += sorted({k for r in bom for k in r} - set(cols))
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in bom:
            lines.append("| " + " | ".join((r.get(c) or "").replace("|", "/") for c in cols) + " |")
    else:
        lines.append("_No BOM on this sheet._")
    rt = rec.get("revision_table") or []
    lines += ["", "**Revision table:** " + ("; ".join(
        f"REV {r.get('rev', '?')}: {r.get('details', '')}" for r in rt) if rt else "_empty_")]
    if rec.get("notes"):
        lines += ["", "**Notes**", ""] + [f"- {n}" for n in rec["notes"]]
    return "\n".join(lines)


def build_report(results: list) -> str:
    out = ["# Drawing Verification Report", ""]
    if len(results) > 1:
        out += ["| Drawing | Status | High | Medium | Low | Read via | Time |", "|---|---|---|---|---|---|---|"]
        for r in results:
            c = {s: sum(1 for f in r["findings"] if f["severity"] == s) for s in ("high", "medium", "low")}
            out.append(f"| {r['record']['file']} p{r['record']['page']} | **{r['status']}** | {c['high']} | "
                       f"{c['medium']} | {c['low']} | {r['record']['text_source']} | {r['seconds']:.0f}s |")
        out.append("")
    for r in results:
        rec = r["record"]
        how = ("PDF text layer" if rec["text_source"] == "embedded"
               else f"OCR (no text layer; rotated {rec['rotation_deg']} deg to read upright)")
        out += [f"## {rec['file']} - page {rec['page']} of {rec['page_count']}", "",
                f"**Status: {r['status']}**  ",
                f"Read via {how}; sheet {rec.get('paper_size') or '?'} {rec['orientation']}; "
                f"producer: {rec.get('producer') or 'unknown'}; {r['seconds']:.1f}s", ""]
        findings = sorted(r["findings"], key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
        out += ["### Findings", ""]
        if not [f for f in findings if f["severity"] != "info"]:
            out.append("No issues found.")
        for f in findings:
            fix = f" **Fix:** {f['suggested_fix']}" if f.get("suggested_fix") else ""
            out.append(f"- **[{f['severity'].upper()}]** `{f['location']}` {f['description']}{fix} "
                       f"_({f.get('source', 'rule')})_")
        out += ["", "### What was read", "", record_to_markdown(rec), ""]
        trace = r.get("language_trace") or {}
        if trace.get("model_error"):
            out += [f"_Language model not used: {trace['model_error']}_", ""]
        if r.get("deep"):
            d = r["deep"]
            out += ["### Deep audit (Gemma, thinking on)", "", d.get("summary", ""), ""]
            if d.get("_thinking_trace"):
                out += ["<details><summary>Reasoning trace</summary>", "", "```",
                        d["_thinking_trace"][:8000], "```", "</details>", ""]
    return "\n".join(out)


def collect_inputs(path: Path):
    if path.is_dir():
        return sorted(p for p in path.iterdir()
                      if p.suffix.lower() == ".pdf" or p.suffix.lower() in IMAGE_SUFFIXES)
    return [path]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline engineering-drawing checker (Gemma 4 E4B via Ollama).")
    ap.add_argument("input", help="A drawing PDF/image, or a folder of them.")
    ap.add_argument("-o", "--output", help="Report file (.md). Default: reports/<name>_report.md")
    ap.add_argument("--json", action="store_true", help="Also write the full records + findings as JSON.")
    ap.add_argument("--model", default=config.MODEL_NAME)
    ap.add_argument("--no-model", action="store_true",
                    help="Skip Gemma entirely (rules, known misspellings and dictionary only).")
    ap.add_argument("--deep", action="store_true",
                    help="Also run the full reasoning audit with thinking on (slow on CPU).")
    ap.add_argument("--force-ocr", action="store_true", help="OCR even if the PDF has a text layer.")
    args = ap.parse_args(argv)

    if config.TESSERACT_CMD:
        os.environ["TESSERACT_CMD"] = config.TESSERACT_CMD

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Input not found: {src}")
    inputs = collect_inputs(src)
    if not inputs:
        sys.exit(f"No PDF or image files in {src}")

    client = None
    if not args.no_model:
        from ollama_client import check_model_available, get_client
        client = get_client(config.OLLAMA_HOST)
        if not check_model_available(client, args.model):
            print(f"  ! Model '{args.model}' not available on Ollama ({config.OLLAMA_HOST}); "
                  f"continuing without it. Run: ollama pull {args.model}", file=sys.stderr)
            client = None

    labels = _labels()
    results = []
    for path in inputs:
        print(f"Reading {path.name} ...")
        t0 = time.time()
        try:
            records = read_drawing(path, force_ocr=args.force_ocr, labels=labels)
        except Exception as e:
            print(f"  ! could not read {path.name}: {e}", file=sys.stderr)
            continue
        for rec_obj in records:
            rec = rec_obj.to_dict()
            findings = rules.run_rules(rec)
            lang, trace = language.check_language(
                rec, client=client, model=args.model,
                extra_words=config.EXTRA_ENGINEERING_WORDS, options=config.LANGUAGE_OPTIONS)
            findings += lang
            deep = None
            if args.deep and client is not None:
                import audit
                deep = audit.audit_drawing(record_to_markdown(rec), client, model=args.model,
                                           use_thinking=config.USE_THINKING)
                for f in deep.get("findings", []):
                    f["source"] = "model-deep"
                    findings.append(f)
            for i, f in enumerate(findings, 1):
                f["id"] = f"F{i}"
            status = rules.overall_status(findings)
            secs = time.time() - t0
            print(f"  page {rec['page']}: {status}  ({rec['text_source']}, {secs:.1f}s, "
                  f"{sum(1 for f in findings if f['severity'] != 'info')} finding(s))")
            results.append({"record": rec, "findings": findings, "status": status,
                            "seconds": secs, "language_trace": trace, "deep": deep})

    if not results:
        sys.exit("Nothing could be read.")
    name = src.name if src.is_dir() else src.stem
    out = Path(args.output) if args.output else config.DEFAULT_OUTPUT_DIR / f"{name}_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(results), encoding="utf-8")
    if args.json:
        slim = [{**r, "record": {k: v for k, v in r["record"].items() if k != "text_lines"}} for r in results]
        out.with_suffix(".json").write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
    print(f"\nReport written to {out}")
    return results


if __name__ == "__main__":
    main()
