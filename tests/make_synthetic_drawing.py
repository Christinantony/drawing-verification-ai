"""Build a small synthetic vector drawing PDF for tests.

Not a real drawing: a landscape sheet with a ruled BOM table, a notes block,
and a bottom-right title block drawn as bordered cells - enough to exercise
the extraction and structuring stages without any proprietary files.
"""
import pymupdf


def make_drawing(path, title="MOUNTING BRAKCET", rev="B"):
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)  # A4 landscape, points

    # Border zone labels (for dimcheck-style grid tests)
    for i, x in enumerate([105, 315, 525, 735]):
        page.insert_text((x, 20), str(4 - i), fontsize=9)
        page.insert_text((x, 588), str(4 - i), fontsize=9)
    for j, y in enumerate([110, 300, 490]):
        page.insert_text((8, y), "CBA"[j], fontsize=9)
        page.insert_text((828, y), "CBA"[j], fontsize=9)

    # Dimensions + general tolerance block
    page.insert_text((300, 120), "25.0", fontsize=9)   # zone C3
    page.insert_text((540, 300), "12", fontsize=9)     # zone B2
    page.insert_text((60, 470), "WITHOUT DECIMAL ±1", fontsize=7)
    page.insert_text((60, 480), "ONE DECIMAL ±0.1", fontsize=7)
    page.insert_text((60, 490), "TWO DECIMAL ±0.05", fontsize=7)
    page.insert_text((60, 500), "ANGLE ±1/2°", fontsize=7)

    # Notes
    page.insert_text((60, 200), "NOTES:", fontsize=9)
    page.insert_text((60, 212), "1. BREAK ALL SHARP EDGES.", fontsize=8)
    page.insert_text((60, 224), "2. ANODIZE PER REV A OF SPEC.", fontsize=8)

    # Ruled BOM table
    x0, y0, cols, rh = 420, 60, [0, 40, 140, 300, 340], 18
    rows = [["ITEM", "PART NO", "DESCRIPTION", "QTY"],
            ["1", "AM-10234", "MOUNTING BRACKET", "1"],
            ["2", "HW-0042", "M4 SCREW", "0"]]
    for r in range(len(rows) + 1):
        page.draw_line((x0, y0 + r * rh), (x0 + cols[-1], y0 + r * rh))
    for c in cols:
        page.draw_line((x0 + c, y0), (x0 + c, y0 + len(rows) * rh))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            page.insert_text((x0 + cols[c] + 3, y0 + r * rh + 13), val, fontsize=8)

    # Title block: label box + value box per field, bottom-right
    fields = [("DWG NO", "AM-10234"), ("TITLE", title), ("REV", rev),
              ("MATERIAL", "AL 6061-T6"), ("SCALE", "1:2")]
    tx, ty, lw, vw, h = 560, 470, 70, 150, 20
    for i, (label, value) in enumerate(fields):
        y = ty + i * h
        page.draw_rect(pymupdf.Rect(tx, y, tx + lw, y + h))
        page.draw_rect(pymupdf.Rect(tx + lw, y, tx + lw + vw, y + h))
        page.insert_text((tx + 3, y + 14), label, fontsize=8)
        page.insert_text((tx + lw + 3, y + 14), value, fontsize=8)

    doc.save(path)
    doc.close()


if __name__ == "__main__":
    import sys
    make_drawing(sys.argv[1] if len(sys.argv) > 1 else "synthetic_drawing.pdf")
