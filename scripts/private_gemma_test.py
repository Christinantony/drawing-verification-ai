"""Run uploaded fixtures only inside a private GitHub Actions repository."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time


def validate_execution(outcomes, review, model):
    identity = review.get("model") or {}
    model_ok = (identity.get("name") == model and bool(identity.get("digest"))
                and review.get("supervisor_status") == "completed"
                and bool(review.get("required_ids"))
                and set(review.get("required_ids", [])) == set(review.get("reviewed_ids", [])))
    extraction_ok = not any(u["question"].startswith(("Could not read", "No sheets read", "No text read",
        "Baseline cross-file checks did not complete", "Checklist could not be verified", "Source changed", "Input changed"))
        for u in review.get("unresolved", []))
    passed = outcomes[0]["exit_code"] == 0 and outcomes[1]["exit_code"] in (0, 1, 2) and model_ok and extraction_ok
    return {"execution_passed": passed, "model": identity, "model_check_passed": outcomes[0]["exit_code"] == 0,
                  "supervisor_status": review.get("supervisor_status"), "extraction_passed": extraction_ok,
                  "engineering_coverage": review.get("coverage"), "issue_status": review.get("issue_status"),
                  "required_sections": len(review.get("required_ids", [])), "reviewed_sections": len(review.get("reviewed_ids", [])),
                  "unresolved_count": len(review.get("unresolved", []))}


def main():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if os.environ.get("GITHUB_ACTIONS") != "true" or not event_path:
        raise RuntimeError("This entry point requires private GitHub Actions.")
    event = json.loads(Path(event_path).read_text())
    if event.get("repository", {}).get("private") is not True:
        raise RuntimeError("Refusing to read drawings in a public repository.")
    model = os.environ.get("TEST_MODEL", "gemma4:e4b")
    if model != "gemma4:e4b":
        raise RuntimeError("This validation requires exactly gemma4:e4b.")
    root = Path(__file__).resolve().parents[1]
    source = root / "private-test-inputs"
    files = sorted(p for p in source.glob("*") if p.is_file())
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    docs = [p for p in files if p.suffix.lower() == ".doc"]
    if len(files) != 6 or len(pdfs) != 5 or len(docs) != 1:
        raise RuntimeError("Expected five PDFs and one legacy Word checklist.")
    import fitz
    manifest = []
    for path in files:
        entry = {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if path in pdfs:
            with fitz.open(path) as document:
                entry["pages"] = len(document)
        manifest.append(entry)
    if sum(e.get("pages", 0) for e in manifest) != 5:
        raise RuntimeError("Expected five single-page PDFs in this test set.")
    output = root / "live-results"
    output.mkdir(exist_ok=True)
    (output / "input-manifest.json").write_text(json.dumps(manifest, indent=2))
    outcomes = []
    commands = [
        ("model-check", ["doctor", "--model", model, "--smoke", "--vision", "--seconds", "600", "--context", "16384"]),
        ("drawing-review", ["review", str(source), "--output", str(output / "review"),
                            "--model", model, "--vision", "--seconds", "600", "--context", "16384",
                            "--max-tokens", "2000", "--rounds", "2"]),
    ]
    for name, args in commands:
        start = time.monotonic()
        with (output / (name + ".log")).open("w") as log:
            result = subprocess.run([sys.executable, "-m", "supervisor", *args],
                                    cwd=root, stdout=log, stderr=subprocess.STDOUT)
        outcomes.append({"check": name, "exit_code": result.returncode,
                         "seconds": round(time.monotonic() - start, 2)})
        (output / "execution.json").write_text(json.dumps(outcomes, indent=2))
        print(f"{name}: exit code {result.returncode}", flush=True)
        # A failed smoke test must not be reported as model validation.
        # Still run the review to preserve extraction and failure evidence.
    # Engineering findings and missing reference drawings are expected review
    # results. They must not be confused with a broken model integration.
    latest_path = output / "review" / "latest.json"
    review = {}
    if latest_path.exists():
        latest = json.loads(latest_path.read_text())
        review = json.loads(Path(latest["report"]).with_suffix(".json").read_text())
    validation = validate_execution(outcomes, review, model)
    passed = validation["execution_passed"]
    (output / "validation.json").write_text(json.dumps(validation, indent=2))
    summary = (f"Execution validation: {'PASSED' if passed else 'FAILED'}. Model: `{model}`.\n\n"
               f"Engineering coverage: **{review.get('coverage', 'unavailable')}**; "
               f"issues: **{review.get('issue_status', 'unavailable')}**; "
               f"unresolved checks: **{validation['unresolved_count']}**.\n\n"
               "Execution success does not mean drawing acceptance. Read the private report.\n")
    print(summary, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(summary)
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
