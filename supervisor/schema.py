"""Output shapes for local Ollama; evidence validation remains independent."""
from __future__ import annotations

STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
NULLABLE = {"type": ["string", "null"]}


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def array(item, **limits):
    return {"type": "array", "items": item, **limits}


FINDING = obj({"claim": STRING,
    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
    "evidence_ids": STRINGS, "quotes": array(obj({"evidence_id": STRING, "text": STRING})),
    "action": STRING, "basis": {"type": "string", "enum": ["text", "visual", "requirement", "calculation"]}})
ASSESSMENT = obj({"kind": {"const": "assessment"}, "reviewed_ids": STRINGS,
    "findings": array(FINDING, maxItems=100),
    "unresolved": array(obj({"question": STRING, "evidence_ids": STRINGS}), maxItems=100),
    "entities": array(obj({"evidence_id": STRING, "part_number": STRING,
        "part_name": NULLABLE, "revision": NULLABLE, "date": NULLABLE}), maxItems=100)})


def arguments(name):
    eid = {"evidence_id": STRING}
    offset = {"type": "integer", "minimum": 0}
    if name == "get_evidence":
        return obj(eid)
    if name == "get_source_layout":
        return obj({**eid, "query": STRING, "offset": offset}, ["evidence_id", "query"])
    if name in ("search_evidence", "lookup_requirement"):
        return obj({"query": STRING, "offset": offset}, ["query"])
    if name == "get_related_records":
        return obj({**eid, "offset": offset}, ["evidence_id"])
    if name == "run_check":
        return obj({**eid, "check": {"enum": ["drawing_rules", "coherence"]}})
    if name == "repeat_read":
        return obj({**eid, "force_ocr": {"type": "boolean"}})
    if name == "get_source_crop":
        return obj({**eid, "box": array({"type": "number"}, minItems=4, maxItems=4)})
    if name == "calculate":
        return obj({"operation": {"enum": ["scaled_length", "tolerance_limits", "unit_conversion"]},
            "values": array({"type": "number"}), "unit": {"enum": ["mm", "cm", "m", "in"]}})
    raise ValueError("Unknown tool schema")


from .protocol import TOOL_SPECS
TOOLS = obj({"kind": {"const": "tools"}, "requests": array({"oneOf": [
    obj({"name": {"const": name}, "arguments": arguments(name)}) for name in TOOL_SPECS
]}, minItems=1, maxItems=4)})
PROTOCOL_SCHEMA = {"oneOf": [TOOLS, ASSESSMENT]}
COLOR_SCHEMA = obj({"color": STRING})


def review_schema(required_ids, final_turn=False):
    import copy
    assessment = copy.deepcopy(ASSESSMENT)
    assessment["properties"]["reviewed_ids"] = {"const": list(required_ids)}
    return assessment if final_turn else {"oneOf": [TOOLS, assessment]}
