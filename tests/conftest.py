import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("drawing-audit", "dimcheck", "coherence", "tests"):
    sys.path.insert(0, str(ROOT / sub))

requires_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None,
                                        reason="Tesseract OCR not installed")


@pytest.fixture(scope="session")
def cad_pdf(tmp_path_factory):
    """Text-layer synthetic drawing, named like a real one (number + rev)."""
    from make_cad_like_drawing import make_cad_like_drawing
    p = tmp_path_factory.mktemp("cad") / "XY-100-01_REV.01.pdf"
    make_cad_like_drawing(str(p))
    return p


@pytest.fixture(scope="session")
def sideways_scan_pdf(tmp_path_factory, cad_pdf):
    """The same sheet as an image-only PDF, turned sideways on a portrait page."""
    from make_cad_like_drawing import make_sideways_scan
    p = tmp_path_factory.mktemp("scan") / "XY-100-01_REV.01.pdf"
    make_sideways_scan(str(cad_pdf), str(p))
    return p
