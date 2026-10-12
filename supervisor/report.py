"""Deterministic report output with separate issue and coverage states."""
from pathlib import Path

from .store import dumps


def escape(value):
    # Escape HTML and Markdown metacharacters from drawings/model text.
    import html
    text = html.escape(str(value), quote=False)
    for char in ("\\", "`", "*", "_", "[", "]", "#", "|"):
        text = text.replace(char, "\\" + char)
    return text.replace("\n", " ")


def write(workspace, result):
    folder = workspace.output / "runs" / workspace.run
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(dumps(result), encoding="utf-8")
    (folder / "evidence.json").write_text(dumps(list(workspace.evidence.values())), encoding="utf-8")
    lines = ["# Drawing Verification AI — Supervisor Edition", "", "Experimental v0.1.0", "",
             f"Run: `{workspace.run}`", "", f"**Coverage: {result['coverage'].upper()}**", "",
             f"**Findings: {result['issue_status'].replace('_', ' ').upper()}**", "",
             f"Supervisor coverage: {len(result['reviewed_ids'])}/{len(result['required_ids'])} evidence sections.", "",
             "A completed review is limited to the stated scope. It is not drawing approval.", "",
             "## Findings", ""]
    if not result["findings"]:
        lines.append("No issue was found in the completed checks. See incomplete checks below.")
    for i, finding in enumerate(result["findings"], 1):
        lines += [f"### F{i}: {escape(finding['severity'].upper())} — {escape(finding['source'])}", "",
                  escape(finding["claim"]), "", "Action: " + escape(finding["action"]), "",
                  "Evidence: " + ", ".join(f"`{e}`" for e in finding["evidence_ids"]), ""]
        for quote in finding.get("quotes", []):
            lines += [f"Source `{quote['evidence_id']}`: {escape(quote['text'])}", ""]
    lines += ["## Incomplete checks", ""]
    lines += ["- " + escape(u["question"]) + " — " + ", ".join(f"`{e}`" for e in u["evidence_ids"])
              for u in result["unresolved"]] or ["None recorded."]
    lines += ["", "## Review scope", "", result["scope"], "", "Not verified:", ""]
    lines += ["- " + x for x in result["not_verified"]]
    lines += ["", "## Sources", "", "| Evidence | File or source | Location | Method |", "|---|---|---|---|"]
    for e in workspace.evidence.values():
        location = f"page {e['page']}; " if e.get("page") else ""
        location += e.get("location", e["kind"])
        lines.append(f"| `{e['id']}` | {escape(e.get('file') or e.get('source') or 'Tool result')} | "
                     f"{escape(location)} | {escape(e.get('method', e['kind']))} |")
    lines += ["", "The JSON evidence file retains complete values, hashes and tool observations.", ""]
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (workspace.output / "latest.json").write_text(dumps({"run_id": workspace.run,
            "report": str(folder / "report.md")}), encoding="utf-8")
    return folder / "report.md"
