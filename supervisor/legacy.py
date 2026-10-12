"""Adapters to the frozen baseline. No parser or check implementation is changed."""
from __future__ import annotations

import importlib.util
import sys
import threading
from functools import lru_cache
from pathlib import Path

from .store import digest, file_hash

ROOT = Path(__file__).resolve().parents[1]
_READ_LOCK = threading.RLock()


@lru_cache
def module(name, relative):
    # Legacy modules use sibling imports. This CLI owns its Python process.
    for folder in (ROOT, ROOT / "drawing-audit", ROOT / "dimcheck", ROOT / "coherence"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location("_supervisor_" + name, ROOT / relative)
    obj = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = obj
    spec.loader.exec_module(obj)
    return obj


def versions():
    paths = [p for d in ("sheetreader", "drawing-audit", "dimcheck", "coherence", "supervisor")
             for p in (ROOT / d).rglob("*.py")]
    return digest({str(p.relative_to(ROOT)): file_hash(p) for p in sorted(paths)})


def read(path, force_ocr=False, with_layout=False):
    import sheetreader.record as record_module
    from sheetreader.titleblock import DEFAULT_LABELS
    config = module("config", "drawing-audit/config.py")
    labels = {k: list(v) for k, v in DEFAULT_LABELS.items()}
    for key, values in config.EXTRA_TITLE_BLOCK_LABELS.items():
        labels[key] = list(values) + labels.get(key, [])
    if config.TESSERACT_CMD:
        import os
        os.environ["TESSERACT_CMD"] = config.TESSERACT_CMD
    # Capture the exact loader output from the normal read. A lock and finally
    # restore prevent the observation hook from leaking to another invocation.
    # No reader algorithm, returned record, word or cell is changed.
    with _READ_LOCK:
        original_loader = record_module.load_sheets
        captured = []
        def observe(*args, **kwargs):
            sheets = original_loader(*args, **kwargs)
            captured.extend(sheets)
            return sheets
        try:
            record_module.load_sheets = observe
            records = [r.to_dict() for r in record_module.read_drawing(path, force_ocr=force_ocr, labels=labels)]
        finally:
            record_module.load_sheets = original_loader
    if not with_layout:
        return records
    from sheetreader.loader import _rot_bbox
    layouts = {}
    for sheet in captured:
        # Map upright reader coordinates back to the original displayed page.
        inverse = (360 - sheet.rotation_deg) % 360
        words = [{"text": w.text, "upright_box": list(w.bbox),
                  "box": list(_rot_bbox(w.bbox, sheet.width, sheet.height, inverse)),
                  "ocr_confidence": w.conf if sheet.text_source == "ocr" else None}
                 for w in sheet.words]
        width, height = sheet.width, sheet.height
        if sheet.rotation_deg in (90, 270):
            width, height = height, width
        if Path(path).suffix.lower() != ".pdf":
            # Image reader points are converted to actual original image pixels.
            from PIL import Image
            with Image.open(path) as image:
                sx, sy = image.width / width, image.height / height
                for word in words:
                    x0, y0, x1, y1 = word["box"]
                    word["box"] = [x0 * sx, y0 * sy, x1 * sx, y1 * sy]
                width, height = image.width, image.height
        layouts[str(sheet.page_number)] = {"width": width, "height": height,
            "coordinates": "original displayed PDF points" if Path(path).suffix.lower() == ".pdf" else "original image pixels",
            "reader_rotation_deg": sheet.rotation_deg, "words": words}
    return {"records": records, "layouts": layouts}


def audit(rec):
    rules = module("rules", "drawing-audit/rules.py")
    language = module("language", "drawing-audit/language.py")
    # Known mistakes stay deterministic. The supervisor handles context and spelling.
    findings = rules.run_rules(rec) + language.known_misspellings(language.text_lines(rec))
    return findings


def coherence():
    return module("coherence", "coherence/check_coherence.py")
