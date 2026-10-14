"""Local model client (Ollama). Model-agnostic: gemma4:26b, gemma4:e4b or any
other local model with JSON output; vision is used only if the model has it.

Every call is short: thinking off, a JSON schema for the answer, a small
output limit. The QC agent asks many small questions instead of one big one.
"""
from __future__ import annotations

import base64
import io
import json
import time
from typing import Optional

import requests


class ModelError(RuntimeError):
    pass


class Client:
    def __init__(self, name="gemma4:26b", host="http://localhost:11434", context=8192, seconds=300):
        self.name, self.host, self.context, self.seconds = name, host.rstrip("/"), context, seconds
        self.calls, self.metrics = 0, []
        self._caps = None

    # -- setup ----------------------------------------------------------------
    def installed(self) -> list:
        r = requests.get(self.host + "/api/tags", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def capabilities(self) -> list:
        if self._caps is None:
            try:
                r = requests.post(self.host + "/api/show", json={"model": self.name}, timeout=30)
                r.raise_for_status()
                self._caps = r.json().get("capabilities") or []
            except Exception:
                self._caps = []
        return self._caps

    @property
    def vision(self) -> bool:
        return "vision" in self.capabilities()

    # -- calls ------------------------------------------------------------------
    def ask(self, system: str, user: str, schema: dict, images=None, max_tokens: int = 300) -> dict:
        msg = {"role": "user", "content": user}
        if images:
            msg["images"] = [_png_b64(i) for i in images]
        payload = {"model": self.name, "stream": False, "think": False, "keep_alive": "30m",
                   "messages": [{"role": "system", "content": system}, msg],
                   "format": schema,
                   "options": {"num_ctx": self.context, "num_predict": max_tokens, "temperature": 0}}
        t = time.monotonic()
        try:
            r = requests.post(self.host + "/api/chat", json=payload, timeout=(10, self.seconds))
            r.raise_for_status()
        except requests.RequestException as e:
            raise ModelError(f"model call failed: {e}") from e
        data = r.json()
        self.calls += 1
        self.metrics.append({k: data.get(k) for k in ("prompt_eval_count", "eval_count", "total_duration",
                                                      "prompt_eval_duration", "eval_duration")}
                            | {"seconds": round(time.monotonic() - t, 1)})
        if data.get("done_reason") == "length":
            raise ModelError("answer cut off by the output limit")
        text = (data.get("message") or {}).get("content") or ""
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ModelError(f"answer was not JSON: {text[:200]}") from e

    def speed(self) -> dict:
        """Prompt and output speed from the calls so far (tokens/second)."""
        pe = sum(m.get("prompt_eval_count") or 0 for m in self.metrics)
        pd = sum(m.get("prompt_eval_duration") or 0 for m in self.metrics) / 1e9
        ec = sum(m.get("eval_count") or 0 for m in self.metrics)
        ed = sum(m.get("eval_duration") or 0 for m in self.metrics) / 1e9
        return {"calls": self.calls, "prompt_tok_s": round(pe / pd, 1) if pd else None,
                "output_tok_s": round(ec / ed, 1) if ed else None,
                "seconds": round(sum(m["seconds"] for m in self.metrics), 1)}


def _png_b64(img) -> str:
    """numpy grey/RGB array or PIL image -> base64 PNG."""
    from PIL import Image
    import numpy as np
    if isinstance(img, np.ndarray):
        img = Image.fromarray(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class NoModel:
    """Stand-in when --no-model is used: every judgement is 'not run'."""
    name = "none"
    vision = False
    calls = 0

    def ask(self, *a, **k):
        raise ModelError("no model (run without --no-model to use one)")

    def speed(self):
        return {"calls": 0}
