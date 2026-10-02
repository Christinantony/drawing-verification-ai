"""
main.py
CLI entry point.

Usage (from PowerShell / cmd, inside the project's venv):

    python main.py path\\to\\drawing.pdf
    python main.py path\\to\\scan.png -o reports\\scan_report.md --json
    python main.py drawing.pdf --model gemma4:e4b --no-thinking

Everything runs locally against Ollama on http://localhost:11434 - no
network access is used or required once the model has been pulled.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config
import extraction
import structuring
import audit
from ollama_client import get_client, check_model_available


def build_report_markdown(source_path: Path, structured: dict, audit_result: dict) -> str:
    lines = [
        "# Drawing Verification Report",
        "",
        f"**Source:** `{source_path}`",
        f"**Pages:** {structured.get('page_count')}  "
        f"**Extraction:** {', '.join(structured.get('extraction_sources', []))}  "
        f"**Title block:** {structured.get('title_block_cell_count', 0)} bordered field box(es), "
        f"{structured.get('title_block_method', 'regex_only')}",
        f"**Overall status:** {audit_result.get('overall_status', 'UNKNOWN')}",
        "",
        "## Title Block",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for k, v in structured.get("title_block", {}).items():
        lines.append(f"| {k} | {v if v else '_(not found)_'} |")

    lines += ["", "## Bill of Materials", ""]
    bom = structured.get("bom", [])
    if bom:
        headers = sorted({k for row in bom for k in row.keys()})
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "---|" * len(headers))
        for row in bom:
            lines.append("| " + " | ".join(row.get(h, "") for h in headers) + " |")
    else:
        lines.append("_No BOM rows detected._")

    lines += ["", "## Findings", ""]
    findings = audit_result.get("findings", [])
    if not findings:
        lines.append("No issues found.")
    for f in findings:
        lines.append(f"### [{str(f.get('severity', '?')).upper()}] {f.get('id', '')} - {f.get('category', '')}")
        lines.append(f"- **Location:** {f.get('location', '')}")
        lines.append(f"- **Issue:** {f.get('description', '')}")
        if f.get("suggested_fix"):
            lines.append(f"- **Suggested fix:** {f.get('suggested_fix')}")
        lines.append("")

    lines += ["## Summary", "", audit_result.get("summary", ""), ""]

    if audit_result.get("_thinking_trace"):
        lines += [
            "<details><summary>Model reasoning trace (Gemma 4 &lt;|think|&gt;)</summary>",
            "",
            "```",
            audit_result["_thinking_trace"][:8000],
            "```",
            "</details>",
        ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Offline 2D engineering-drawing verification checker "
                     "(Gemma 4 E4B via Ollama)."
    )
    parser.add_argument("input", help="Path to a drawing PDF or image (PNG/JPG/TIFF/BMP).")
    parser.add_argument("-o", "--output",
                         help="Report file to write (.md). Default: reports/<name>_report.md")
    parser.add_argument("--model", default=config.MODEL_NAME,
                         help=f"Ollama model tag (default: {config.MODEL_NAME})")
    parser.add_argument("--no-thinking", action="store_true",
                         help="Disable Gemma 4's <|think|> reasoning mode (faster, shallower).")
    parser.add_argument("--json", action="store_true",
                         help="Also write the full structured+audit data as JSON next to the report.")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Input file not found: {src}")

    client = get_client()
    if not check_model_available(client, args.model):
        sys.exit(
            f"Could not find model '{args.model}' on your local Ollama server.\n"
            f"  1. Make sure Ollama is running (it starts automatically after install on Windows,\n"
            f"     or run 'ollama serve' in a terminal).\n"
            f"  2. Pull the model:  ollama pull {args.model}"
        )

    print(f"[1/3] Extracting text/tables from {src.name} ...")
    doc = extraction.load_document(src)

    sources = ", ".join(sorted({p.source for p in doc.pages}))
    orientations = ", ".join(f"p{p.page_number}={p.orientation}"
                              + (f"(rotated {p.rotation_applied_deg}deg)" if p.rotation_applied_deg else "")
                              for p in doc.pages)
    print(f"       text source: {sources}  |  orientation: {orientations}")

    print("[2/3] Structuring title block + BOM ...")
    structured = structuring.build_structured_document(doc)
    structured_md = structuring.to_markdown(structured)
    print(f"       title block: {structured['title_block_cell_count']} bordered field box(es) "
          f"detected ({structured['title_block_method']})")

    thinking_label = "on" if not args.no_thinking else "off"
    print(f"[3/3] Auditing with {args.model} (thinking={thinking_label}) - this can take a "
          f"while on first load / CPU-only hardware ...")
    audit_result = audit.audit_drawing(
        structured_md, client, model=args.model, use_thinking=not args.no_thinking
    )

    out_path = Path(args.output) if args.output else config.DEFAULT_OUTPUT_DIR / f"{src.stem}_report.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_report_markdown(src, structured, audit_result), encoding="utf-8")

    if args.json:
        json_path = out_path.with_suffix(".json")
        json_path.write_text(
            json.dumps({"structured": structured, "audit": audit_result}, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"Structured JSON: {json_path}")

    print(f"\nStatus: {audit_result.get('overall_status')}")
    print(f"Report written to: {out_path}")


if __name__ == "__main__":
    main()
