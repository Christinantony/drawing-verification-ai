"""Regressions found on a real 17-sheet drawing set (kept out of the repo).
Every drawing here is synthetic; only the layout traits are reproduced."""
import json

import pymupdf

from check_coherence import check_drawings
from sheetreader.record import read_drawing

W, H = 842, 595


def _h(p, x0, x1, y, w=0.5):
    p.draw_line((x0, y), (x1, y), width=w)


def _v(p, x, y0, y1, w=0.5):
    p.draw_line((x, y0), (x, y1), width=w)


def _t(p, x, y, s, size=7):
    p.insert_text((x, y), s, fontsize=size)


def make_tight_bom_sheet(path):
    """BOM rows only 10.5 pt (3.7 mm) tall, every column line drawn one row at
    a time; a 'DETAIL A / SCALE 5 : 1' view frame touching both BOM and title
    block; a note with a product code ('AW 4510') and a weight ('86 g')."""
    doc = pymupdf.open()
    p = doc.new_page(width=W, height=H)
    _h(p, 20, 822, 25); _h(p, 20, 822, 570); _v(p, 20, 25, 570); _v(p, 822, 25, 570)
    _t(p, 300, 18, "XY-F-DRG-01 REV.00", 6)
    _t(p, 300, 200, "393 ±0.5", 9)
    _t(p, 300, 260, "158.75 ±0.1", 9)
    _t(p, 300, 330, "NOTE:", 8)
    _t(p, 310, 342, "USE ARALDITE AW 4510 + HW 4511-1 FOR BONDING", 7)
    _t(p, 310, 352, "PREFORM : 86 g", 7)

    cols = [40, 60, 140, 175, 340]
    rows = [("1", "BASE PLATE", "1 NO", "XY-200-02"), ("2", "CLAMP-B", "1 NO", "XY-200-3B"),
            ("3", "PCB-B", "1 NO", "XY-200-04B"), ("4", "EPOXY ADHESIVE", "A/R", "ARALDITE 2011"),
            ("5", "SEALANT", "A/R", "ANABOND 666")]
    rh, hdr_top, hdr_bot = 10.5, 545.0, 560.0
    top = hdr_top - rh * len(rows)
    ys = [top + k * rh for k in range(len(rows) + 1)]
    for y in ys + [hdr_bot]:
        for a, b in zip(cols, cols[1:]):
            _h(p, a, b, y)
    for a, b in zip(ys + [hdr_top], (ys + [hdr_top, hdr_bot])[1:]):
        for x in cols:
            _v(p, x, a, b)          # one short piece per row, like the CAD export
    for k, row in enumerate(reversed(rows)):
        y = top + k * rh + 8
        for x, s in zip(cols, row):
            _t(p, x + 2, y, s, 6)
    for x, s in zip(cols, ("SL NO", "DESCRIPTION", "QTY.", "REF./MATERIAL")):
        _t(p, x + 2, hdr_top + 10, s, 6)

    # View frame between the BOM and the title block, touching both.
    _t(p, 360, 552, "DETAIL A", 7)
    _t(p, 360, 562, "SCALE 5 : 1", 7)
    _h(p, 340, 480, 570)

    x0, x1 = 480, 822
    for y in (470, 490, 510, 545, 570):
        _h(p, x0, x1, y)
    _v(p, x0, 470, 570)
    _v(p, 560, 470, 570)
    _t(p, 483, 478, "TOLERANCES UNLESS", 5)
    _t(p, 483, 486, "WITHOUT DECIMAL : ±1", 5)
    _t(p, 483, 494, "ONE DECIMAL : ±0.1", 5)
    _t(p, 483, 502, "TWO DECIMAL : ±0.05", 5)
    _t(p, 563, 480, "MATERIAL: SEE TABLE")
    for x in (620, 680, 740):
        _v(p, x, 490, 510)
    for x, lab, val in ((563, "DRAWN", "A.B"), (623, "CHECKED", "C.D"), (683, "WEIGHT", "SEE NOTE"),
                        (743, "SCALE", "1:1.5")):
        _t(p, x, 497, lab, 6)
        _t(p, x + 4, 507, val)
    _t(p, 563, 520, "TITLE :", 6)
    _t(p, 620, 522, "BASE ASSEMBLY", 8)
    _v(p, 760, 510, 570)
    _t(p, 563, 535, "DRAWING No. :", 6)
    _t(p, 630, 537, "XY-200-01", 9)
    _t(p, 765, 530, "REV.00")
    _t(p, 765, 540, "01/07/2026")
    _t(p, 563, 560, "NEXT ASSY. : XY-200-00")
    _t(p, 765, 560, "SIZE : A4")
    doc.save(path)
    doc.close()


def test_tight_bom_rows_scale_and_product_codes(tmp_path):
    pdf = tmp_path / "XY-200-01 REV.00.pdf"
    make_tight_bom_sheet(str(pdf))
    rec = read_drawing(str(pdf))[0]
    assert [r["item"] for r in rec.bom] == ["1", "2", "3", "4", "5"]
    assert rec.bom[1] == {"item": "2", "description": "CLAMP-B", "qty": "1 NO", "ref": "XY-200-3B"}
    assert rec.title_block["scale"] == "1:1.5"           # not the DETAIL A view's 5 : 1
    assert 4510.0 not in rec.dimension_values and 666.0 not in rec.dimension_values
    assert 86.0 not in rec.dimension_values               # a weight in a note
    assert {393.0, 158.75} <= set(rec.dimension_values)


def test_see_note_weight_needs_no_unit(tmp_path):
    import rules
    pdf = tmp_path / "XY-200-01 REV.00.pdf"
    make_tight_bom_sheet(str(pdf))
    rec = read_drawing(str(pdf))[0].to_dict()
    claims = [f["description"] for f in rules.run_rules(rec)]
    assert not any("WEIGHT" in c and "unit" in c for c in claims)
    assert not any("SCALE" in c and "cannot be right" in c for c in claims)


def _dwg(no, title, next_assy=None, bom=(), gt=None, fn=None):
    return {"drawing_no": no, "title": title, "revision": "00", "next_assy": next_assy,
            "bom": [dict(b) for b in bom], "_ocr": False, "_file": f"{fn or no}.pdf",
            "filename_drawing_no": fn, "form_id": None, "general_tolerance": gt or {}}


def _types(issues):
    return sorted(i["type"] for i in issues)


def test_part_used_in_two_assemblies_and_combined_titles():
    a = _dwg("XY-200-02", "PCB ASSEMBLY A", "XY-200-01",
             bom=[{"item_no": "1", "description": "PCB-A", "ref": "XY-200-04A"}])
    b = _dwg("XY-200-03", "PCB ASSEMBLY B", "XY-200-01",
             bom=[{"item_no": "1", "description": "PCB-B", "ref": "XY-200-04B"}])
    top = _dwg("XY-200-01", "FINAL", bom=[{"item_no": "1", "description": "PCB ASSEMBLY A", "ref": "XY-200-02"},
                                         {"item_no": "2", "description": "PCB ASSEMBLY B", "ref": "XY-200-03"}])
    pcb = _dwg("XY-200-04A&B", "PCB-A&B", "XY-200-02, XY-200-03")
    assert check_drawings([top, a, b, pcb]) == []


def test_reference_without_leading_zero_is_found_and_reported():
    parent = _dwg("XY-200-01", "FINAL", bom=[{"item_no": "1", "description": "CLAMP-B", "ref": "XY-200-3B"}])
    clamp = _dwg("XY-200-03A&B", "CLAMP-A & B", "XY-200-01")
    issues = check_drawings([parent, clamp])
    assert _types(issues) == ["Reference written differently from drawing number"]
    assert "XY-200-3B" in issues[0]["detail"]


def test_title_block_number_missing_prefix_still_links_by_file_name():
    parent = _dwg("XY-200-01", "FINAL", bom=[{"item_no": "1", "description": "CLAMP-A", "ref": "XY-200-03A"}])
    clamp = _dwg("200-03A&B", "CLAMP-A & B", "XY-200-01", fn="XY-200-03A&B")
    assert check_drawings([parent, clamp]) == []


def test_drawing_with_odd_general_tolerances_is_flagged_against_the_set():
    std = {"without_decimal": "±1", "one_decimal": "±0.1", "two_decimal": "±0.05"}
    odd = {"without_decimal": "±1", "one_decimal": "±0.5", "two_decimal": "±0.1"}
    ds = [_dwg(f"XY-200-0{i}", f"PART {i}", gt=std) for i in range(1, 5)] + [_dwg("XY-200-09", "ODD", gt=odd)]
    issues = check_drawings(ds)
    assert _types(issues) == ["General tolerances differ from the rest of the set"]
    assert issues[0]["drawing_no"] == "XY-200-09"


def test_ocr_number_with_one_stray_character_is_corrected():
    parent = _dwg("XY-KD 200-E-05", "SHELL", bom=[{"item_no": "3", "description": "BLOCK", "ref": "VI-KD 2l00-E-08"}])
    parent["_ocr"] = True
    block = _dwg("VT-KD 200-E-08", "BLOCK", "XY-KD 200-E-05")
    parent["drawing_no"] = "VT-KD 200-E-05"
    block["next_assy"] = "VT-KD 200-E-05"
    assert check_drawings([parent, block]) == []


def test_report_keeps_ampersand_readable():
    from supervisor.report import escape
    assert escape("E-03A&B") == "E-03A&B"
    assert escape("<b>") == "&lt;b&gt;"
