"""One local Ollama transport with exact model resolution and bounded output."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import requests

from .protocol import SYSTEM, TOOL_SPECS
from .schema import PROTOCOL_SCHEMA


class Ollama:
    def __init__(self, host="http://localhost:11434", model="gemma4:e4b", timeout=180, context=16384):
        url = urlparse(host)
        if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password:
            raise ValueError("This edition accepts a local loopback Ollama endpoint only")
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError("Use the Ollama origin without a path")
        self.host, self.model = host.rstrip("/"), model
        self.timeout, self.context = timeout, context
        self.session = requests.Session()
        self.session.trust_env = False
        self.identity = None

    def request(self, path, data=None, timeout=None):
        timeout = min(self.timeout, timeout or self.timeout)
        if timeout <= 0:
            raise TimeoutError("Model time budget exhausted")
        response = self.session.request("POST" if data is not None else "GET", self.host + path,
                                        json=data, timeout=(min(5, timeout), timeout))
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise ValueError("Model response exceeds transport limit")
        return response.json()

    def preflight(self):
        models = self.request("/api/tags").get("models", [])
        requested = self.model if ":" in self.model else self.model + ":latest"
        found = next((m for m in models if m.get("name", m.get("model")) == requested), None)
        if found is None:
            raise ValueError(f"Exact local model {requested} is not installed")
        info = self.request("/api/show", {"model": requested})
        self.model = requested
        self.identity = {"name": requested, "digest": found.get("digest"),
                         "ollama_version": self.request("/api/version").get("version"),
                         "capabilities": info.get("capabilities", []), "context": self.context}
        if not self.identity["digest"]:
            raise ValueError("Model digest is absent; cannot establish review identity")
        return self.identity

    def chat(self, messages, thinking=False, timeout=None, max_tokens=1000, response_schema=None):
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "think": thinking, "keep_alive": "10m",
                   "format": PROTOCOL_SCHEMA if response_schema is None else response_schema,
                   "options": {"num_ctx": self.context, "num_predict": max_tokens,
                               "temperature": 1.0, "top_p": 0.95, "top_k": 64}}
        response = self.request("/api/chat", payload, timeout)
        if response.get("done_reason") == "length":
            raise ValueError("Model output was truncated by the generation limit")
        msg = response.get("message") or {}
        if not isinstance(msg.get("content"), str):
            raise ValueError("Model returned no final text")
        # Retain concise result and runtime counters, never recycle thinking traces.
        if not msg["content"].strip():
            raise ValueError("Model returned empty final text")
        metrics = {k: response.get(k) for k in ("total_duration", "load_duration", "prompt_eval_count",
                    "prompt_eval_duration", "eval_count", "eval_duration", "done_reason")}
        return msg["content"], metrics

    def close(self):
        self.session.close()


def system_message():
    return {"role": "system", "content": SYSTEM + "\nAllowed tools:\n" + json.dumps(TOOL_SPECS)}
