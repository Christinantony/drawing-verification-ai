"""The shared sheet reader on CAD-like synthetic drawings: loose-line title
block and BOM, header-at-bottom BOM, vertical text, and a sideways image-only
scan (the AutoCAD "Print to PDF" case)."""
from conftest import requires_tesseract
from sheetreader import build_cells, find_tables, load_sheets, read_title_block
from sheetreader.normalize import ids_from_filename, ocr_key, rev_key, same_id
from sheetreader.record import read_drawing


def test_text_layer_sheet_is_not_ocrd_and_keeps_text_direction(cad_pdf):
    sheet = load_sheets(cad_pdf)[0]
    assert sheet.text_source == "embedded"
    assert sheet.rotation_deg == 0
    assert sheet.paper_size == "A4" and sheet.orientation == "landscape"
    vertical = [w for w in sheet.words if w.text == "40.0"]
    assert vertical and not vertical[0].horizontal  # read as "40.0", not reversed


def test_title_block_from_loose_lines(cad_pdf):
    sheet = load_sheets(cad_pdf)[0]
    assert not sheet.v_lines or all(s.length > 0 for s in sheet.v_lines)
    tb = read_title_block(sheet)
    f = tb.fields
    assert f["drawing_number"] == "XY-100-01"
    assert f["title"] == "SUPPORT BRAKCET"
    assert f["revision"] == "01"
    assert f["date"] == "05/03/2026"
    assert f["drawn_by"] == "A.B" and f["checked_by"] == "C.D" and f["approved_by"] == "E.F"
    assert f["material"] == "AL 6061-T6" and f["finish"] == "ANODIZED"
    assert f["scale"] == "10:1" and f["size"] == "A4" and f["sheet"] == "1/1"
    assert f["next_assy"] == "XY-100-00"
    assert f["weight"] == "120±5 g"
    assert tb.general_tolerance["one_decimal"] == "±0.1"
    assert tb.general_tolerance["two_decimal"] == "±0.05"


def test_bom_with_header_at_bottom_and_revision_table(cad_pdf):
    sheet = load_sheets(cad_pdf)[0]
    cells = build_cells(sheet)
    bom = find_tables(cells, "bom", sheet)[0]
    assert bom.header_at == "bottom"
    assert [r["item"] for r in bom.rows] == ["1", "2", "3"]
    assert bom.rows[1]["description"] == "SILICON SEALANT"
    assert bom.rows[2]["qty"] == "0"
    rev = find_tables(cells, "revision", sheet)[0]
    assert rev.rows == [{"rev": "01", "zone": "B2", "details": "HOLE ADDED"}]


def test_record_collects_notes_form_and_dimensions(cad_pdf):
    rec = read_drawing(cad_pdf)[0]
    assert rec.filename_drawing_number == "XY-100-01" and rec.filename_revision == "01"
    assert rec.form_id == "XY-F-DRG-01 REV.00"
    assert "APPLY SILICON SEALANT ON MATING FACES." in rec.notes
    assert max(rec.dimension_values) == 180.0


@requires_tesseract
def test_sideways_scan_is_turned_upright_and_read(sideways_scan_pdf):
    rec = read_drawing(sideways_scan_pdf)[0]
    assert rec.text_source == "ocr"
    assert rec.rotation_deg in (90, 270)
    assert rec.orientation == "landscape" and rec.paper_size == "A4"
    tb = rec.title_block
    assert ocr_key(tb["drawing_number"]) == ocr_key("XY-100-01")
    assert "SUPPORT" in (tb["title"] or "")
    assert tb["revision"] == "01"
    assert [r.get("item") for r in rec.bom] == ["1", "2", "3"]
    assert rec.bom[2].get("qty") == "0"  # a lone digit the page-wide OCR misses
    assert rec.general_tolerance["one_decimal"] == "±0.1"


def test_identifier_helpers():
    assert ids_from_filename("AT-PX_2B_40-03_REV._00.pdf") == ("AT-PX 2B 40-03", "00")
    assert ids_from_filename("XY-100-01 REV.01.pdf") == ("XY-100-01", "01")
    assert ids_from_filename("XY-100-01_REV.01_sideways_scan.pdf") == ("XY-100-01", "01")
    assert ids_from_filename("AT-KD_M12-E-03_AB_REV.01.pdf") == ("AT-KD M12-E-03 AB", "01")
    assert same_id("AT-KD_M12-E-03_AB", "AT-KD M12-E-03 A&B")
    assert not same_id("AI-KD M12-E-02", "AT-KD M12-E-02")
    assert same_id("AI-KD M12-E-02", "AT-KD M12-E-02", ocr=True)
    assert rev_key("02") > rev_key("01") and rev_key("B") > rev_key("A")
