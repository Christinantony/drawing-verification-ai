"""Settings for the QC agent. Everything here can be overridden by a JSON
file passed with --config (see qc/qc_config.example.json); only the keys you
give are changed."""
from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULTS = {
    "model": {
        "name": "gemma4:26b",
        "host": "http://localhost:11434",
        "context": 8192,
        "seconds_per_call": 300,
        "vision": True,          # may read an image of a single text box (never the views)
        "doubt_passes": 2,
    },
    # must = must fix before print; look = please look.
    "severity": {
        "A1": "must", "A2_missing_balloon": "look", "A2_extra_balloon": "must", "A3": "look", "A4": "look",
        "A5": "must", "A6": "must",
        "B1": "must", "B2": "must", "B2_unsure": "look", "B3": "look", "B4": "look", "B5": "look",
        "C1_styled": "must", "C1_missing": "look", "C2": "must", "C3": "must", "C4": "look",
        "C5": "must", "C6": "must", "C7": "look", "C8": "must",
        "D1": "look", "D2": "must", "D3": "look", "D4": "look", "D5": "look",
        "E1": "look", "E2": "look",
    },
    # Title-block fields every drawing must have ...
    "required_fields": ["drawing_number", "title", "material", "weight", "scale", "size", "sheet",
                        "revision", "date", "drawn_by", "checked_by"],
    # ... at least one of these (approval / verification signature)
    "approval_fields": ["approved_by", "verified_by"],
    # ... except for these drawing types (detected from the title).
    "drawing_types": {
        "schematic": {"title_words": ["SCHEMATIC", "WIRING DIAGRAM", "CIRCUIT"],
                      "exempt": ["material", "weight", "scale"]},
        "installation": {"title_words": ["INSTALLATION"], "exempt": ["material"]},
    },
    # Words that are correct on your drawings but not in the dictionary.
    "shop_words": ["KAPTON", "FIBRE", "CHROMATISATION", "LITZ", "KEVLAR", "PREFORM", "INFUSION",
                   "SWG", "GSM", "ENAMELLED", "RIVETING", "ARALDITE", "LOCTITE", "ANABOND", "VSWR",
                   "MHZ", "BNC", "FR4", "PTFE", "NTS", "VHF", "PCB", "CSK", "THK", "DXF", "STEP", "RAR",
                   "ASTM", "AMS", "MIL", "DTL", "IPC", "IS"],
    # Drawing-number prefixes of other document series (jigs, moulds,
    # templates). Detected automatically; listed here only for the report text.
    "tool_series_words": ["JIG", "ML", "TP", "FIX", "TOOL"],
}


def load(path=None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        user = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif k == "shop_words" and isinstance(v, list):
                cfg[k] = sorted(set(cfg[k]) | {w.upper() for w in v})
            else:
                cfg[k] = v
    return cfg
