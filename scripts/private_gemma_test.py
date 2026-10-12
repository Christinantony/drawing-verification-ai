"""Run uploaded fixtures only inside a private GitHub Actions repository."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time


def main():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if os.environ.get("GITHUB_ACTIONS") != "true" or not event_path:
        raise RuntimeError("This entry point requires private GitHub Actions.")
    event = json.loads(Path(event_path).read_text())
    if event.get("repository", {}).get("private") is not True:
        raise RuntimeError("Refusing to read drawings in a public repository.")
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
        ("model-check", ["doctor", "--smoke", "--vision", "--seconds", "600", "--context", "8192"]),
        ("drawing-review", ["review", str(source), "--output", str(output / "review"),
                            "--vision", "--seconds", "600", "--context", "8192",
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
    summary = ("Private drawing test finished. See the private artifact.\n"
               "Review exit codes: 0 = complete, no issue found; 1 = complete, issues found; "
               "2 = incomplete; 3 = execution error. This is not engineering approval.\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(summary)
    return int(outcomes[0]["exit_code"] != 0 or outcomes[1]["exit_code"] not in (0, 1))


if __name__ == "__main__":
    raise SystemExit(main())
