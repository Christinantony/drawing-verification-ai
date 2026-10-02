"""
ollama_client.py
Thin wrapper around the `ollama` Python package for talking to a locally
running Gemma 4 E4B.

Native thinking mode: Gemma 4's chat template exposes reasoning through a
literal `<|think|>` control token placed at the start of the system turn
(see https://ollama.com/library/gemma4:e4b, "Thinking Mode Configuration").
Ollama's `think=True` chat argument is the documented, version-safe way to
trigger this - Ollama renders the token into the template for you and
returns the reasoning trace separately as `message.thinking`, instead of it
being mixed into the final answer. We fall back to manually prepending the
raw token if an older ollama-python build doesn't accept `think=` yet.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import ollama

import config


def _get(obj, key, default=None):
    """Ollama's Python client has changed between dict-style and
    attribute-style (pydantic) responses across versions - this reads
    either safely."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_client(host: str = config.OLLAMA_HOST) -> ollama.Client:
    return ollama.Client(host=host)


def check_model_available(client: ollama.Client, model_name: str = config.MODEL_NAME) -> bool:
    try:
        response = client.list()
        models = _get(response, "models", []) or []
        names = [n for m in models for n in [_get(m, "model") or _get(m, "name")] if n]
        base = model_name.split(":")[0]
        return any(n == model_name or n.startswith(base + ":") or n == base for n in names)
    except Exception:
        return False


def chat_with_thinking(client: ollama.Client, system_prompt: str, user_prompt: str,
                        model: str = config.MODEL_NAME,
                        use_thinking: bool = config.USE_THINKING,
                        options: Optional[dict] = None):
    """Returns (thinking_trace, final_content) strings."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    kwargs = dict(model=model, messages=messages, stream=False,
                  options=options or config.MODEL_OPTIONS)

    try:
        response = client.chat(think=use_thinking, **kwargs) if use_thinking else client.chat(**kwargs)
    except TypeError:
        # Older ollama-python without a `think=` kwarg: fall back to the raw
        # control token exactly as Google/Ollama document it.
        if use_thinking:
            messages[0]["content"] = "<|think|>\n" + system_prompt
        response = client.chat(**kwargs)

    message = _get(response, "message")
    thinking = _get(message, "thinking", "") or ""
    content = _get(message, "content", "") or ""
    return thinking, content


def extract_json(content: str) -> Optional[dict]:
    """Pull the first ```json fenced block (or bare {...} object) out of a
    model response and parse it. We deliberately don't use Ollama's
    format='json' constrained decoding here, since that's documented to be
    mutually exclusive with thinking mode for this model family."""
    fence = re.search(r"```json\s*(.*?)```", content, re.DOTALL)
    candidate = fence.group(1) if fence else content
    brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not brace_match:
        return None
    try:
        return json.loads(brace_match.group(0))
    except json.JSONDecodeError:
        return None
