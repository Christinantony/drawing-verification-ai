"""Bounded supervisor investigations with explicit coverage and failure states."""
from __future__ import annotations

import time
import json
from jsonschema import validate as validate_schema

from .model import system_message
from .protocol import PROTOCOL_VERSION, parse, validate_assessment
from .store import digest, dumps
from .tools import Tools
from .schema import review_schema


class Controller:
    def __init__(self, workspace, client=None, rounds=2, seconds=180, vision=False,
                 max_tokens=1000, thinking="auto", reuse=False):
        self.ws, self.client = workspace, client
        self.rounds, self.seconds, self.vision = rounds, seconds, vision
        self.max_tokens, self.thinking, self.reuse = max_tokens, thinking, reuse

    def review(self):
        started = time.monotonic()
        assessments, failures, metrics = [], [], []
        identity = None
        if self.client:
            try:
                identity = self.client.preflight()
                if self.vision and "vision" not in identity.get("capabilities", []):
                    raise ValueError("Installed model does not advertise vision")
            except Exception as exc:
                failures.append({"question": f"Supervisor unavailable: {exc}", "evidence_ids": []})
                self.client = None
        self.ws.store.event(self.ws.run, "model_identity", identity)
        for number, group in enumerate(self.ws.groups, 1):
            print(f"Supervisor: {number}/{len(self.ws.groups)}", flush=True)
            if self.client is None:
                failures.append({"question": "Supervisor review not performed", "evidence_ids": group["required_ids"]})
                continue
            try:
                result, counters = self.investigate(group, identity)
                assessments.append(result)
                metrics.extend(counters)
            except Exception as exc:
                failure = {"question": f"Supervisor review incomplete: {exc}", "evidence_ids": group["required_ids"]}
                failures.append(failure)
                self.ws.store.event(self.ws.run, "incomplete", failure)
        entities = [e for a in assessments for e in a["entities"]]
        initial_groups = len(self.ws.groups)
        self.ws.entity_checks(entities)
        for group in self.ws.groups[initial_groups:]:
            print("Supervisor: document relationship", flush=True)
            try:
                if self.client is None:
                    raise ValueError("Supervisor unavailable")
                assessment, counters = self.investigate(group, identity)
                assessments.append(assessment)
                metrics.extend(counters)
            except Exception as exc:
                failures.append({"question": f"Document relationship review incomplete: {exc}",
                                 "evidence_ids": group["required_ids"]})
        findings = list(self.ws.baseline)
        for a in assessments:
            findings.extend({**f, "source": "supervisor", "evidence_quality": "model interpretation; engineer confirmation required"}
                            for f in a["findings"])
        unresolved = self.ws.incomplete + failures + [u for a in assessments for u in a["unresolved"]]
        # Recheck every source before emitting a completed review.
        from .store import file_hash
        for name, path in self.ws.paths.items():
            if not path.exists() or file_hash(path) != self.ws.hashes[name]:
                unresolved.append({"question": f"Input changed during review: {name}", "evidence_ids": []})
        reviewed = sorted({e for a in assessments for e in a["reviewed_ids"]})
        required = sorted({e for g in self.ws.groups for e in g["required_ids"]})
        result = {"run_id": self.ws.run, "manifest": self.ws.manifest, "model": identity,
                  "supervisor_status": "completed" if identity and not failures and set(reviewed) == set(required) else "incomplete",
                  "coverage": "complete" if not unresolved and set(reviewed) == set(required) else "incomplete",
                  "issue_status": "issues_found" if any(f["severity"] != "info" for f in findings) else "no_issue_found",
                  "required_ids": required, "reviewed_ids": reviewed, "findings": findings,
                  "unresolved": unresolved, "warnings": list(self.ws.warnings),
                  "checklists": dict(getattr(self.ws, "checklist_results", {})),
                  "skipped": list(getattr(self.ws, "skipped", [])), "plan": getattr(self.ws, "plan", None),
                  "metrics": metrics, "supervisor_seconds": time.monotonic() - started,
                  "scope": "Extracted text, drawing metadata, document relations, registered checks; visual regions only when requested.",
                  "not_verified": ["Complete geometric correctness", "Complete GD&T interpretation", "Manufacturing suitability", "Official drawing approval"]}
        self.ws.store.finish(self.ws.run, result)
        return result

    def investigate(self, group, identity):
        ws = self.ws
        tools = Tools(ws, self.vision)
        required = group["required_ids"]
        allowed = set(required)
        baseline = [f for f in ws.baseline if set(f["evidence_ids"]) & set(required)]
        # Keep complete text in the prompt. Repeated hashes and source geometry
        # stay in the ledger and remain available through tools.
        fields = ("id", "kind", "text", "file", "page", "location", "chunk", "chunks", "char_start")
        package = {"required_ids": required, "evidence": [{k: ws.evidence[e][k] for k in fields if k in ws.evidence[e]} for e in required],
                   "related_index": tools.get_related_records(required[0]),
                   "baseline_findings": baseline[:8], "baseline_finding_count": len(baseline),
                   "baseline_more": len(baseline) > 8,
                   "review_policy": "Assess the supplied text and relevant relationships. Request missing evidence. Do not imply full geometric verification.",
                   "crop_coordinates": "Original displayed PDF page in points (72/inch); original image pixels. Not the reader's upright coordinates.",
                   "requirements_available": len(ws.requirements), "vision_enabled": self.vision}
        # Related part identities are visible through the index; their content is fetched on demand.
        cache_key = digest({"protocol": PROTOCOL_VERSION, "manifest": ws.manifest, "package": package,
                            "model": identity, "rounds": self.rounds, "thinking": self.thinking,
                            "max_tokens": self.max_tokens, "seconds": self.seconds})
        cached = ws.store.cached(cache_key) if self.reuse else None
        if cached:
            # Only no-tool assessments are cached, so citations refer to this run's evidence.
            validate_assessment(cached, required, ws.evidence, allowed)
            ws.store.event(ws.run, "assessment_cache_hit", {"group": group["id"]})
            return cached, [{"cache_hit": True}]
        messages = [system_message(), {"role": "user", "content": json.dumps(package, ensure_ascii=False, separators=(",", ":"))}]
        deadline = time.monotonic() + self.seconds
        metrics, tool_used, blocked = [], False, []
        for turn in range(self.rounds + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Investigation time limit reached")
            thinking = self.thinking == "on" or (self.thinking == "auto" and turn > 0)
            # A conservative UTF-8 byte bound avoids silent prompt truncation for text.
            byte_budget = sum(len(m["content"].encode("utf-8")) for m in messages)
            image_reserve = 4096 * sum(len(m.get("images", [])) for m in messages)
            if byte_budget + self.max_tokens + image_reserve + 512 > self.client.context:
                raise ValueError("Context budget exhausted; split evidence or increase --context")
            schema = review_schema(required, turn == self.rounds, evidence=ws.evidence, allowed=allowed)
            content, counters = self.client.chat(messages, thinking=thinking, timeout=remaining,
                                                  max_tokens=self.max_tokens,
                                                  response_schema=schema)
            metrics.append(counters)
            ws.store.event(ws.run, "model_turn", {"group": group["id"], "turn": turn,
                "thinking": thinking, "response": content, "metrics": counters})
            obj = parse(content)
            validate_schema(obj, schema)
            if obj["kind"] == "assessment":
                result = validate_assessment(obj, required, ws.evidence, allowed)
                # A failed investigation cannot disappear through a clean model answer.
                result["unresolved"].extend(blocked)
                if not tool_used and not result["unresolved"]:
                    ws.store.cache(cache_key, result)
                ws.store.event(ws.run, "assessment", result)
                return result, metrics
            if turn == self.rounds:
                raise ValueError("Tool investigation round limit reached")
            tool_used = True
            results, images = [], []
            new_information = False
            for request in obj["requests"]:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Investigation time limit reached")
                try:
                    output = tools.execute(request, timeout=deadline - time.monotonic())
                    eids = {e["id"] for e in output.get("evidence", [])}
                    gained = bool(eids - allowed) or bool(output.get("matches"))
                    if output.get("new_information") is False:
                        gained = False
                    new_information |= gained
                    allowed.update(eids)
                    images.extend(tools.images[e] for e in output.get("image_ids", []))
                    result = {"request": request, "result": output}
                except Exception as exc:
                    result = {"request": request, "error": str(exc)}
                    blocked.append({"question": f"Tool {request['name']} did not complete: {exc}",
                                    "evidence_ids": required})
                ws.store.event(ws.run, "tool", result)
                results.append(result)
            if not new_information:
                # Permit a final assessment, but explicitly require unresolved coverage.
                results.append({"controller": "No new evidence. Finish with an unresolved item for the unanswered question."})
                blocked.append({"question": "Investigation returned no new evidence; check the requested action.",
                                "evidence_ids": required})
            messages.append({"role": "assistant", "content": content})
            message = {"role": "user", "content": dumps({"tool_results": results,
                        "remaining_rounds": self.rounds - turn - 1})}
            if images:
                message["images"] = images
            messages.append(message)
        raise ValueError("Investigation ended without assessment")
