"""Tests for the dimcheck building blocks that are present in this repo
(zone grid, general tolerance table, token extraction). The dimension
matcher module is not included yet, so the full pipeline is not tested."""
from dimcheck.pdf_layout import load_page_layout, flat_text
from dimcheck.zoning import detect_grid
from dimcheck.general_tolerance import parse_general_tolerance_table, count_decimals
from make_synthetic_drawing import make_drawing


def _layout(tmp_path):
    pdf = tmp_path / "synthetic.pdf"
    make_drawing(str(pdf))
    return load_page_layout(str(pdf))


def test_zone_grid_detected_from_border_labels(tmp_path):
    layout = _layout(tmp_path)
    grid = detect_grid(layout.tokens, layout.page_width, layout.page_height)
    assert grid.col_labels == ["4", "3", "2", "1"]
    assert grid.row_labels == ["C", "B", "A"]
    assert grid.locate(300, 120) == "C3"
    assert grid.locate(540, 300) == "B2"
    assert not grid.warnings


def test_general_tolerance_table_parsed(tmp_path):
    general = parse_general_tolerance_table(flat_text(_layout(tmp_path)))
    assert general.without_decimal == "±1"
    assert general.one_decimal == "±0.1"
    assert general.two_decimal == "±0.05"
    assert general.angle == "±0.5°"
    assert general.lookup_by_decimals(count_decimals("25.0")) == "±0.1"


def test_count_decimals():
    assert count_decimals("12") == 0
    assert count_decimals("3.5") == 1
    assert count_decimals("0.25") == 2
    assert count_decimals("M3") is None
