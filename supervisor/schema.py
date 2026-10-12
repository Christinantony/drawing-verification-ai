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


def source_quotes(text, size=240):
    """Literal source options, not model-written or normalized quotations."""
    import json
    options = []
    for line in text.splitlines():
        line = line.strip()
        for start in range(0, len(line), size):
            options.append(line[start:start + size])
    # Include short JSON values as convenient exact quote options. The full
    # source text remains in the prompt and ledger, with its original format.
    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                leaves(item)
        elif isinstance(value, list):
            for item in value:
                leaves(item)
        elif isinstance(value, str):
            if value and len(value) <= size:
                options.append(value)
    try:
        leaves(json.loads(text))
    except ValueError:
        pass
    return sorted({q for q in options if q.strip() and q in text})


def review_schema(required_ids, final_turn=False, evidence=None, allowed=None):
    import copy
    assessment = copy.deepcopy(ASSESSMENT)
    assessment["properties"]["reviewed_ids"] = {"const": list(required_ids)}
    if evidence is not None:
        ids = sorted(allowed if allowed is not None else required_ids)
        references = array({"type": "string", "enum": ids}, minItems=1, maxItems=50)
        finding = assessment["properties"]["findings"]["items"]
        finding["properties"]["evidence_ids"] = references
        bases = ["text"]
        kinds = {evidence[e]["kind"] for e in ids}
        for basis, kind in (("requirement", "requirement"), ("calculation", "calculation"), ("visual", "image")):
            if kind in kinds:
                bases.append(basis)
        finding["properties"]["basis"] = {"type": "string", "enum": bases}
        quotes = [obj({"evidence_id": {"const": eid}, "text": {"type": "string", "enum": values}})
                  for eid in ids if (values := source_quotes(evidence[eid]["text"]))]
        if quotes:
            finding["properties"]["quotes"] = array({"oneOf": quotes}, minItems=1)
        else:
            assessment["properties"]["findings"] = {"const": []}
        assessment["properties"]["unresolved"]["items"]["properties"]["evidence_ids"] = references
        documents = [e for e in ids if evidence[e]["kind"] == "document"]
        if documents:
            assessment["properties"]["entities"]["items"]["properties"]["evidence_id"] = {"enum": documents}
        else:
            assessment["properties"]["entities"] = {"const": []}
    return assessment if final_turn else {"oneOf": [TOOLS, assessment]}
