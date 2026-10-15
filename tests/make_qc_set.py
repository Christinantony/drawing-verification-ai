"""A small synthetic drawing set with planted mistakes for the QC agent
(no real drawings). Numbering 'AB-CD 10-E-nn', like a real company set.

Planted in the assembly AB-CD 10-E-01:
  * BOM item 2 refers to 'AB-CD 10-E-3'       (leading zero missing; drawing is E-03)
  * note 2 'USE FILE AB-CD 10-01.STEP'          (no 'E-': normal for model files)
  * note 3 refers to ITEM NO. 9                 (BOM has items 1-4)
  * note 4 'SEE TABLE 2'                        (no table 2 on the sheet)
  * notes numbered 1, 2, 3, 5                   (gap)
  * BOM item 4 'ANEROBIC ADHESIVE'              (known misspelling)
  * FINISH 'CLEAR CHROMATISATION', child E-02 'CLEAR CHROMATIZATION'
  * balloons for items 1, 2, 3 only; balloon 7 not in BOM
  * jig number AB-JIG-CD 10-05 in a note
"""
import pymupdf

W, H = 842, 595


def _h(p, x0, x1, y):
    p.draw_line((x0, y), (x1, y), width=0.5)


def _v(p, x, y0, y1):
    p.draw_line((x, y0), (x, y1), width=0.5)


def _t(p, x, y, s, size=7):
    p.insert_text((x, y), s, fontsize=size)


def _title_block(p, number, title, material, finish, weight="12±1 g", next_assy="", scale="1:1"):
    x0, x1 = 480, 822
    for y in (470, 490, 510, 545, 570):
        _h(p, x0, x1, y)
    _v(p, x0, 470, 570)
    _v(p, 560, 470, 570)
    _t(p, 483, 478, "TOLERANCES UNLESS", 5)
    _t(p, 483, 486, "WITHOUT DECIMAL : ±1", 5)
    _t(p, 483, 494, "ONE DECIMAL : ±0.1", 5)
    _t(p, 483, 502, "TWO DECIMAL : ±0.05", 5)
    _v(p, 690, 470, 490)
    _t(p, 563, 480, f"MATERIAL: {material}")
    _t(p, 693, 480, f"FINISH: {finish}", 6)
    for x in (610, 660, 710, 770):
        _v(p, x, 490, 510)
    for x, lab, val in ((563, "DRAWN", "A.B"), (613, "CHECKED", "C.D"), (663, "APPROVED", "E.F"),
                        (713, "WEIGHT", weight), (773, "SCALE", scale)):
        _t(p, x, 497, lab, 6)
        _t(p, x + 3, 507, val)
    _t(p, 563, 520, "TITLE :", 6)
    _t(p, 620, 522, title, 8)
    _v(p, 760, 510, 570)
    _t(p, 563, 535, "DRAWING No. :", 6)
    _t(p, 630, 537, number, 9)
    _t(p, 765, 530, "REV.00")
    _t(p, 765, 540, "01/07/2026")
    _t(p, 563, 560, f"NEXT ASSY. : {next_assy}")
    _t(p, 700, 560, "SIZE : A4")
    _t(p, 765, 560, "SHEET:1/1")


def _frame(p):
    _h(p, 20, 822, 25)
    _h(p, 20, 822, 570)
    _v(p, 20, 25, 570)
    _v(p, 822, 25, 570)


def _balloon(p, x, y, n):
    p.draw_circle((x, y), 7.5, width=0.5)
    _t(p, x - (2 if len(n) == 1 else 4), y + 2.5, n, 7)


def make_assembly(path):
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    _frame(p)
    _t(p, 300, 18, "AB-F-DRG-01 REV.00", 6)
    for (x, y, n) in ((200, 150, "1"), (260, 150, "2"), (320, 150, "3"), (380, 150, "7")):
        _balloon(p, x, y, n)
    _t(p, 300, 250, "120.0 ±0.1", 9)
    _t(p, 300, 300, "NOTE:", 8)
    for i, (num, txt) in enumerate((("1.", "NORMAL SOLDERING TO BE DONE BEFORE HIGH TEMPERATURE SOLDERING"),
                                    ("2.", "USE FILE AB-CD 10-01.STEP FOR MACHINING"),
                                    ("3.", "APPLY ITEM NO. 9 ON MATING FACES"),
                                    ("5.", "WEIGHT OF EACH PART SEE TABLE 2"),
                                    ("6.", "USE JIG AB-JIG-CD 10-05 FOR ASSEMBLY"))):
        _t(p, 305, 312 + 10 * i, f"{num}  {txt}", 7)
    cols = [40, 60, 160, 200, 340]
    rows = [("1", "BASE PLATE", "1 NO", "AB-CD 10-E-02"), ("2", "CLAMP", "1 NO", "AB-CD 10-E-3"),
            ("3", "SS FASTENERS", "4 NOS", "M3X8 CSK SCREW"), ("4", "ANEROBIC ADHESIVE", "A/R", "LOCTITE 263")]
    rh, hdr_top, hdr_bot = 12.0, 545.0, 560.0
    top = hdr_top - rh * len(rows)
    ys = [top + k * rh for k in range(len(rows) + 1)] + [hdr_bot]
    for y in ys:
        _h(p, cols[0], cols[-1], y)
    for x in cols:
        _v(p, x, top, hdr_bot)
    for k, row in enumerate(reversed(rows)):
        for x, s in zip(cols, row):
            _t(p, x + 2, top + k * rh + 9, s, 6)
    for x, s in zip(cols, ("SL NO", "DESCRIPTION", "QTY.", "REF./MATERIAL")):
        _t(p, x + 2, hdr_top + 10, s, 6)
    _title_block(p, "AB-CD 10-E-01", "FINAL ASSEMBLY", "SEE TABLE", "CLEAR CHROMATISATION (ASTM B449)")
    doc.save(path)
    doc.close()


def make_part(path, number, title, finish="CLEAR CHROMATIZATION (ASTM B449)", next_assy="AB-CD 10-E-01",
              file_note=None, weight="12±1 g", extra=()):
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    _frame(p)
    _t(p, 300, 18, "AB-F-DRG-01 REV.00", 6)
    _t(p, 300, 250, "60.0 ±0.1", 9)
    _t(p, 300, 300, "NOTE:", 8)
    _t(p, 305, 312, f"1.  USE FILE {file_note or number}.STEP FOR MACHINING", 7)
    _t(p, 305, 322, "2.  BREAK ALL SHARP EDGES", 7)
    for k, line in enumerate(extra):
        _t(p, 305, 332 + 10 * k, line, 7)
    _title_block(p, number, title, "AL 6061-T6", finish, weight=weight, next_assy=next_assy, scale="1:1")
    doc.save(path)
    doc.close()


def make_set(folder):
    make_assembly(str(folder / "AB-CD 10-E-01 REV.00.pdf"))
    make_part(str(folder / "AB-CD 10-E-02 REV.00.pdf"), "AB-CD 10-E-02", "BASE PLATE")
    make_part(str(folder / "AB-CD 10-E-03 REV.00.pdf"), "AB-CD 10-E-03", "CLAMP")
