"""The lessons library: what the agent learns from your verdicts.

Every findings.csv you mark (real / false / dont care) is a lesson. Keep the
marked files in one folder (your verdicts folder, e.g. C:\\qc-verdicts) and
the agent uses all of them, in three ways:

1. Silence list (code, exact). A finding you marked false or dont care is
   not reported again when the same check finds the same text in the same
   product family. It is listed under "Silenced by your earlier verdicts".
   Checks that are never dropped (missing title-block fields, wrong
   references, ...) are not silenced; they become please look.
2. Relevant examples (model). Each batch of facts is shown the earlier
   verdicts most like it: same check, similar words, same product family.
3. House rules (model, approved by you). `python -m qc learn` asks the model
   to turn your false verdicts into general rules. You accept, edit or
   reject each one; accepted rules are kept in house_rules.md in the
   verdicts folder and read next to the QC policy on every run.

`python -m qc score` shows, per run, how many reported items were real.

Nothing here is written to the repository: the verdicts folder is yours.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Iterable, List, Optional

from sheetreader.normalize import alnum

SILENCING = ("false", "dont care")
HOUSE_RULES = "house_rules.md"
LEARNT = "learnt.json"


# ---------------------------------------------------------------------------
# Reading verdicts
# ---------------------------------------------------------------------------
def verdict_files(sources: Iterable) -> List[Path]:
    """CSV files from a mix of files and folders, oldest first (so a later
    verdict on the same finding wins)."""
    out = []
    for s in sources or []:
        if not s:
            continue
        p = Path(s)
        if p.is_dir():
            out += [q for q in p.glob("*.csv") if q.is_file()]
        elif p.is_file():
            out.append(p)
    seen, uniq = set(), []
    for p in sorted(out, key=lambda q: (q.stat().st_mtime, q.name)):
        if p.resolve() not in seen:
            seen.add(p.resolve())
            uniq.append(p)
    return uniq


def _norm_verdict(v: str) -> str:
    v = (v or "").strip().lower().replace("'", "").replace("’", "")
    if v in ("real", "true", "yes", "r"):
        return "real"
    if v in ("false", "no", "f", "wrong"):
        return "false"
    if v in ("dont care", "dontcare", "dont-care", "do not care", "dc", "minor"):
        return "dont care"
    return ""


def key(code: str, text: str, rule_finding: str = "") -> str:
    """What makes two findings 'the same': the check and the text on the
    sheet. Findings with no sheet text (a missing title-block field) use the
    rule's own wording, which does not change between runs."""
    body = alnum(text or "") or alnum(rule_finding or "")
    return f"{(code or '').upper()}|{body}"


def read_verdicts(sources) -> List[dict]:
    out = []
    for p in verdict_files(sources):
        try:
            fh = open(p, encoding="utf-8-sig", newline="")
        except OSError:
            continue
        date = _dt.date.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y")
        with fh:
            for row in csv.DictReader(fh):
                v = _norm_verdict(row.get("your_verdict (real / false / dont care)") or row.get("your_verdict") or "")
                if not v:
                    continue
                code = (row.get("check") or "").strip()
                # older files have no rule_finding column: the finding wording
                # is the best we have
                rule_finding = row.get("rule_finding") or row.get("finding", "")
                out.append({
                    "id": row.get("id", ""), "group": row.get("group", ""), "check": code,
                    "check_name": row.get("check_name") or code, "drawing": (row.get("drawing") or "").strip(),
                    "where": row.get("where", ""), "text": row.get("text", ""), "finding": row.get("finding", ""),
                    "rule_finding": rule_finding, "verdict": v, "comment": (row.get("your_comment") or "").strip(),
                    "source": p.name, "date": date,
                    "key": key(code, row.get("text", ""), rule_finding),
                })
    return out


# ---------------------------------------------------------------------------
# 1. Silence list
# ---------------------------------------------------------------------------
def _same_family(v: dict, f, smap) -> bool:
    if f.sheet is None:
        return not v["drawing"]
    if not v["drawing"]:
        return False
    if smap is not None:
        i = smap.family_of(f.sheet)
        if i >= 0:
            return bool(smap.patterns[i].regex.search(v["drawing"]))
    return alnum(v["drawing"]) == alnum(f.sheet.number or "")


def finding_key(f) -> str:
    return key(f.code, f.quote, getattr(f, "rule_message", None) or f.message)


def silence(findings, verdicts, smap, cfg, log=lambda *_: None) -> int:
    """Silence findings you marked false / dont care before. Returns how
    many were silenced. The latest verdict on a finding wins."""
    if not verdicts:
        return 0
    by_key = {}
    for v in verdicts:
        by_key.setdefault(v["key"], []).append(v)
    never = set(cfg.get("never_drop") or [])
    n = 0
    for f in findings:
        if f.status not in ("fail", "check", "doubt"):
            continue
        cands = [v for v in by_key.get(finding_key(f), []) if _same_family(v, f, smap)]
        if not cands:
            continue
        v = cands[-1]
        if v["verdict"] not in SILENCING:
            if not any("you marked it real" in h for h in f.history):
                f.history.append(f"you marked it real on {v['date']} ({v['source']})")
            continue
        why = f"you marked it {v['verdict']} on {v['date']}" + (f": {v['comment']}" if v["comment"] else "") + \
              f" ({v['source']} {v['id']})"
        if f.code in never:
            if not any("never silenced" in h for h in f.history):
                f.severity = "look"
                f.history.append(why + "; this check is never silenced, so it is please look")
            continue
        f.status = "silenced"
        f.silenced_by = why
        f.history.append("silenced: " + why)
        n += 1
    if n:
        log(f"    {n} finding(s) silenced by your earlier verdicts (listed in the report)")
    return n


# ---------------------------------------------------------------------------
# 2. Relevant examples
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[A-Z0-9]{2,}")


def _words(s: str) -> set:
    return set(_WORD.findall((s or "").upper()))


def pick(verdicts, facts=(), sheet=None, smap=None, k=10, codes=None) -> List[dict]:
    """The k earlier verdicts most like these facts (or, for the open review,
    most like this sheet's model observations)."""
    if not verdicts or len(verdicts) <= k:
        return list(verdicts or [])
    codes = set(codes or [f.code for f in facts])
    words = set()
    for f in facts:
        words |= _words(f.message) | _words(f.quote)
    fam = None
    if sheet is not None and smap is not None:
        i = smap.family_of(sheet)
        fam = smap.patterns[i].regex if i >= 0 else None

    def score(v):
        s = 3.0 if v["check"] in codes else 0.0
        vw = _words(v["finding"]) | _words(v["text"])
        if words and vw:
            s += 4.0 * len(words & vw) / len(words | vw)
        if fam is not None and v["drawing"] and fam.search(v["drawing"]):
            s += 1.0
        if v["verdict"] in SILENCING:
            s += 0.5          # what not to report teaches the most
        if v["comment"]:
            s += 0.5
        return s

    ranked = sorted(enumerate(verdicts), key=lambda iv: (-score(iv[1]), -iv[0]))
    uniq, seen = [], set()
    for _, v in ranked:
        sig = (v["check"], v["finding"][:80], v["verdict"])
        if sig not in seen:
            seen.add(sig)
            uniq.append(v)
    out = uniq[:k]
    # Keep a balance: if every example says "false", the model learns to
    # report nothing at all. At least a third are real ones, when there are.
    want = min(k // 3, sum(v["verdict"] == "real" for v in uniq))
    have = sum(v["verdict"] == "real" for v in out)
    if have < want:
        extra = [v for v in uniq[k:] if v["verdict"] == "real"][:want - have]
        keep = [v for v in out if v["verdict"] == "real"]
        rest = [v for v in out if v["verdict"] != "real"][:k - len(keep) - len(extra)]
        out = sorted(keep + rest + extra, key=uniq.index)
    return out


# ---------------------------------------------------------------------------
# 3. House rules
# ---------------------------------------------------------------------------
def house_rules_path(folder) -> Optional[Path]:
    return Path(folder) / HOUSE_RULES if folder else None


def load_house_rules(folder) -> str:
    p = house_rules_path(folder)
    if not p or not p.is_file():
        return ""
    rules = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip().startswith("-")]
    return "\n".join(rules)


def with_house_rules(policy: str, folder) -> str:
    rules = load_house_rules(folder)
    if not rules:
        return policy
    return policy.rstrip() + "\n\n## House rules (approved by the engineer from earlier verdicts)\n" + rules + "\n"


def _seen(folder) -> dict:
    p = Path(folder) / LEARNT
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"done": [], "rejected": []}


def _save_seen(folder, data):
    (Path(folder) / LEARNT).write_text(json.dumps(data, indent=1), encoding="utf-8")


def _vid(v) -> str:
    return f"{v['key']}|{alnum(v['drawing'])}|{v['verdict']}"


def propose(verdicts, client, policy: str, house: str, log, batch=20) -> List[dict]:
    """Ask the model for general rules behind your false verdicts."""
    from .model import ModelError
    proposals = []
    for i in range(0, len(verdicts), batch):
        part = verdicts[i:i + batch]
        lines = "\n".join(f"{n + 1}. [{v['check_name']}] reported: \"{v['finding'][:160]}\""
                          + (f" (text on the drawing: \"{v['text'][:80]}\")" if v["text"] else "")
                          + f" -> engineer: {v['verdict']}" + (f", because: {v['comment'][:160]}" if v["comment"] else "")
                          for n, v in enumerate(part))
        schema = {"type": "object", "properties": {"rules": {"type": "array", "items": {
            "type": "object", "properties": {"rule": {"type": "string"},
                                             "based_on": {"type": "array", "items": {"type": "integer"}}},
            "required": ["rule", "based_on"]}}}, "required": ["rules"]}
        user = (f"QC POLICY (already in force):\n{policy}\n\nHOUSE RULES (already in force):\n{house or 'none'}\n\n"
                f"The QC agent reported these items and the engineer said they were NOT problems:\n{lines}\n\n"
                "Write the general rules that would have stopped these false reports, so the agent does not make "
                "the same kind of mistake again. Each rule: one plain-English sentence of at most 30 words, about a "
                "kind of thing (not one drawing number), never weakening a check for real mistakes, not repeating "
                "the policy or a house rule. Give the numbers of the items each rule is based on. Items with no "
                "general lesson get no rule.")
        try:
            ans = client.ask("You write QC rules for engineering drawings from an engineer's verdicts. "
                             "Answer only in JSON.", user, schema, max_tokens=700)
        except ModelError as e:
            log(f"    model call failed: {e}")
            continue
        for r in ans.get("rules", []):
            idx = [n - 1 for n in r.get("based_on", []) if isinstance(n, int) and 0 < n <= len(part)]
            text = (r.get("rule") or "").strip()
            if text and idx:
                proposals.append({"rule": text, "based_on": [part[j] for j in idx]})
    return proposals


def learn(folder, client, policy: str, log=print, ask=input, out=print) -> int:
    """Interactive: propose house rules from new false verdicts, and keep the
    ones the engineer accepts. Returns the number of rules added."""
    folder = Path(folder)
    if not folder.is_dir():
        out(f"Not a folder: {folder}")
        return 0
    seen = _seen(folder)
    done = set(seen.get("done", []))
    todo = [v for v in read_verdicts([folder]) if v["verdict"] in SILENCING and _vid(v) not in done]
    if not todo:
        out("No new false / dont care verdicts to learn from.")
        return 0
    out(f"{len(todo)} new false / dont care verdict(s). Asking the model for rules...")
    props = propose(todo, client, policy, load_house_rules(folder), log)
    if not props:
        out("The model proposed no rules.")
        return 0
    path = house_rules_path(folder)
    added = 0
    for k, p in enumerate(props, 1):
        out(f"\nRule {k} of {len(props)}:\n  {p['rule']}\n  based on:")
        for v in p["based_on"]:
            out(f"    - {v['drawing']} {v['check']}: {v['finding'][:100]}" + (f"  [{v['comment'][:80]}]" if v["comment"] else ""))
        while True:
            a = (ask("  [a]ccept / [e]dit / [r]eject / [s]kip for now / [q]uit: ") or "").strip().lower()
            if a in ("a", "e", "r", "s", "q"):
                break
        if a == "q":
            break
        if a == "s":
            continue
        rule = p["rule"]
        if a == "e":
            rule = (ask("  your wording: ") or "").strip() or rule
        ids = [_vid(v) for v in p["based_on"]]
        if a == "r":
            seen.setdefault("rejected", []).append(p["rule"])
        else:
            new = not path.exists()
            with open(path, "a", encoding="utf-8") as fh:
                if new:
                    fh.write("# House rules\n\nApproved by the engineer from earlier verdicts. Read next to the QC "
                             "policy on every run. Edit or delete lines freely; one rule per line starting with '-'.\n\n")
                fh.write(f"- {rule}  _(from {len(ids)} verdict(s), {_dt.date.today():%d/%m/%Y})_\n")
            added += 1
        seen["done"] = sorted(done.union(ids))
        done = set(seen["done"])
        _save_seen(folder, seen)
    out(f"\n{added} rule(s) added to {path}" if added else "\nNo rules added.")
    return added


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def score(sources) -> List[dict]:
    """Per verdict file: how many reported items were real, and wrong drops."""
    rows = []
    for p in verdict_files(sources):
        vs = read_verdicts([p])
        if not vs:
            continue
        rep = [v for v in vs if v["group"] in ("must", "look", "check")]
        drops = [v for v in vs if v["group"] in ("dropped", "silenced")]
        per = {}
        for v in rep:
            c = per.setdefault(v["check_name"], [0, 0])
            c[1] += 1
            c[0] += v["verdict"] == "real"
        rows.append({"file": p.name, "judged": len(rep), "real": sum(v["verdict"] == "real" for v in rep),
                     "false": sum(v["verdict"] == "false" for v in rep),
                     "dont_care": sum(v["verdict"] == "dont care" for v in rep),
                     "wrong_drops": sum(v["verdict"] == "real" for v in drops), "per_check": per})
    return rows


def score_text(rows) -> str:
    if not rows:
        return "No marked findings.csv files found."
    out = ["run (verdict file)                         judged  real  false  dont care  real %  wrong drops"]
    for r in rows:
        pct = f"{100 * r['real'] / r['judged']:.0f}%" if r["judged"] else "-"
        out.append(f"{r['file'][:42]:<42} {r['judged']:>6} {r['real']:>5} {r['false']:>6} {r['dont_care']:>10} "
                   f"{pct:>7} {r['wrong_drops']:>12}")
    out.append("\nPer check (all runs): real of reported")
    tot = {}
    for r in rows:
        for name, (real, n) in r["per_check"].items():
            t = tot.setdefault(name, [0, 0])
            t[0] += real
            t[1] += n
    for name, (real, n) in sorted(tot.items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1)):
        out.append(f"  {name[:40]:<40} {real} of {n}")
    return "\n".join(out)
