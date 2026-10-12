"""Render the README figures and example outputs from real runs on the
synthetic CAD-like drawing (tests/make_cad_like_drawing.py). No model is
called and no real drawing is used.

    python tests/make_figures.py      # writes assets/*.png and examples/*

assets/sheetreader_overlay.png  cells, title block, BOM, revision table and
                                tolerances the reader found
assets/sideways_scan.png        the image-only sideways sheet and what OCR read
assets/dimcheck_overlay.png     zone grid and checklist dimensions located
assets/dimcheck_console.png     verify-mode console output
assets/coherence_rules.png      cross-drawing rules on a small drawing set
assets/tests_passing.png        pytest summary
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("drawing-audit", "dimcheck", "coherence", "tests"):
    sys.path.insert(0, str(ROOT / sub))

import docx  # noqa: E402
import pymupdf  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from make_cad_like_drawing import make_cad_like_drawing, make_sideways_scan  # noqa: E402
from sheetreader import build_cells, find_tables, load_sheets, read_title_block  # noqa: E402

OUT = ROOT / "assets"
EX = ROOT / "examples"
OUT.mkdir(exist_ok=True)
EX.mkdir(exist_ok=True)
PDF = EX / "XY-100-01_REV.01.pdf"
SCAN = EX / "XY-100-01_REV.01_sideways_scan.pdf"
SCALE = 2.0
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def render(pdf, scale=SCALE):
    doc = pymupdf.open(str(pdf))
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    return img


def label(d, xy, text, fill, size=12):
    f = ImageFont.truetype(BOLD, size)
    bb = d.textbbox(xy, text, font=f)
    d.rectangle([bb[0] - 3, bb[1] - 2, bb[2] + 3, bb[3] + 2], fill=fill)
    d.text(xy, text, font=f, fill="white")


def legend(img, rows, title):
    f = ImageFont.truetype(FONT, 14)
    fb = ImageFont.truetype(BOLD, 15)
    h = 36 + 24 * len(rows)
    canvas = Image.new("RGB", (img.width, img.height + h), "white")
    canvas.paste(img, (0, h))
    d = ImageDraw.Draw(canvas)
    d.text((12, 8), title, font=fb, fill="#111")
    y = 36
    for colour, text in rows:
        d.rectangle([14, y + 4, 34, y + 18], outline=colour, width=3)
        d.text((44, y), text, font=f, fill="#222")
        y += 24
    return canvas


def box(d, b, colour, width=3, pad=0):
    d.rectangle([b[0] * SCALE - pad, b[1] * SCALE - pad, b[2] * SCALE + pad, b[3] * SCALE + pad],
                outline=colour, width=width)


def figure_sheetreader():
    from dimcheck.dimension_matcher import read_tolerance
    sheet = load_sheets(PDF)[0]
    cells = build_cells(sheet)
    tb = read_title_block(sheet, cells)
    bom = find_tables(cells, "bom", sheet)[0]
    rev = find_tables(cells, "revision", sheet)[0]
    img = render(PDF)
    d = ImageDraw.Draw(img)
    for c in tb.cells:
        box(d, c.bbox, "#2563eb", 2)
    box(d, tb.region, "#f59e0b", 3, pad=4)
    label(d, (tb.region[0] * SCALE, tb.region[1] * SCALE - 22), "title block: cells joined to DRAWING No.", "#f59e0b")
    for c in cells:
        if bom.bbox[0] - 2 <= c.xc <= bom.bbox[2] + 2 and bom.bbox[1] - 60 <= c.yc <= bom.bbox[3]:
            box(d, c.bbox, "#16a34a", 2)
    label(d, (bom.bbox[0] * SCALE, (bom.bbox[1] - 52) * SCALE), f"BOM, header at bottom: {len(bom.rows)} rows", "#16a34a")
    for c in rev.header_cells.values():
        box(d, c.bbox, "#9333ea", 2)
    label(d, (rev.bbox[0] * SCALE, rev.bbox[3] * SCALE + 30), f"revision table: REV {rev.rows[0]['rev']}", "#9333ea")
    for w in sheet.words:
        if w.text in ("50.0", "30.5", "40.0"):
            tol, _, _ = read_tolerance(sheet.words, w)
            box(d, w.bbox, "#dc2626", 3, pad=4)
            label(d, (w.x1 * SCALE + 30, w.y0 * SCALE - 24), f"{w.text} -> {tol.format()}", "#dc2626")
    f = tb.fields
    rows = [
        ("#2563eb", f"Ruled cells built from loose line segments (no rectangle objects): "
                    f"{f['drawing_number']} | {f['title']} | REV {f['revision']} | {f['date']} | SCALE {f['scale']}"),
        ("#f59e0b", "Title block = cells connected to the DRAWING No. cell (free labels like 'DETAIL A / SCALE' are not)"),
        ("#16a34a", "BOM read upward from a header at the bottom; rows anchored on the item number"),
        ("#9333ea", "Revision table read downward from its header"),
        ("#dc2626", "Tolerances read in the text's own frame: stacked limits, a '±' drawn as a graphic, vertical text"),
    ]
    legend(img, rows, "sheetreader on a SolidWorks-style export (examples/XY-100-01_REV.01.pdf)").save(OUT / "sheetreader_overlay.png")


def figure_sideways_scan():
    from sheetreader.record import read_drawing
    rec = read_drawing(SCAN)[0]
    left = render(SCAN, 0.9)
    tb = rec.title_block
    lines = [f"Text layer: none ({rec.text_source.upper()})",
             f"Turned upright by: {rec.rotation_deg} deg clockwise",
             f"Sheet: {rec.paper_size} {rec.orientation}", ""]
    lines += [f"{k:15} {v}" for k, v in tb.items() if v]
    lines += ["", "BOM:"] + [f"  {r.get('item', '?'):>2}  {r.get('description', ''):18} {r.get('qty', ''):5} {r.get('ref', '')}"
                             for r in rec.bom]
    f = ImageFont.truetype(MONO, 15)
    w = 640
    h = max(left.height, 30 + 20 * len(lines))
    canvas = Image.new("RGB", (left.width + w + 30, h + 50), "white")
    canvas.paste(left, (10, 50))
    d = ImageDraw.Draw(canvas)
    d.text((10, 12), "Image-only PDF, sheet printed sideways on a portrait page (like AutoCAD via Microsoft Print to PDF)",
           font=ImageFont.truetype(BOLD, 15), fill="#111")
    y = 60
    for ln in lines:
        d.text((left.width + 30, y), ln, font=f, fill="#111")
        y += 20
    canvas.save(OUT / "sideways_scan.png")


def figure_dimcheck():
    from dimcheck.dimension_matcher import find_dimension
    from dimcheck.pdf_layout import load_page_layout
    from dimcheck.zoning import detect_grid
    layout = load_page_layout(str(PDF))
    grid = detect_grid(layout.tokens, layout.page_width, layout.page_height)
    img = render(PDF)
    d = ImageDraw.Draw(img)
    for x in grid.col_edges[1:-1]:
        d.line([x * SCALE, 0, x * SCALE, img.height], fill="#94a3b8", width=2)
    for y in grid.row_edges[1:-1]:
        d.line([0, y * SCALE, img.width, y * SCALE], fill="#94a3b8", width=2)
    fz = ImageFont.truetype(BOLD, 22)
    for ci, cl in enumerate(grid.col_labels):
        for ri, rl in enumerate(grid.row_labels):
            d.text((grid.col_edges[ci] * SCALE + 10, grid.row_edges[ri] * SCALE + 8), f"{rl}{cl}", font=fz, fill="#cbd5e1")
    for zone, dim in (("C4", "50"), ("C3", "Ø30.5"), ("B4", "40.0"), ("B3", "12")):
        m = find_dimension(layout.tokens, grid, zone, dim)
        t = m.matched_token
        box(d, t.bbox, "#dc2626", 3, pad=6)
        tol = m.tolerance.format() if m.tolerance else "general table"
        label(d, (t.x1 * SCALE + 18, t.y0 * SCALE - 8), f"checklist {zone} '{dim}' -> {t.text}  {tol}", "#dc2626")
    rows = [("#94a3b8", f"Zone grid from the upright border labels: columns {grid.col_labels}, rows {grid.row_labels}"),
            ("#dc2626", "Checklist value matched by value ('50' finds '50.0', 'Ø30.5' finds '30.5'), tolerance read beside it")]
    legend(img, rows, "dimcheck on examples/XY-100-01_REV.01.pdf").save(OUT / "dimcheck_overlay.png")


def text_image(text, path, title):
    f = ImageFont.truetype(MONO, 14)
    fb = ImageFont.truetype(BOLD, 15)
    lines = text.rstrip().splitlines()
    w = max(900, 9 * max(len(l) for l in lines) + 40)
    img = Image.new("RGB", (w, 50 + 19 * len(lines)), "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((16, 12), title, font=fb, fill="#e2e8f0")
    for i, ln in enumerate(lines):
        col = "#fca5a5" if "MISMATCH" in ln or "FAIL" in ln or "failed" in ln else \
              "#86efac" if (" match " in ln or "passed" in ln) else "#e2e8f0"
        d.text((16, 42 + 19 * i), ln, font=f, fill=col)
    img.save(path)


def figure_dimcheck_console(tmp: Path):
    chk = tmp / "checklist.docx"
    document = docx.Document()
    rows = [("C4", "50", "0/-0.2"), ("C3", "Ø30.5", "±0.2"), ("B4", "40.0", "0/-0.1"), ("B3", "12", "")]
    table = document.add_table(rows=len(rows) + 1, cols=4)
    for c, h in zip(table.rows[0].cells, ("Sl No", "ZONE", "DIMENSIONS", "TOLERANCE")):
        c.text = h
    for i, (z, dim, tol) in enumerate(rows, 1):
        cells = table.rows[i].cells
        cells[0].text, cells[1].text, cells[2].text, cells[3].text = str(i), z, dim, tol
    document.save(str(chk))
    res = subprocess.run([sys.executable, str(ROOT / "dimcheck" / "check_tolerances.py"), str(PDF), str(chk),
                          "-o", str(tmp / "out.docx")], capture_output=True, text=True, cwd=ROOT / "dimcheck")
    out = "\n".join(l for l in (res.stdout + res.stderr).splitlines() if "deprecated" not in l)
    text_image("$ python check_tolerances.py XY-100-01_REV.01.pdf checklist.docx -o out.docx\n" + out,
               OUT / "dimcheck_console.png", "dimcheck: verify a filled checklist, fill the empty row")


def figure_coherence():
    from check_coherence import check_drawings
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    parent = {"drawing_no": "ASM-1", "revision": "01", "title": "ASSEMBLY", "_file": "ASM-1.pdf",
              "bom": [{"description": "BRACKET", "ref_part_no": "PRT-9"}]}
    child = {"drawing_no": "PRT-1", "revision": "02", "title": "BRACKET", "_file": "PRT-1 sheet 1.pdf",
             "next_assy": "ASM-1", "bom": []}
    child_sheet2 = dict(child, revision="03", _file="PRT-1 sheet 2.pdf")
    issues = check_drawings([parent, child, child_sheet2])

    fig, ax = plt.subplots(figsize=(13, 6.4), dpi=120)
    ax.axis("off")
    boxes = {"ASM-1": (0.5, 4.2, "ASM-1  rev 01\nASSEMBLY\nBOM: BRACKET → PRT-9"),
             "PRT-1a": (0.5, 1.2, "PRT-1  rev 02  (sheet 1)\nBRACKET\nNEXT ASSY. ASM-1"),
             "PRT-1b": (4.6, 1.2, "PRT-1  rev 03  (sheet 2)\nBRACKET\nNEXT ASSY. ASM-1")}
    for _, (x, y, text) in boxes.items():
        ax.add_patch(FancyBboxPatch((x, y), 3.6, 1.7, boxstyle="round,pad=0.1", fc="#eff6ff", ec="#1e3a8a", lw=1.2))
        ax.text(x + 0.2, y + 1.45, text, fontsize=9.5, va="top", family="monospace")
    ax.add_patch(FancyArrowPatch((2.3, 2.9), (2.3, 4.1), arrowstyle="-|>", mutation_scale=14, color="#1e3a8a"))
    ax.text(2.45, 3.5, "NEXT ASSY.", fontsize=8.5, color="#1e3a8a")
    ax.add_patch(FancyArrowPatch((6.4, 2.9), (4.2, 4.5), arrowstyle="-|>", mutation_scale=14, color="#1e3a8a"))
    ax.add_patch(FancyArrowPatch((4.1, 5.0), (8.6, 5.0), arrowstyle="-|>", mutation_scale=14, color="#dc2626", linestyle="--"))
    ax.text(4.3, 5.15, "BOM line references PRT-9", fontsize=8.5, color="#dc2626")
    ax.add_patch(FancyBboxPatch((8.7, 4.4), 2.6, 1.2, boxstyle="round,pad=0.1", fc="#fee2e2", ec="#dc2626", lw=1.2))
    ax.text(8.9, 5.3, "PRT-9\n(no drawing in the set)", fontsize=9.5, va="top", family="monospace")
    ax.text(0.5, 0.75, f"check_drawings() raised {len(issues)} issues from these three sheets:", fontsize=10.5,
            weight="bold", va="top")
    y = 0.35
    for i in issues:
        detail = i.get("detail")
        detail = ", ".join(sorted(detail)) if isinstance(detail, set) else detail
        ax.text(0.7, y, f"• [{i['severity'].upper()}] {i['type']}: {detail}"[:150], fontsize=8.4, va="top",
                family="monospace")
        y -= 0.42
    ax.set_xlim(0, 12)
    ax.set_ylim(y - 0.2, 6.3)
    ax.set_title("coherence: cross-file rules on the drawing set from tests/test_coherence.py (no model call)")
    fig.tight_layout()
    fig.savefig(OUT / "coherence_rules.png")


def figure_tests():
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rN"],
                         capture_output=True, text=True, cwd=ROOT)
    lines = [l for l in res.stdout.splitlines() if l.strip()]
    res2 = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                          capture_output=True, text=True, cwd=ROOT)
    files = {}
    for l in res2.stdout.splitlines():
        if "::" in l:
            files[l.split("::")[0]] = files.get(l.split("::")[0], 0) + 1
    body = "$ python -m pytest -q\n" + "\n".join(lines[-2:]) + "\n\n" + \
           "\n".join(f"  {k:32} {v:3d} tests" for k, v in sorted(files.items()))
    text_image(body, OUT / "tests_passing.png", "Test suite (no model, no real drawings)")


def example_report():
    import main as audit_main
    audit_main.main([str(PDF), "-o", str(EX / "XY-100-01_REV.01_report.md"), "--no-model"])
    audit_main.main([str(SCAN), "-o", str(EX / "XY-100-01_REV.01_sideways_scan_report.md"), "--no-model"])


if __name__ == "__main__":
    import tempfile
    make_cad_like_drawing(str(PDF))
    make_sideways_scan(str(PDF), str(SCAN), dpi=200)
    with tempfile.TemporaryDirectory() as t:
        figure_sheetreader()
        figure_sideways_scan()
        figure_dimcheck()
        figure_dimcheck_console(Path(t))
        figure_coherence()
        example_report()
        figure_tests()
    print("wrote", sorted(p.name for p in OUT.iterdir()), sorted(p.name for p in EX.iterdir()))
