"""
sheetreader: the shared "read a drawing sheet" layer used by drawing-audit,
dimcheck and coherence.

    from sheetreader import load_sheets, read_title_block, find_tables

Every page becomes a Sheet (words + ruling lines, upright), whatever produced
the PDF: SolidWorks text-layer exports and AutoCAD sheets plotted through
"Microsoft Print to PDF" (no text layer, sideways on the page) come out the
same shape.
"""
from .model import Word, Segment, Cell, Sheet
from .loader import load_sheets
from .cells import build_cells, cells_in_region
from .titleblock import read_title_block, TitleBlock
from .tables import find_tables, Table

__all__ = ["Word", "Segment", "Cell", "Sheet", "load_sheets", "build_cells",
           "cells_in_region", "read_title_block", "TitleBlock", "find_tables", "Table"]
