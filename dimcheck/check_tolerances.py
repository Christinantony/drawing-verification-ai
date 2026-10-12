#!/usr/bin/env python3
"""
CLI: read tolerances off a drawing PDF and fill them into a dimension
checklist DOCX.

Usage:
    python check_tolerances.py DRAWING.pdf CHECKLIST.docx -o OUTPUT.docx

Options:
    --page N            Drawing PDF page to read (0-indexed, default 0)
    --zone-col N        Checklist table column index holding the zone code (default 1)
    --dim-col N         Checklist table column index holding the nominal dimension (default 2)
    --tol-col N         Checklist table column index to write the tolerance into (default 3)
    --overwrite         Overwrite tolerance cells that already have a value (default: skip them)
    --force-ocr         Force OCR even if the PDF appears to have a text layer
    --ocr-dpi N         Rasterization DPI used for OCR fallback (default 300)
    --report report.csv Also write the per-row report to a CSV file
"""
import argparse
import csv
import sys

from dimcheck.pipeline import process, print_report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("drawing_pdf")
    ap.add_argument("checklist_docx", help="Checklist .docx (or legacy .doc, converted automatically)")
    ap.add_argument("-o", "--output", required=True, help="Path to write the updated checklist docx")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--zone-col", type=int, default=1)
    ap.add_argument("--dim-col", type=int, default=2)
    ap.add_argument("--tol-col", type=int, default=3)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--force-ocr", action="store_true")
    ap.add_argument("--ocr-dpi", type=int, default=300)
    ap.add_argument("--report", default=None, help="Optional CSV path for the per-row review report")
    args = ap.parse_args()

    if args.output.lower().endswith(".doc"):
        args.output += "x"
        print(f"Writing {args.output} (.doc can't be written; Word opens .docx).", file=sys.stderr)

    reports = process(
        pdf_path=args.drawing_pdf,
        checklist_path=args.checklist_docx,
        output_path=args.output,
        page_number=args.page,
        zone_col=args.zone_col,
        dim_col=args.dim_col,
        tol_col=args.tol_col,
        overwrite_existing=args.overwrite,
        force_ocr=args.force_ocr,
        ocr_dpi=args.ocr_dpi,
    )

    print_report(reports)

    unresolved = [r for r in reports if r.source == "unresolved"]
    mismatched = [r for r in reports if r.verdict.startswith("MISMATCH")]
    if mismatched:
        print(f"\n{len(mismatched)} row(s) disagree with the drawing -- see MISMATCH above.", file=sys.stderr)
    if unresolved:
        print(f"{len(unresolved)} of {len(reports)} row(s) could not be resolved from the drawing.", file=sys.stderr)

    if args.report:
        with open(args.report, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Zone", "Dimension", "Checklist Tolerance", "Drawing Tolerance", "Verdict", "Source", "Confidence", "Notes"])
            for r in reports:
                w.writerow([r.zone, r.dimension, r.existing_tolerance, r.resolved_tolerance or "", r.verdict, r.source, r.confidence, " | ".join(r.notes)])
        print(f"\nReview report written to {args.report}")


if __name__ == "__main__":
    main()
