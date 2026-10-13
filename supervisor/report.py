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


def plan_lines(plan, warnings, incomplete=()):
    """Inventory, what the supervisor reviewed and skipped, and warnings."""
    lines = []
    if plan:
        lines += ["## Files and plan", "", "| File | Status |", "|---|---|"]
        lines += [f"| {escape(name)} | {escape(state)} |" for name, state in sorted(plan["files"].items())]
        lines += ["", f"Drawing sheets read: {plan['drawings']}. Checklists: {plan['checklists']}.", ""]
        lines += [f"Reviewed by the supervisor ({len(plan['planned'])}):", ""]
        lines += [f"- {escape(x['label'])}: {escape('; '.join(x['reasons']))}" for x in plan["planned"]] or ["- Nothing concerning."]
        lines += ["", f"Skipped as not concerning ({len(plan['skipped'])}):", ""]
        lines += [f"- {escape(x['label'])}: {escape(x['reason'])}" for x in plan["skipped"]] or ["- None."]
        lines.append("")
    lines += ["## Warnings", "", "Not checked, or checked with an assumption. Warnings do not make a review incomplete.", ""]
    lines += [f"- **{escape(w['code'])}**: {escape(w['message'])}" for w in warnings] or ["- None."]
    if incomplete:
        lines += ["", "## Checks that could not run", ""]
        lines += [f"- {escape(u['question'])}" for u in incomplete]
    lines.append("")
    return lines


def checklist_lines(results):
    if not results:
        return []
    lines = ["## Checklists", "",
             "Empty tolerance cells are filled in a copy; the source checklist is not changed. "
             "No rows are added and existing values are never changed. "
             "In the copy, yellow = read from the drawing; orange = assumed or found near its zone, confirm it.", "",
             "| Checklist | Rows | Matched | Inserted | To confirm | Mismatch | Still empty | Complete | Completed copy |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, r in sorted(results.items()):
        copy = escape(r["completed_copy"]) if r["completed_copy"] else "no empty cells"
        lines.append(f"| {escape(name)} | {r['rows']} | {r['matched']} | {r['inserted']} | {r['inserted_to_confirm']} | "
                     f"{len(r['mismatched'])} | {len(r['still_empty'])} | {'yes' if r['complete'] else 'NO'} | {copy} |")
    for name, r in sorted(results.items()):
        if r["still_empty"]:
            lines.append(f"\n{escape(name)}: still empty: {escape(', '.join(r['still_empty']))}. Fill these by hand.")
        if r["mismatched"]:
            lines.append(f"\n{escape(name)}: values that disagree with the drawing (not changed): {escape(', '.join(r['mismatched']))}.")
    lines.append("")
    return lines


def write(workspace, result):
    folder = workspace.output / "runs" / workspace.run
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(dumps(result), encoding="utf-8")
    (folder / "evidence.json").write_text(dumps(list(workspace.evidence.values())), encoding="utf-8")
    from . import __version__
    lines = ["# Drawing Verification AI — Supervisor Edition", "", f"Experimental v{__version__}", "",
             f"Run: `{workspace.run}`", "", f"**Coverage: {result['coverage'].upper()}**", "",
             f"**Findings: {result['issue_status'].replace('_', ' ').upper()}**", "",
             f"Supervisor coverage: {len(result['reviewed_ids'])}/{len(result['required_ids'])} planned evidence sections; "
             f"{len(result.get('skipped', []))} section group(s) skipped as not concerning; {len(result.get('warnings', []))} warning(s).", "",
             "A completed review is limited to the stated scope. It is not drawing approval.", "",
             "## Findings", ""]
    if not result["findings"]:
        lines.append("No issue was found in the completed checks. See warnings and incomplete checks below.")
    for i, finding in enumerate(result["findings"], 1):
        lines += [f"### F{i}: {escape(finding['severity'].upper())} — {escape(finding['source'])}", "",
                  escape(finding["claim"]), "", "Action: " + escape(finding["action"]), "",
                  "Evidence: " + ", ".join(f"`{e}`" for e in finding["evidence_ids"]), ""]
        for quote in finding.get("quotes", []):
            lines += [f"Source `{quote['evidence_id']}`: {escape(quote['text'])}", ""]
    lines += checklist_lines(result.get("checklists") or {})
    lines += plan_lines(result.get("plan"), result.get("warnings", []))
    lines += ["## Incomplete checks", "", "Planned checks that could not be completed. Any entry here makes coverage incomplete.", ""]
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
