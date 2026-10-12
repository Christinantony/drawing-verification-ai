"""Validated, read-only tools. Every result is retained with its source."""
from __future__ import annotations

import base64
import io
import math
import multiprocessing
import queue

from . import legacy
from .store import digest, dumps, file_hash


def _read_worker(path, force_ocr, channel):
    try:
        channel.put({"records": legacy.read(path, force_ocr=force_ocr)})
    except Exception as exc:
        channel.put({"error": str(exc)})


def bounded_read(path, force_ocr, timeout):
    """Expensive OCR requests run in a terminable process, including on Windows."""
    ctx = multiprocessing.get_context("spawn")
    channel = ctx.Queue()
    process = ctx.Process(target=_read_worker, args=(str(path), force_ocr, channel))
    process.start()
    try:
        try:
            result = channel.get(timeout=max(0.01, timeout))
        except queue.Empty:
            raise TimeoutError("Repeat read exceeded the investigation time budget")
        if "error" in result:
            raise ValueError(result["error"])
        return result["records"]
    finally:
        process.join(timeout=0.1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        channel.close()


class Tools:
    def __init__(self, workspace, vision=False):
        self.ws = workspace
        self.vision = vision
        self.seen = set()
        self.images = {}

    def execute(self, request, timeout=180):
        name, args = request["name"], request["arguments"]
        signature = digest(request)
        if signature in self.seen:
            raise ValueError("Repeated request: no new evidence")
        self.seen.add(signature)
        allowed = {
            "get_evidence": ({"evidence_id"}, set()),
            "get_source_layout": ({"evidence_id", "query"}, {"offset"}),
            "search_evidence": ({"query"}, {"offset"}),
            "get_related_records": ({"evidence_id"}, {"offset"}),
            "run_check": ({"evidence_id", "check"}, set()),
            "repeat_read": ({"evidence_id", "force_ocr"}, set()),
            "get_source_crop": ({"evidence_id", "box"}, set()),
            "lookup_requirement": ({"query"}, {"offset"}),
            "calculate": ({"operation", "values", "unit"}, set()),
        }
        if name not in allowed:
            raise ValueError("Tool is not allowed")
        required, optional = allowed[name]
        if not required <= set(args) or set(args) - required - optional:
            raise ValueError("Invalid tool arguments")
        if "evidence_id" in args:
            eid = args["evidence_id"]
            if not isinstance(eid, str) or eid not in self.ws.evidence:
                raise ValueError("Unknown evidence id")
        if name == "repeat_read":
            return self.repeat_read(**args, timeout=timeout)
        return getattr(self, name)(**args)

    def get_evidence(self, evidence_id):
        if self.ws.evidence[evidence_id]["kind"] == "source_layout":
            raise ValueError("Use get_source_layout for bounded, paginated coordinate results")
        return {"evidence": [self.ws.evidence[evidence_id]]}

    def get_source_layout(self, evidence_id, query, offset=0):
        item = self.ws.evidence[evidence_id]
        if item.get("record_key") not in self.ws.layouts or not isinstance(query, str) or len(query) > 200:
            raise ValueError("Layout lookup requires drawing evidence and a short query")
        layout = self.ws.layouts[item["record_key"]]
        terms = query.casefold().split()
        words = [w for w in layout["words"] if not terms or any(t in w["text"].casefold() for t in terms)]
        result = self.page(words, offset)
        result["source_bounds"] = {k: v for k, v in layout.items() if k != "words"}
        eid = self.ws.add("layout_result", dumps(result), item["file"], item["page"], item["record_key"], query=query)
        result["evidence"] = [self.ws.evidence[eid]]
        return result

    def search_evidence(self, query, offset=0):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("Use a short nonempty source query")
        hits = [eid for eid, e in self.ws.evidence.items() if e["kind"] in
                ("drawing", "document", "checklist", "relationship") and query.casefold() in e["text"].casefold()]
        return self.page([self.summary(eid) for eid in sorted(hits)], offset)

    @staticmethod
    def page(items, offset):
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid offset")
        selected = items[offset:offset + 8]
        return {"matches": selected, "total": len(items),
                "next_offset": offset + len(selected) if offset + len(selected) < len(items) else None}

    def get_related_records(self, evidence_id, offset=0):
        item = self.ws.evidence[evidence_id]
        ids = set(self.ws.relations.get(evidence_id, set()))
        if item.get("record_key"):
            ids.update(self.ws.record_ids[item["record_key"]])
        ids.discard(evidence_id)
        return self.page([self.summary(eid) for eid in sorted(ids)], offset)

    def summary(self, eid):
        e = self.ws.evidence[eid]
        return {"id": eid, "file": e["file"], "page": e["page"], "kind": e["kind"],
                "location": e.get("location"), "chunk": e.get("chunk"), "chars": len(e["text"])}

    def run_check(self, evidence_id, check):
        item = self.ws.evidence[evidence_id]
        if check == "drawing_rules":
            key = item.get("record_key")
            if key not in self.ws.records:
                raise ValueError("This evidence has no drawing record")
            findings = legacy.audit(self.ws.records[key])
        elif check == "coherence":
            findings = [f for f in self.ws.baseline if evidence_id in f["evidence_ids"]]
        else:
            raise ValueError("Unknown registered check")
        ids = self.ws.add_text("check_result", dumps(findings), file=item["file"], page=item["page"],
                               record_key=item.get("record_key"), check=check)
        return self.result(ids)

    def repeat_read(self, evidence_id, force_ocr, timeout=180):
        item = self.ws.evidence[evidence_id]
        if type(force_ocr) is not bool or item.get("record_key") not in self.ws.records:
            raise ValueError("Repeat read requires drawing evidence and a boolean OCR setting")
        path = self.ws.paths[item["file"]]
        if file_hash(path) != item["file_hash"]:
            raise ValueError("Source has changed since extraction")
        original = self.ws.records[item["record_key"]]
        records = bounded_read(path, force_ocr, timeout)
        reread = next(r for r in records if r["page"] == item["page"])
        reread["file"] = item["file"]
        ids = self.ws.add_text("repeat_read", dumps(reread), item["file"], item["page"],
                               item["record_key"], settings={"force_ocr": force_ocr})
        result = self.result(ids)
        result["new_information"] = reread != original
        result["original_preserved"] = True
        return result

    def lookup_requirement(self, query, offset=0):
        if not isinstance(query, str) or not 1 <= len(query) <= 200:
            raise ValueError("Invalid search query")
        terms = query.casefold().split()
        ids = [eid for eid, e in self.ws.evidence.items() if e["kind"] == "requirement" and
               any(t in (e["text"] + " " + e.get("requirement_id", "")).casefold() for t in terms)]
        result = self.page([self.summary(eid) for eid in sorted(ids)], offset)
        result["supplied_requirement_count"] = len(self.ws.requirements)
        return result

    def calculate(self, operation, values, unit):
        if unit not in ("mm", "cm", "m", "in") or not isinstance(values, list) or any(
                type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e12 for v in values):
            raise ValueError("Use finite numeric values and a supported length unit")
        if operation == "scaled_length" and len(values) == 3:
            length, numerator, denominator = values
            if min(values) <= 0:
                raise ValueError("Scale and length must be positive")
            result = {"paper_length": length * numerator / denominator, "unit": unit}
        elif operation == "tolerance_limits" and len(values) == 3:
            nominal, upper, lower = values
            if upper < lower:
                raise ValueError("Upper deviation must be at least the lower deviation")
            result = {"upper_limit": nominal + upper, "lower_limit": nominal + lower, "unit": unit}
        elif operation == "unit_conversion" and len(values) == 1:
            result = {"value_mm": values[0] * {"mm": 1, "cm": 10, "m": 1000, "in": 25.4}[unit]}
        else:
            raise ValueError("Unsupported calculation or argument count")
        eid = self.ws.add("calculation", dumps({"operation": operation, "inputs": values, "unit": unit,
                                                "result": result}), input_origin="model supplied; check against source")
        return self.result([eid])

    def get_source_crop(self, evidence_id, box):
        if not self.vision:
            raise ValueError("Vision is disabled. Run the local vision smoke test, then use --vision.")
        item = self.ws.evidence[evidence_id]
        if item.get("record_key") not in self.ws.records:
            raise ValueError("Source crop requires drawing evidence")
        if not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box):
            raise ValueError("box must contain four finite coordinates")
        path = self.ws.paths[item["file"]]
        if file_hash(path) != item["file_hash"]:
            raise ValueError("Source has changed")
        from PIL import Image
        x0, y0, x1, y1 = box
        if path.suffix.lower() == ".pdf":
            import pymupdf
            with pymupdf.open(path) as doc:
                page = doc[item["page"] - 1]
                if not (0 <= x0 < x1 <= page.rect.width and 0 <= y0 < y1 <= page.rect.height):
                    raise ValueError(f"Crop outside original displayed page: {page.rect.width} x {page.rect.height} points")
                scale = min(3.0, 1600 / max(x1 - x0, y1 - y0))
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=pymupdf.Rect(box), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        else:
            with Image.open(path) as original:
                if not (0 <= x0 < x1 <= original.width and 0 <= y0 < y1 <= original.height):
                    raise ValueError("Image crop coordinates are original image pixels")
                image = original.convert("RGB").crop(box)
                image.thumbnail((1600, 1600))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        eid = self.ws.add("image", f"Original source crop: {item['file']}, page {item['page']}, box {box}.",
                          item["file"], item["page"], item["record_key"], box=box,
                          image_hash=digest(base64.b64encode(data).decode()), coordinates="original displayed page")
        folder = self.ws.output / "runs" / self.ws.run / "crops"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{eid}.png").write_bytes(data)
        self.images[eid] = base64.b64encode(data).decode()
        result = self.result([eid])
        result["image_ids"] = [eid]
        return result

    def result(self, ids):
        # Return one full chunk; disclose all remaining IDs for explicit retrieval.
        return {"evidence": [self.ws.evidence[ids[0]]],
                "remaining_evidence": [self.summary(eid) for eid in ids[1:]], "total_chunks": len(ids)}
