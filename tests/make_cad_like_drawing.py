"""Synthetic drawings that reproduce the layouts real CAD exports produce,
without using any real (confidential) drawing:

* make_cad_like_drawing(): a text-layer PDF like a SolidWorks export -
  title block and BOM drawn from loose line segments (no rectangle objects),
  BOM header at the bottom with items numbered upward, a revision table,
  stacked limits whose signs are separate words, a "±" left out of the text
  (as SolidWorks draws it as a graphic), a vertical dimension, a template
  form number in the margin. Contains deliberate errors for the checks to
  find: BRAKCET, SILICON SEALANT, a zero quantity, SCALE 10:1 on a part
  too big for it, and a checklist-worthy set of tolerances.

* make_sideways_scan(): the same sheet rasterised, turned 90 degrees and
  saved as an image-only PDF on a portrait page - what AutoCAD plotted
  through "Microsoft Print to PDF" looks like to a reader (no text layer).
"""
import pymupdf

W, H = 842, 595  # A4 landscape


def _hline(page, x0, x1, y):
    page.draw_line((x0, y), (x1, y), width=0.6)


def _vline(page, x, y0, y1):
    page.draw_line((x, y0), (x, y1), width=0.6)


def _t(page, x, y, s, size=7, rotate=0):
    page.insert_text((x, y), s, fontsize=size, rotate=rotate)


def make_cad_like_drawing(path, title="SUPPORT BRAKCET", scale="10:1", rev="01",
                          drawing_no="XY-100-01", qty3="0"):
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)

    # Frame and zone labels (columns 4..1 left to right, rows C..A top to bottom)
    for x0, x1, y in ((20, 822, 25), (20, 822, 570)):
        _hline(page, x0, x1, y)
    _vline(page, 20, 25, 570)
    _vline(page, 822, 25, 570)
    for i, x in enumerate([115, 315, 515, 715]):
        _t(page, x, 18, str(4 - i), 9)
        _t(page, x, 584, str(4 - i), 9)
    for j, y in enumerate([120, 300, 480]):
        _t(page, 8, y, "CBA"[j], 9)
        _t(page, 828, y, "CBA"[j], 9)
    _t(page, 600, 18, "XY-F-DRG-01 REV.00", 6)

    # Dimensions
    _t(page, 100, 110, "50.0", 9)           # stacked: 0.0 over -0.2  (zone C4)
    _t(page, 120, 104, "0.0", 7)
    _t(page, 117, 110, "-", 7)
    _t(page, 120, 110, "0.2", 7)
    _t(page, 330, 110, "30.5", 9)           # symmetric, "±" drawn as a graphic (zone C3)
    _t(page, 350, 110, "0.1", 9)
    _t(page, 330, 300, "12", 9)             # general table, no decimals (zone B3)
    # Vertical 40.0 reading bottom-to-top, with stacked 0.0 / -0.1 (zone B4)
    _t(page, 120, 320, "40.0", 9, rotate=90)
    _t(page, 113, 300, "0.0", 7, rotate=90)
    _t(page, 119, 303, "-", 7, rotate=90)
    _t(page, 119, 300, "0.1", 7, rotate=90)
    _t(page, 160, 230, "180.0", 9)          # the biggest dimension (scale check)

    # Notes
    _t(page, 40, 380, "NOTE:", 8)
    _t(page, 50, 392, "BREAK ALL SHARP EDGES.", 7)
    _t(page, 50, 402, "APPLY SILICON SEALANT ON MATING FACES.", 7)

    # Revision table, top-right: header at top
    rx = [520, 560, 600, 822]
    _hline(page, 520, 822, 25)
    _hline(page, 520, 822, 37)
    _hline(page, 520, 822, 49)
    for x in rx:
        _vline(page, x, 25, 49)
    _t(page, 523, 34, "REV NO.")
    _t(page, 566, 34, "ZONE")
    _t(page, 680, 34, "REVISION DETAILS")
    _t(page, 530, 46, rev)
    _t(page, 570, 46, "B2")
    _t(page, 605, 46, "HOLE ADDED")

    # BOM above the title block: header at the bottom, items numbered upward
    bx = [520, 550, 680, 720, 822]
    rows = [("1", "BASE PLATE", "1 NO", "XY-100-02"),
            ("2", "SILICON SEALANT", "A/R", "RTV 108"),
            ("3", "M3x8 CSK SCREW", qty3, "SS 304")]
    hdr_y1 = 440
    rh = 12
    top = hdr_y1 - rh * (len(rows) + 1)
    for k in range(len(rows) + 2):
        _hline(page, 520, 822, top + k * rh)
    for x in bx:
        _vline(page, x, top, hdr_y1)
    for k, (it, desc, q, ref) in enumerate(rows):
        y = hdr_y1 - rh * (k + 2) + 9
        for x, s in zip(bx, (it, desc, q, ref)):
            _t(page, x + 3, y, s)
    for x, s in zip(bx, ("SL NO", "DESCRIPTION", "QTY.", "REF./MATERIAL")):
        _t(page, x + 3, hdr_y1 - 3, s)

    # Title block, bottom-right, all loose lines
    x0, x1 = 420, 822
    for y in (450, 470, 490, 505, 545, 570):
        _hline(page, x0, x1, y)
    # row 1: tolerances | material | finish
    _vline(page, x0, 450, 570)
    _vline(page, 520, 450, 570)
    _vline(page, 700, 450, 470)
    _t(page, 423, 458, "TOLERANCES UNLESS", 6)
    _t(page, 423, 465, "OTHERWISE SPECIFIED:", 6)
    _t(page, 423, 475, "WITHOUT DECIMAL : ±1", 6)
    _t(page, 423, 482, "ONE DECIMAL : ±0.1", 6)
    _t(page, 423, 489, "TWO DECIMAL : ±0.05", 6)
    _t(page, 423, 496, "ANGLE : ±½°", 6)
    _t(page, 523, 462, "MATERIAL: AL 6061-T6")
    _t(page, 703, 462, "FINISH: ANODIZED")
    # row 2: people, weight, scale
    for x in (580, 640, 700, 760):
        _vline(page, x, 470, 490)
    for x, lab, val in ((523, "DRAWN", "A.B"), (583, "CHECKED", "C.D"), (643, "APPROVED", "E.F"),
                        (703, "WEIGHT", "120±5 g"), (763, "SCALE", scale)):
        _t(page, x, 477, lab, 6)
        _t(page, x + 4, 487, val)
    # row 3: title
    _t(page, 523, 500, "TITLE :", 6)
    _t(page, 600, 500, title, 8)
    # row 4: drawing no | rev/date
    _vline(page, 760, 505, 545)
    _hline(page, 760, 822, 525)  # splits REV / date only
    _t(page, 423, 518, "COMPANY LOGO", 8)
    _t(page, 523, 518, "DRAWING No. :", 6)
    _t(page, 600, 520, drawing_no, 9)
    _t(page, 765, 518, f"REV.{rev}")
    _t(page, 765, 538, "05/03/2026")
    # row 5: next assy | size | sheet
    _vline(page, 700, 545, 570)
    _vline(page, 760, 545, 570)
    _t(page, 523, 560, "NEXT ASSY. : XY-100-00")
    _t(page, 703, 560, "SIZE : A4")
    _t(page, 763, 560, "SHEET:1/1")

    doc.save(path)
    doc.close()


def make_sideways_scan(src_pdf, out_pdf, dpi=200):
    """Rasterise src_pdf, turn it 90 degrees and wrap it in an image-only PDF
    on a portrait page (no text layer), like a sideways AutoCAD plot."""
    src = pymupdf.open(src_pdf)
    pix = src[0].get_pixmap(dpi=dpi)
    src.close()
    from PIL import Image
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).rotate(90, expand=True)
    img = img.convert("L").point(lambda v: 255 if v > 160 else 0).convert("1")  # 1-bit, like a plot
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=H, height=W)  # portrait
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(out_pdf)
    doc.close()


if __name__ == "__main__":
    make_cad_like_drawing("cad_like.pdf")
    make_sideways_scan("cad_like.pdf", "cad_like_sideways.pdf")
