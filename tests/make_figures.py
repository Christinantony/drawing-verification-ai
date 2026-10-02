"""Render the README figures from a real run of the deterministic stages on
the synthetic drawing. No model is called.

    python tests/make_figures.py            # writes assets/*.png

Figure 1 (extraction_overlay.png): the synthetic drawing with the regions the
drawing-audit stage actually detected: the title-block corner, every bordered
field box it read, and the BOM table pdfplumber found.
Figure 2 (dimcheck_overlay.png): the zone grid dimcheck detected from the
border labels, the two checklist dimensions it located, and the general
tolerance table it parsed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("drawing-audit", "dimcheck", "tests"):
    sys.path.insert(0, str(ROOT / sub))

import pymupdf
from PIL import Image, ImageDraw, ImageFont

import config
import extraction
import structuring
from dimcheck.pdf_layout import load_page_layout, flat_text
from dimcheck.zoning import detect_grid
from dimcheck.general_tolerance import parse_general_tolerance_table
from dimcheck.dimension_matcher import find_dimension
from dimcheck.pipeline import resolve_tolerance

PDF = ROOT / "examples" / "synthetic_drawing.pdf"
OUT = ROOT / "assets"
OUT.mkdir(exist_ok=True)
SCALE = 2.0  # render at 144 dpi
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def render_page():
    doc = pymupdf.open(str(PDF))
    page = doc[0]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return doc, page, img


def label(d, xy, text, fill, size=13, anchor="la"):
    f = ImageFont.truetype(BOLD, size)
    x, y = xy
    bb = d.textbbox((x, y), text, font=f, anchor=anchor)
    d.rectangle([bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2], fill=fill)
    d.text((x, y), text, font=f, fill="white", anchor=anchor)


def legend(img, rows, title):
    f = ImageFont.truetype(FONT, 14)
    fb = ImageFont.truetype(BOLD, 15)
    h = 34 + 24 * len(rows)
    canvas = Image.new("RGB", (img.width, img.height + h), "white")
    canvas.paste(img, (0, h))
    d = ImageDraw.Draw(canvas)
    d.text((12, 8), title, font=fb, fill="#111")
    y = 34
    for colour, text in rows:
        d.rectangle([14, y + 4, 34, y + 18], outline=colour, width=3)
        d.text((44, y), text, font=f, fill="#222")
        y += 24
    return canvas


def figure_extraction():
    doc, page, img = render_page()
    d = ImageDraw.Draw(img)
    data = extraction.load_document(PDF)
    p = data.pages[0]
    structured = structuring.build_structured_document(data)

    # title-block search region (same arithmetic as extraction.py)
    w, h = page.rect.width, page.rect.height
    r = config.TITLE_BLOCK_REGION
    rx0, ry0 = w * (1 - r["width_frac"]), h * (1 - r["height_frac"])
    d.rectangle([rx0 * SCALE, ry0 * SCALE, w * SCALE, h * SCALE], outline="#f59e0b", width=3)
    label(d, (rx0 * SCALE + 6, ry0 * SCALE - 20), "title-block region (config.TITLE_BLOCK_REGION)", "#f59e0b")

    # every bordered cell read from the CAD rectangles
    for c in p.title_block_cells:
        x0, y0, x1, y1 = [v * SCALE for v in c.bbox]
        d.rectangle([x0, y0, x1, y1], outline="#2563eb", width=3)
    cx0 = min(c.bbox[0] for c in p.title_block_cells) * SCALE
    cy1 = max(c.bbox[3] for c in p.title_block_cells) * SCALE
    label(d, (cx0, cy1 + 8), f"{len(p.title_block_cells)} field boxes read cell by cell", "#2563eb")

    # BOM table found by pdfplumber: locate it from the ruled lines on the page
    import pdfplumber
    with pdfplumber.open(str(PDF)) as pl:
        tables = pl.pages[0].find_tables()
    for t in tables:
        if not any("ITEM" in (c or "") for row in t.extract() for c in row):
            continue  # the title block is a ruled table too; the BOM is picked by its headers
        x0, y0, x1, y1 = [v * SCALE for v in t.bbox]
        d.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline="#16a34a", width=3)
        label(d, (x0, y1 + 8), f"BOM table: {len(structured['bom'])} rows parsed", "#16a34a")

    # notes block
    for tok in page.get_text("words"):
        if tok[4] == "NOTES:":
            d.rectangle([tok[0] * SCALE - 6, tok[1] * SCALE - 6, tok[2] * SCALE + 300, tok[3] * SCALE + 62], outline="#9333ea", width=3)
            label(d, (tok[0] * SCALE - 6, tok[3] * SCALE + 72), "notes", "#9333ea")

    tb = structured["title_block"]
    rows = [
        ("#f59e0b", "Corner searched for the title block"),
        ("#2563eb", f"Bordered cells read individually → drawing_number={tb['drawing_number']}, title={tb['title']}, revision={tb['revision']}, material={tb['material']}, scale={tb['scale']}"),
        ("#16a34a", f"Ruled table parsed as the BOM ({len(structured['bom'])} rows) — text source: {p.source}, orientation: {p.orientation}"),
        ("#9333ea", "Notes block carried into the model input"),
    ]
    legend(img, rows, "drawing-audit — what the extraction and structuring stages detected on examples/synthetic_drawing.pdf").save(OUT / "extraction_overlay.png")
    doc.close()


def figure_dimcheck():
    doc, page, img = render_page()
    d = ImageDraw.Draw(img)
    layout = load_page_layout(str(PDF))
    grid = detect_grid(layout.tokens, layout.page_width, layout.page_height)
    general = parse_general_tolerance_table(flat_text(layout))

    # zone grid
    for x in grid.col_edges[1:-1]:
        d.line([x * SCALE, 0, x * SCALE, img.height], fill="#94a3b8", width=2)
    for y in grid.row_edges[1:-1]:
        d.line([0, y * SCALE, img.width, y * SCALE], fill="#94a3b8", width=2)
    f = ImageFont.truetype(BOLD, 22)
    for ci, cl in enumerate(grid.col_labels):
        for ri, rl in enumerate(grid.row_labels):
            cx = grid.col_edges[ci] * SCALE + 10
            cy = grid.row_edges[ri] * SCALE + 8
            d.text((cx, cy), f"{rl}{cl}", font=f, fill="#cbd5e1", anchor="la")

    # checklist rows located on the drawing
    results = []
    for zone, dim in [("C3", "25.0"), ("B2", "12")]:
        m = find_dimension(layout.tokens, grid, zone, dim)
        value, source, _ = resolve_tolerance(m, general)
        results.append((zone, dim, value, source, m.confidence))
        t = m.matched_token
        d.rectangle([t.x0 * SCALE - 8, t.top * SCALE - 8, t.x1 * SCALE + 8, t.bottom * SCALE + 8], outline="#dc2626", width=3)
        label(d, (t.x1 * SCALE + 14, t.top * SCALE - 6), f"checklist row {zone} / {dim}  →  {value} ({source}, {m.confidence})", "#dc2626")

    # general tolerance table
    toks = [t for t in layout.tokens if t.text in ("WITHOUT", "ANGLE")]
    x0 = min(t.x0 for t in toks) * SCALE - 8
    y0 = min(t.top for t in toks) * SCALE - 8
    y1 = max(t.bottom for t in toks) * SCALE + 8
    d.rectangle([x0, y0, x0 + 230, y1], outline="#16a34a", width=3)
    label(d, (x0, y1 + 8), f"general tolerances parsed: {general.without_decimal} / {general.one_decimal} / {general.two_decimal} / {general.angle}", "#16a34a")

    rows = [
        ("#94a3b8", f"Zone grid detected from the border labels: columns {grid.col_labels}, rows {grid.row_labels}"),
        ("#dc2626", "Checklist dimension found inside its zone cell; tolerance resolved from the general table because none is printed beside it"),
        ("#16a34a", "Title-block general tolerance note parsed by decimal-place tier"),
    ]
    legend(img, rows, "dimcheck — zone grid, dimension matches and general tolerances read from examples/synthetic_drawing.pdf").save(OUT / "dimcheck_overlay.png")
    doc.close()
    return results




def figure_coherence():
    """Figure 3 (coherence_rules.png): the cross-drawing rules applied to the
    hand-built records from tests/test_coherence.py. check_drawings() is the
    real function; the drawing set is the same one the test uses."""
    sys.path.insert(0, str(ROOT / "coherence"))
    from check_coherence import check_drawings
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

    parent = {"drawing_no": "ASM-1", "revision": "01", "title": "ASSEMBLY", "_file": "ASM-1.pdf",
              "bom": [{"description": "BRACKET", "ref_part_no": "PRT-9"}]}
    child = {"drawing_no": "PRT-1", "revision": "02", "title": "BRACKET", "_file": "PRT-1 sheet 1.pdf",
             "next_assy": "ASM-1", "bom": []}
    child_sheet2 = dict(child, revision="03", _file="PRT-1 sheet 2.pdf")
    issues = check_drawings([parent, child, child_sheet2])

    fig, ax = plt.subplots(figsize=(13, 6.4), dpi=120)
    ax.axis("off")
    boxes = {
        "ASM-1": (0.5, 4.2, "ASM-1  rev 01\nASSEMBLY\nBOM: BRACKET → PRT-9"),
        "PRT-1a": (0.5, 1.2, "PRT-1  rev 02  (sheet 1)\nBRACKET\nNEXT ASSY. ASM-1"),
        "PRT-1b": (4.6, 1.2, "PRT-1  rev 03  (sheet 2)\nBRACKET\nNEXT ASSY. ASM-1"),
    }
    for key, (x, y, text) in boxes.items():
        ax.add_patch(FancyBboxPatch((x, y), 3.6, 1.7, boxstyle="round,pad=0.1", fc="#eff6ff", ec="#1e3a8a", lw=1.2))
        ax.text(x + 0.2, y + 1.45, text, fontsize=9.5, va="top", family="monospace")
    ax.add_patch(FancyArrowPatch((2.3, 2.9), (2.3, 4.1), arrowstyle="-|>", mutation_scale=14, color="#1e3a8a"))
    ax.text(2.45, 3.5, "NEXT ASSY.", fontsize=8.5, color="#1e3a8a")
    ax.add_patch(FancyArrowPatch((6.4, 2.9), (4.2, 4.5), arrowstyle="-|>", mutation_scale=14, color="#1e3a8a"))
    ax.add_patch(FancyArrowPatch((4.1, 5.0), (8.6, 5.0), arrowstyle="-|>", mutation_scale=14, color="#dc2626", linestyle="--"))
    ax.text(4.3, 5.15, "BOM line references PRT-9", fontsize=8.5, color="#dc2626")
    ax.add_patch(FancyBboxPatch((8.7, 4.4), 2.6, 1.2, boxstyle="round,pad=0.1", fc="#fee2e2", ec="#dc2626", lw=1.2))
    ax.text(8.9, 5.3, "PRT-9\n(no drawing in the set)", fontsize=9.5, va="top", family="monospace")

    ax.text(0.5, 0.75, f"check_drawings() raised {len(issues)} issues from these three sheets:", fontsize=10.5, weight="bold", va="top")
    y = 0.35
    for i in issues:
        ax.text(0.7, y, f"• [{i.get('type')}]  {i.get('detail', i.get('message', ''))}", fontsize=8.8, va="top", family="monospace")
        y -= 0.42
    ax.set_xlim(0, 12)
    ax.set_ylim(y - 0.2, 6.3)
    ax.set_title("coherence — cross-file rules applied to the drawing set from tests/test_coherence.py (no model call)")
    fig.tight_layout()
    fig.savefig(OUT / "coherence_rules.png")
    return issues


if __name__ == "__main__":
    figure_extraction()
    print("dimcheck rows:", figure_dimcheck())
    print("coherence issues:", [i.get("type") for i in figure_coherence()])
    print("wrote", OUT / "extraction_overlay.png", OUT / "dimcheck_overlay.png")
