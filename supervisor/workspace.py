"""Build a complete manifest and evidence ledger from the unchanged readers."""
from __future__ import annotations

import copy
import importlib.metadata
import json
import platform
from pathlib import Path

from . import BASE_COMMIT, __version__, legacy
from .store import digest, dumps, file_hash

SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".doc", ".docx", ".xlsx"}

# Files that are found but cannot be reviewed get a warning, never silence.
UNSUPPORTED_HINTS = {
    (".dwg", ".dxf"): "CAD source file; export the sheet as PDF to review it",
    (".step", ".stp", ".igs", ".iges", ".sldprt", ".sldasm", ".slddrw", ".ipt", ".iam", ".prt", ".x_t"):
        "3D model or native CAD file; not reviewed",
    (".xls", ".xlsm", ".csv"): "spreadsheet format not supported; save as .xlsx",
    (".pptx", ".ppt", ".txt", ".rtf", ".odt", ".msg", ".eml", ".zip"): "format not supported",
}
IGNORED_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}


def unsupported_reason(path):
    suffix = path.suffix.lower()
    for suffixes, reason in UNSUPPORTED_HINTS.items():
        if suffix in suffixes:
            return reason
    return "file type not supported"


def chunks(text, size=4500):
    # Fixed chunks guarantee that even a single very long line is not lost.
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def runtime_versions():
    names = ("pymupdf", "pytesseract", "opencv-python-headless", "python-docx", "openpyxl", "jsonschema")
    out = {"python": platform.python_version()}
    for name in names:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = "missing"
    try:
        import pytesseract
        out["tesseract"] = str(pytesseract.get_tesseract_version())
    except Exception:
        out["tesseract"] = "unavailable"
    return out


class Workspace:
    def __init__(self, source, store, output, requirements=None):
        self.source = Path(source).resolve()
        self.output = Path(output).resolve()
        if not self.source.exists():
            raise ValueError("Input does not exist")
        self.root = self.source if self.source.is_dir() else self.source.parent
        if self.source == self.output or self.output in self.source.parents:
            raise ValueError("Output must not contain the input directory")
        paths = sorted(self.source.rglob("*")) if self.source.is_dir() else [self.source]
        self.paths = {}
        self.unsupported = {}
        for p in paths:
            if not p.is_file() or p.is_symlink() or self.output in p.resolve().parents:
                continue
            relative = p.relative_to(self.root)
            if any(part.startswith(".") or part in ("venv", "__pycache__", "reports", "supervisor-output")
                   for part in relative.parts) or p.name.startswith("~$"):
                continue
            if p.suffix.lower() in SUFFIXES:
                self.paths[relative.as_posix()] = p
            elif p.name.lower() not in IGNORED_NAMES:
                self.unsupported[relative.as_posix()] = unsupported_reason(p)
        if not self.paths:
            found = f" Found only: {', '.join(sorted(self.unsupported))}." if self.unsupported else ""
            raise ValueError("No supported drawing or document files found "
                             "(PDF, PNG/JPG/TIFF/BMP, DOC/DOCX, XLSX)." + found)
        self.store = store
        self.code_version = legacy.versions()
        self.hashes = {name: file_hash(p) for name, p in self.paths.items()}
        self.requirements = []
        if requirements:
            self.requirements = json.loads(Path(requirements).read_text(encoding="utf-8"))
            if not isinstance(self.requirements, list):
                raise ValueError("Requirements must be a JSON list")
            seen = set()
            for r in self.requirements:
                if not isinstance(r, dict) or set(r) != {"id", "source", "revision", "text"} or any(
                        not isinstance(v, str) or not v.strip() for v in r.values()):
                    raise ValueError("Each requirement needs id, source, revision and text")
                if r["id"] in seen:
                    raise ValueError("Duplicate requirement id")
                seen.add(r["id"])
        self.manifest = {"edition": __version__, "base_commit": BASE_COMMIT,
                         "code_version": self.code_version, "runtime": runtime_versions(),
                         "files": self.hashes, "requirements_hash": digest(self.requirements)}
        self.run = store.start(self.manifest)
        self.evidence = {}
        self.records = {}
        self.record_ids = {}
        self.layouts = {}
        self.groups = []
        self.baseline = []
        self.incomplete = []
        self.warnings = []
        self.relations = {}
        self.checklists = []
        # Per-checklist outcome for triage: unmatched | clean | concern
        self.checklist_status = {}
        # Per-checklist fill result: counts, cells still empty, completed copy path
        self.checklist_results = {}
        for name, reason in self.unsupported.items():
            self.warn("unsupported_file", f"Not reviewed: {name} ({reason}).")

    def add(self, kind, text, file=None, page=None, record_key=None, **extra):
        item = {"kind": kind, "text": text, "file": file, "page": page,
                "file_hash": self.hashes.get(file), "record_key": record_key, **extra}
        eid = "E-" + digest(item)[:20]
        item["id"] = eid
        if eid not in self.evidence:
            self.evidence[eid] = item
            self.store.evidence(self.run, item)
        return eid

    def add_text(self, kind, text, file=None, page=None, record_key=None, **extra):
        parts = chunks(text)
        return [self.add(kind, part, file, page, record_key, chunk=i + 1, chunks=len(parts),
                         char_start=i * 4500, **extra) for i, part in enumerate(parts)]

    def issue(self, question, refs=()):
        """A planned check that could not be completed: coverage stays incomplete."""
        self.incomplete.append({"question": question, "evidence_ids": list(refs)})

    def warn(self, code, message, refs=()):
        """Something not checked because it is absent or not concerning. Reported,
        but it does not make the review incomplete."""
        self.warnings.append({"code": code, "message": message, "evidence_ids": list(refs)})

    def link(self, a, b, kind):
        self.relations.setdefault(a, set()).add(b)
        self.relations.setdefault(b, set()).add(a)
        self.store.relate(self.run, a, b, kind)

    def baseline_finding(self, finding, refs):
        f = {"claim": finding.get("description", str(finding.get("detail", ""))),
             "severity": finding.get("severity", "medium"), "category": finding.get("category", finding.get("type")),
             "evidence_ids": refs, "action": finding.get("suggested_fix") or "Check the cited source.",
             "source": finding.get("source", "baseline"), "location": finding.get("location", "")}
        self.baseline.append(f)

    def prepare(self):
        checker = legacy.coherence()
        for name, path in self.paths.items():
            print(f"Read: {name}", flush=True)
            try:
                if path.suffix.lower() in {".doc", ".docx", ".xlsx"}:
                    self.read_document(name, path, checker)
                    continue
                key = digest({"read": self.hashes[name], "name": path.name,
                              "code": self.code_version, "runtime": self.manifest["runtime"]})
                bundle = self.store.cached(key)
                if bundle is None:
                    bundle = legacy.read(path, with_layout=True)
                    self.store.cache(key, bundle)
                records = bundle["records"]
                for record in copy.deepcopy(records):
                    record["file"] = name
                    record_key = f"{name}::page:{record['page']}"
                    self.records[record_key] = record
                    self.layouts[record_key] = bundle["layouts"][str(record["page"])]
                    layout = self.layouts[record_key]
                    self.add("source_layout", dumps(layout), name, record["page"], record_key,
                             location="word coordinates and OCR confidence")
                    # Structured values and complete extracted text both receive coverage.
                    structured = {k: v for k, v in record.items() if k != "text_lines"}
                    ids = self.add_text("drawing", json.dumps(structured, ensure_ascii=False, indent=2),
                                        name, record["page"], record_key, method=record["text_source"],
                                        rotation_deg=record["rotation_deg"], location="structured sheet",
                                        source_bounds={k: v for k, v in layout.items() if k != "words"})
                    raw = "\n".join(record.get("text_lines") or [])
                    ids += self.add_text("drawing", raw, name, record["page"], record_key,
                                         method=record["text_source"], location="complete extracted text")
                    self.record_ids[record_key] = ids
                    for eid in ids:
                        self.groups.append({"id": eid, "required_ids": [eid], "record_key": record_key})
                    for finding in legacy.audit(record):
                        self.baseline_finding(finding, ids)
                if not records:
                    self.issue(f"No sheets read: {name}")
            except Exception as exc:
                self.issue(f"Could not read {name}: {exc}")
            if file_hash(path) != self.hashes[name]:
                self.issue(f"Source changed during extraction: {name}. Repeat the review.")
        for req in self.requirements:
            self.add_text("requirement", req["text"], requirement_id=req["id"],
                          source=req["source"], revision=req["revision"])
        try:
            self.cross_checks(checker)
        except Exception as exc:
            self.issue(f"Baseline cross-file checks did not complete: {exc}")
        self.pack_groups()
        self.store.event(self.run, "baseline", {"findings": self.baseline, "incomplete": self.incomplete})

    def pack_groups(self):
        """Pack adjacent sections of one source without dropping any evidence."""
        packed = []
        for group in self.groups:
            eid = group["id"]
            item = self.evidence[eid]
            key = group["record_key"] or (item["file"], item["kind"])
            size = len(item["text"].encode("utf-8"))
            if packed and packed[-1]["pack_key"] == key and packed[-1]["bytes"] + size <= 6000:
                packed[-1]["required_ids"].append(eid)
                packed[-1]["bytes"] += size
            else:
                packed.append({**group, "required_ids": [eid], "pack_key": key, "bytes": size})
        self.groups = packed

    def read_document(self, name, path, checker):
        if path.suffix.lower() == ".xlsx":
            text = checker.read_xlsx(path)
            kind = "document"
            # Cached formula values can be absent. Do not silently treat them as empty cells.
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=False, read_only=True)
            try:
                if any(c.data_type == "f" for sheet in wb for row in sheet for c in row):
                    self.warn("excel_formulas", f"{name} has formulas; their last saved values were reviewed, not recalculated.")
            finally:
                wb.close()
        else:
            from dimcheck.checklist_io import ensure_docx
            converted = ensure_docx(str(path))
            text = checker.read_docx(converted)
            chk = checker.read_checklist(Path(converted))
            kind = "checklist" if chk else "document"
            if chk:
                chk["_file"] = name
                self.checklists.append(chk)
        if not text.strip():
            self.issue(f"No text read from {name}")
        ids = self.add_text(kind, text, name, method="document parser", location="document text")
        for eid in ids:
            self.groups.append({"id": eid, "required_ids": [eid], "record_key": None})

    def cross_checks(self, checker):
        drawings = [checker.drawing_from_record(rec) for rec in self.records.values()]
        issues = checker.check_drawings(drawings)
        index = checker.DrawingIndex(drawings)
        reviewed_pairs = set()
        def ids_for(d):
            return self.record_ids.get(f"{d['_file']}::page:{d['_page']}", [])
        for d in drawings:
            for ref in [d.get("next_assy")] + [x.get("ref") for x in d.get("bom", [])]:
                if ref:
                    for other in index.find_any(ref):
                        for a in ids_for(d):
                            for b in ids_for(other):
                                if a != b:
                                    self.link(a, b, "drawing reference")
                        pair = tuple(sorted((f"{d['_file']}:{d['_page']}", f"{other['_file']}:{other['_page']}")))
                        if pair[0] != pair[1] and pair not in reviewed_pairs:
                            reviewed_pairs.add(pair)
                            fields = ("_file", "_page", "drawing_no", "revision", "title", "material", "weight", "next_assy", "general_tolerance")
                            packet = {"relationship": "drawing reference", "reference": ref,
                                      "a": {k: d.get(k) for k in fields}, "b": {k: other.get(k) for k in fields},
                                      "source_ids": ids_for(d) + ids_for(other)}
                            eids = self.add_text("relationship", dumps(packet), location="drawing relationship")
                            for eid in eids:
                                self.groups.append({"id": eid, "required_ids": [eid], "record_key": None})
                                for source_id in packet["source_ids"]:
                                    self.link(eid, source_id, "relationship source")
        for issue in issues:
            ids = list(dict.fromkeys(e for d in issue.get("sheets", []) for e in ids_for(d)))
            if issue["type"] in ("BOM references missing drawing", "NEXT ASSY. points to unknown drawing"):
                # The referenced drawing is not in this set: the link cannot be
                # checked, which is a gap in the input, not an error in the drawing.
                self.warn("reference_not_in_set", "Skipped link check: " + str(issue["detail"]), ids)
                continue
            self.baseline_finding(issue, ids)
        from dimcheck.pipeline import process
        pdfs = {name: p for name, p in self.paths.items() if p.suffix.lower() == ".pdf"}
        for chk in self.checklists:
            cid = [e for e, data in self.evidence.items() if data["file"] == chk["_file"]]
            # Existing name matching is used, with an ambiguity guard before execution.
            stem = checker.alnum(Path(chk["_file"]).stem)
            scores = {n: checker._common_suffix(stem, checker.alnum(Path(n).stem)) for n in pdfs}
            best = max(scores.values(), default=0)
            matches = [n for n, score in scores.items() if score == best and score >= 8]
            if not matches:
                self.checklist_status[chk["_file"]] = "unmatched"
                self.warn("checklist_without_drawing",
                          f"Skipped checklist verification: no drawing in this set matches {chk['_file']}.", cid)
                continue
            if len(matches) > 1:
                self.checklist_status[chk["_file"]] = "concern"
                self.issue(f"Checklist matches several drawings equally ({', '.join(matches)}): {chk['_file']}", cid)
                continue
            name = matches[0]
            did = [e for e, data in self.evidence.items() if data["file"] == name]
            for a in cid:
                for b in did:
                    self.link(a, b, "inspection checklist")
            recs = [r for r in self.records.values() if r["file"] == name]
            if len(recs) != 1:
                self.checklist_status[chk["_file"]] = "concern"
                self.issue(f"Checklist page match needs engineer review: {chk['_file']}", cid + did)
                continue
            try:
                flat = checker.drawing_from_record(recs[0])
                flat["_file"] = pdfs[name].name
                metadata = checker.check_checklists([{**chk, "_file": Path(chk["_file"]).name}],
                                                   [flat], [pdfs[name]], run_dimcheck=False)
                for issue in metadata:
                    self.baseline_finding(issue, cid + did)
                # Fill empty tolerance cells in a copy in this run's folder. The
                # source checklist is never written; rows are never added and
                # existing values are never changed.
                completed = self.output / "runs" / self.run / "checklists" / (Path(chk["_file"]).stem + "_completed.docx")
                completed.parent.mkdir(parents=True, exist_ok=True)
                rows = process(str(pdfs[name]), chk["path"], str(completed), mark_filled=True)
                concern = bool(metadata)
                rid = self.add_text("check_result", dumps([r.__dict__ for r in rows]),
                                    chk["_file"], location="dimcheck verify results")
                for eid in rid:
                    self.groups.append({"id": eid, "required_ids": [eid], "record_key": None})
                for r in rows:
                    label = f"{chk['_file']} zone {r.zone}, dimension {r.dimension}"
                    if r.verdict.startswith("MISMATCH"):
                        concern = True
                        self.baseline_finding({"severity": "high", "description":
                            f"{label}: checklist {r.existing_tolerance}; drawing {r.resolved_tolerance}"}, cid + did + rid)
                    elif r.source == "unresolved" or r.verdict in ("kept", "not set"):
                        # The row could not be compared with the drawing.
                        concern = True
                        what = ("tolerance cell left empty: the drawing value could not be read"
                                if r.verdict == "not set" else "could not be compared with the drawing")
                        self.issue(f"Checklist row {what}: {label}: {'; '.join(r.notes) or r.verdict}", cid + rid)
                    elif r.verdict == "filled":
                        to_confirm = r.source not in ("explicit", "general_table") or r.confidence != "high"
                        self.warn("tolerance_inserted_confirm" if to_confirm else "tolerance_inserted",
                                  f"{label}: empty cell filled with {r.resolved_tolerance} from the drawing ({r.source}, "
                                  f"{r.confidence} confidence)" + ("; confirm this value" if to_confirm else "") + ".", cid + rid)
                    elif r.source == "assumed":
                        self.warn("tolerance_assumed",
                                  f"{label}: no tolerance printed beside it; general tolerance {r.resolved_tolerance} assumed.", cid + rid)
                self.checklist_status[chk["_file"]] = "concern" if concern else "clean"
                filled = [r for r in rows if r.verdict == "filled"]
                empty = [f"{r.zone} {r.dimension}" for r in rows if r.verdict == "not set"]
                mismatched = [f"{r.zone} {r.dimension}" for r in rows if r.verdict.startswith("MISMATCH")]
                unverified = [f"{r.zone} {r.dimension}" for r in rows if r.verdict == "kept"]
                if not filled:
                    completed.unlink(missing_ok=True)
                self.checklist_results[chk["_file"]] = {
                    "drawing": name, "rows": len(rows), "matched": sum(r.verdict == "match" for r in rows),
                    "inserted": len(filled),
                    "inserted_to_confirm": sum(1 for r in filled if r.source not in ("explicit", "general_table") or r.confidence != "high"),
                    "still_empty": empty, "mismatched": mismatched, "unverified": unverified,
                    "complete": not empty,
                    "completed_copy": str(completed) if filled else None}

            except Exception as exc:
                self.checklist_status[chk["_file"]] = "concern"
                self.issue(f"Checklist could not be verified: {chk['_file']}: {exc}", cid + did)

    def entity_checks(self, entities):
        checker = legacy.coherence()
        records = [{**e, "_file": self.evidence[e["evidence_id"]]["file"]} for e in entities]
        for issue in checker.find_generic_mismatches(checker.group_by_part_number(records)):
            refs = sorted({r["evidence_id"] for r in issue["records"]})
            self.baseline_finding({"description": f"{issue['type']}: {issue['detail']}", "severity": "medium",
                                   "source": "model-derived-rule"}, refs)
            self.add_relationship_review({"relationship": issue["type"], "records": issue["records"]}, refs)
        for ent in entities:
            for key, rec in self.records.items():
                tb = rec["title_block"]
                if checker.alnum(ent["part_number"]) != checker.alnum(tb.get("drawing_number")):
                    continue
                ids = self.record_ids[key]
                for eid in ids:
                    self.link(ent["evidence_id"], eid, "document part number")
                self.add_relationship_review({"relationship": "generic document to drawing", "document_record": ent,
                                              "drawing_title_block": tb}, [ent["evidence_id"]] + ids)
                for left, right in (("part_name", "title"), ("revision", "revision")):
                    if ent.get(left) and tb.get(right) and checker.alnum(ent[left]) != checker.alnum(tb[right]):
                        self.baseline_finding({"severity": "medium", "source": "model-derived-rule", "description":
                            f"Document {left} '{ent[left]}' differs from drawing {right} '{tb[right]}' for {ent['part_number']}."}, [ent["evidence_id"]] + ids)

    def add_relationship_review(self, packet, refs):
        packet = {**packet, "source_ids": refs}
        ids = self.add_text("relationship", dumps(packet), location="document relationship")
        current = {e for group in self.groups for e in group["required_ids"]}
        for eid in ids:
            if eid not in current:
                self.groups.append({"id": eid, "required_ids": [eid], "record_key": None})
            for source in refs:
                self.link(eid, source, "document relationship source")
