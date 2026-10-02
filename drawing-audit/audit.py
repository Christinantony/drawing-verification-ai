"""
audit.py
Step 3 of the pipeline: feed the structured title-block + BOM data to
Gemma 4 E4B (via Ollama, native thinking mode) and have it cross-examine
the text for spelling/grammar errors, internal contradictions, and
BOM-to-title-block mismatches.
"""

from __future__ import annotations

import config
from ollama_client import chat_with_thinking, extract_json

SYSTEM_PROMPT = """You are a meticulous quality-control auditor for mechanical/engineering \
2D drawings. You are given a drawing's Title Block and Bill of Materials (BOM), already \
extracted and structured by an upstream OCR/parsing pipeline - you are not looking at the \
drawing image itself, only this text, so judge only what the text tells you.

Audit it for exactly three things:

1. SPELLING / GRAMMAR - typos or grammatical errors in the title, notes, and BOM \
descriptions. Ignore standard drafting abbreviations (QTY, MATL, TYP, REF, DIA, THK, etc.) \
and all-caps drafting style - that is not a grammar error.

2. INTERNAL CONTRADICTIONS - anything in the title block or notes that logically conflicts \
with itself: e.g. two different scales given, a revision letter mentioned in a note that \
doesn't match the title block's revision field, inconsistent units (mm vs inch), a sheet \
count that doesn't add up.

3. BOM-TO-TITLE-BLOCK MATCHING - cross-check the title block against the BOM: for a \
single-part drawing, does the title block's material/part number match the corresponding \
BOM row; are there duplicate or missing item numbers in the BOM; do part numbers referenced \
in notes actually appear in the BOM; do quantities look sane (e.g. not zero or blank).

Be conservative - only report something as a finding if you are reasonably confident it is \
a real error, not a stylistic or template choice. If a field wasn't found by extraction \
(shown as "(not found)"), that alone is a low-severity "missing_field" finding, not a \
contradiction. If the extracted text looks OCR-garbled, say so under category "ocr_quality" \
rather than guessing at a fix.

Think through the title block and BOM carefully, then give your final answer as ONLY the \
following JSON inside a single ```json fenced block:

{
  "overall_status": "PASS" | "NEEDS_REVIEW" | "FAIL",
  "findings": [
    {
      "id": "F1",
      "severity": "high" | "medium" | "low",
      "category": "spelling" | "grammar" | "contradiction" | "bom_mismatch" | "missing_field" | "ocr_quality",
      "location": "e.g. title_block.title, bom.item_3, notes",
      "description": "plain-language explanation of the issue",
      "suggested_fix": "concrete correction, or null"
    }
  ],
  "summary": "one short paragraph"
}

overall_status is "FAIL" if any high-severity finding exists, "NEEDS_REVIEW" if only \
medium/low findings exist, "PASS" if there are none.
"""

USER_PROMPT_TEMPLATE = """Here is the structured drawing data:

{structured_markdown}

Audit it now.
"""

_FALLBACK_RESULT_TEMPLATE = {
    "overall_status": "NEEDS_REVIEW",
    "findings": [{
        "id": "F0",
        "severity": "medium",
        "category": "other",
        "location": "n/a",
        "description": "The model's response could not be parsed as JSON. "
                        "See the raw response / reasoning trace for manual review.",
        "suggested_fix": None,
    }],
    "summary": "Automatic JSON parsing failed; manual review of the raw response is needed.",
}


def audit_drawing(structured_markdown: str, client, model: str = config.MODEL_NAME,
                   use_thinking: bool = config.USE_THINKING) -> dict:
    user_prompt = USER_PROMPT_TEMPLATE.format(structured_markdown=structured_markdown)
    thinking, content = chat_with_thinking(
        client, SYSTEM_PROMPT, user_prompt, model=model, use_thinking=use_thinking
    )

    result = extract_json(content)
    if result is None:
        result = dict(_FALLBACK_RESULT_TEMPLATE)

    result["_thinking_trace"] = thinking
    result["_raw_response"] = content
    return result
