"""Separate CLI: python -m supervisor review FOLDER or doctor --smoke."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path

from . import __version__
from .controller import Controller
from .model import Ollama
from .report import write
from .store import Store
from .workspace import Workspace


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("Use a positive integer")
    return n


def parser():
    p = argparse.ArgumentParser(description="Drawing Verification AI — Supervisor Edition (experimental)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("review", "plan", "doctor"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--model", default="gemma4:e4b")
        cmd.add_argument("--host", default="http://localhost:11434")
        cmd.add_argument("--seconds", type=positive, default=180, help="Model call / investigation time budget")
        cmd.add_argument("--context", type=positive, default=16384)
        cmd.add_argument("--vision", action="store_true")
        if name in ("review", "plan"):
            cmd.add_argument("input")
            cmd.add_argument("--output", default="supervisor-output")
            cmd.add_argument("--requirements", help="Approved local requirements JSON")
            cmd.add_argument("--all", action="store_true",
                             help="Send every section to the supervisor, not only concerning ones")
        if name == "review":
            cmd.add_argument("--no-model", action="store_true", help="Baseline only; supervisor coverage remains incomplete")
            cmd.add_argument("--rounds", type=int, choices=range(0, 5), default=2)
            cmd.add_argument("--max-tokens", type=positive, default=1000)
            cmd.add_argument("--thinking", choices=("auto", "on", "off"), default="auto")
            cmd.add_argument("--reuse", action="store_true", help="Reuse compatible prior no-tool assessments")
        else:
            cmd.add_argument("--smoke", action="store_true", help="Run text, thinking and protocol checks on the local model")
    return p


def doctor(client, smoke=False, vision=False):
    from .protocol import parse, decode_json
    from .schema import COLOR_SCHEMA
    identity = client.preflight()
    report = {"model": identity, "checks": []}
    if vision and not smoke:
        raise ValueError("Use --smoke with --vision")
    if smoke:
        for thinking in (False, True):
            response, metrics = client.chat([{"role": "user", "content":
                'Return only this JSON object: {"kind":"assessment","reviewed_ids":[],"findings":[],"unresolved":[],"entities":[]}'}],
                thinking=thinking, max_tokens=1200)
            try:
                obj = parse(response)
            except ValueError as exc:
                raise ValueError(f"Text protocol check (thinking={thinking}): {exc}; final response={response[:240]!r}") from exc
            if obj != {"kind": "assessment", "reviewed_ids": [], "findings": [], "unresolved": [], "entities": []}:
                raise ValueError("Protocol smoke check failed")
            report["checks"].append({"thinking": thinking, "result": "passed", "metrics": metrics})
        response, metrics = client.chat([{"role": "user", "content":
            'Return only {"kind":"tools","requests":[{"name":"calculate","arguments":{"operation":"scaled_length","values":[10,2,1],"unit":"mm"}}]}'}])
        if parse(response)["kind"] != "tools":
            raise ValueError("Tool request smoke check failed")
        report["checks"].append({"tool_protocol": "passed", "metrics": metrics})
        if vision:
            if "vision" not in identity["capabilities"]:
                raise ValueError("Model does not advertise vision")
            from PIL import Image, ImageDraw
            image = Image.new("RGB", (500, 200), "white")
            ImageDraw.Draw(image).rectangle((100, 50, 400, 150), fill="red")
            data = io.BytesIO(); image.save(data, format="PNG")
            response, metrics = client.chat([{"role": "user", "content":
                'Identify the rectangle color. Return only a JSON object with one key, color, and the color name as its value.',
                "images": [base64.b64encode(data.getvalue()).decode()]}], max_tokens=100,
                response_schema=COLOR_SCHEMA)
            try:
                color = decode_json(response)
            except ValueError as exc:
                raise ValueError(f"Vision JSON check: {exc}; final response={response[:240]!r}") from exc
            if color != {"color": "red"}:
                raise ValueError("Vision smoke check failed")
            report["checks"].append({"vision": "passed", "metrics": metrics})
    return report


def write_plan(ws, output):
    from .store import dumps
    from .report import plan_lines
    folder = output / "plans"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{ws.run}.json").write_text(dumps({"plan": ws.plan, "warnings": ws.warnings,
                                                    "incomplete": ws.incomplete}), encoding="utf-8")
    path = folder / f"{ws.run}.md"
    path.write_text("\n".join(["# Review plan", ""] + plan_lines(ws.plan, ws.warnings, ws.incomplete)), encoding="utf-8")
    return path


def main(argv=None):
    args = parser().parse_args(argv)
    client, store = None, None
    try:
        if args.command == "doctor":
            client = Ollama(args.host, args.model, args.seconds, args.context)
            print(json.dumps(doctor(client, args.smoke, args.vision), indent=2))
            return 0
        output = Path(args.output).resolve()
        source = Path(args.input).resolve()
        if output == source or output in source.parents:
            raise ValueError("Use a separate output folder")
        store = Store(output / "evidence.sqlite3")
        ws = Workspace(source, store, output, args.requirements)
        ws.prepare()
        from . import triage
        triage.plan(ws, review_all=args.all)
        print(triage.summary(ws, None if args.command == "plan" or args.no_model else args.seconds), flush=True)
        if args.command == "plan":
            path = write_plan(ws, output)
            print(f"Plan written to {path}. No model was used.")
            return 0
        if not args.no_model:
            client = Ollama(args.host, args.model, args.seconds, args.context)
        result = Controller(ws, client, args.rounds, args.seconds, args.vision,
                            args.max_tokens, args.thinking, args.reuse).review()
        report = write(ws, result)
        print(f"Report: {report}\nCoverage: {result['coverage']}; {result['issue_status']}")
        for name, r in sorted(result.get("checklists", {}).items()):
            state = "complete" if r["complete"] else f"{len(r['still_empty'])} cell(s) still empty"
            copy = f" -> {r['completed_copy']}" if r["completed_copy"] else ""
            print(f"Checklist {name}: {r['inserted']} tolerance(s) inserted, {state}{copy}")
        return 2 if result["coverage"] == "incomplete" else (1 if result["issue_status"] == "issues_found" else 0)
    except Exception as exc:
        print(f"Supervisor error: {exc}")
        return 3
    finally:
        if client:
            client.close()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
