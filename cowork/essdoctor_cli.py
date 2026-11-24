"""ESS Doctor command line, packaged as scripts/essdoctor.pyz in the Copilot Cowork plugin.

Every command prints a Markdown report on stdout and exits 0; problems with the input print a one-line
"ERROR: ..." on stderr and exit 2. Commands that produce new instructions also write them to --out so the
maker can download the file from Cowork's output folder.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from ess_maker.doctor import devui, evaltriage, evidence, practices
from ess_maker.doctor.agent_file import AgentDoc, from_instructions, parse_text
from ess_maker.doctor.blocks import diff_docs
from ess_maker.doctor.condense import condense, studio_len
from ess_maker.doctor.drafter import CHANGE_TYPES, Intake, draft
from ess_maker.doctor.fixes import fix
from ess_maker.doctor.rules import CORE_BLOCKS, DA_INSTRUCTION_LIMIT, core_presence, validate

TEMPLATE_MODERATION = {"hr": "Medium", "it": "Medium", "hub": "High"}


class InputError(Exception):
    """The maker's file or arguments can't be used; the message says what to provide instead."""


# --- Loading ----------------------------------------------------------------------------------------------


def _read(path: str) -> bytes:
    p = Path(path)
    if not p.is_file():
        raise InputError(f"File not found: {path}")
    return p.read_bytes()


def _text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _manifest_instructions(obj) -> tuple[str, str] | None:
    """(name, instructions) from a declarative agent manifest or any JSON that carries an instructions string."""
    if isinstance(obj, dict):
        ins = obj.get("instructions")
        if isinstance(ins, str) and len(ins.strip()) > 40:
            return str(obj.get("name") or obj.get("displayName") or "Agent"), ins
        for v in obj.values():
            r = _manifest_instructions(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _manifest_instructions(v)
            if r:
                return r
    return None


def _from_text(text: str, name: str, source: str) -> AgentDoc | None:
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            found = _manifest_instructions(json.loads(stripped))
        except ValueError:
            found = None
        if found:
            return from_instructions(found[1], found[0], source)
    if re.search(r"(?m)^\s*instructions:\s*[|>]", text):
        doc = parse_text(text, name, source)
        if doc.instructions:
            return doc
    return None


def load_agent(path: str, name: str = "") -> AgentDoc:
    """Agent instructions from pasted text, a Copilot Studio agent YAML, a declarative agent manifest or an export .zip."""
    data = _read(path)
    label = name or Path(path).stem
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            entries = sorted((i for i in z.infolist() if not i.is_dir() and i.file_size < 5_000_000),
                             key=lambda i: (not re.search(r"(?i)declarativeagent|gpt|manifest|agent", i.filename), i.filename))
            for info in entries:
                if not re.search(r"(?i)\.(json|ya?ml|txt|md)$|/data$|^data$", info.filename):
                    continue
                doc = _from_text(_text(z.read(info)), label, f"zip:{info.filename}")
                if doc and doc.instructions:
                    return doc
        raise InputError("No agent instructions found in this .zip. Upload the agent export package, the agent YAML, "
                         "or paste the instructions as text.")
    text = _text(data)
    doc = _from_text(text, label, f"file:{Path(path).name}")
    if doc is None:
        doc = from_instructions(text, label, "pasted")
    if not doc.instructions or not doc.instruction_text.strip():
        raise InputError("The instructions are empty.")
    return doc


def _template_text(persona: str) -> str:
    here = Path(__file__).resolve().parent
    local = here / "templates" / f"template_{persona}.txt"
    if local.is_file():
        return local.read_text(encoding="utf-8")
    import pkgutil

    data = pkgutil.get_data("templates", f"template_{persona}.txt")
    return data.decode("utf-8") if data else ""


def guess_persona(doc: AgentDoc) -> str:
    s = (doc.bot_schema or doc.name or "").lower()
    if s.endswith("it") or "it." in s or s.endswith("dait") or re.search(r"\bit\b", s):
        return "it"
    if "core" in s or "hub" in s:
        return "hub"
    return "hr"


def baseline(persona: str) -> AgentDoc | None:
    if persona == "none":
        return None
    text = _template_text(persona)
    if not text:
        return None
    doc = from_instructions(text, f"ESS template ({persona.upper()})", f"baseline:{persona}")
    doc.content_moderation = TEMPLATE_MODERATION[persona]
    return doc


def _persona(arg: str, doc: AgentDoc) -> str:
    return guess_persona(doc) if arg == "auto" else arg


def _cell(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "\\|").strip()


def _lines(f: dict) -> str:
    return ", ".join(str(n) for n in (f.get("lines") or [])[:6]) or "-"


def _write(path: str, text: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text.rstrip("\n") + "\n", encoding="utf-8")


def _diff_md(diff: dict, limit: int = 60, per_block: int = 5) -> str:
    out, n = [], 0
    for r in diff.get("blocks") or []:
        if r["status"] == "unchanged":
            continue
        out.append(f"- **{r['status']}**: {r['title']}")
        hunks = r.get("hunks") or []
        for h in hunks[:per_block]:
            if n >= limit:
                break
            out.append(f"  - `{h['op']}` line {h.get('line', '?')}: {_cell(h['text'])[:220]}")
            n += 1
        if len(hunks) > per_block:
            out.append(f"  - … {len(hunks) - per_block} more line(s)")
    if diff.get("moderation_changed"):
        out.append(f"- **content moderation**: {diff['old'].get('content_moderation') or 'not set'} → "
                   f"{diff['new'].get('content_moderation') or 'not set'}")
    if diff.get("char_delta"):
        out.append(f"- Length changed by {diff['char_delta']:+,} characters.")
    return "\n".join(out) if out else "No section changed."


# --- Commands ---------------------------------------------------------------------------------------------


def cmd_check(a) -> str:
    doc = load_agent(a.instructions)
    persona = _persona(a.persona, doc)
    base = baseline(persona)
    findings = [f.to_dict() for f in validate(doc, base)]
    gaps, card = practices.check(doc)
    base_gaps = set(practices.check(base)[1]["gaps"]) if base else set()
    presence = core_presence(doc)
    raw, studio = len(doc.instruction_text), studio_len(doc.instruction_text)
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low")}
    maker = sum(1 for f in findings if f["origin"] != "template")
    missing = [b["title"] for b in CORE_BLOCKS if not presence[b["id"]]]
    who = f"the ESS {persona.upper()} template" if base else "the ESS rules (no template baseline)"
    md = ["## ESS Doctor check", "",
          f"Checked against {who}: {counts['high']} high, {counts['medium']} medium, {counts['low']} low "
          f"({maker} from your instructions, {len(findings) - maker} also in the ESS template). "
          f"Follows {card['followed']} of {card['applicable']} applicable authoring practices. "
          f"Length: {studio:,} characters as Copilot Studio counts them ({raw:,} raw) of the {DA_INSTRUCTION_LIMIT:,} limit. "
          + ("All ESS core blocks are present." if not missing else f"Missing ESS core blocks: {', '.join(missing)}."), ""]
    if a.previous:
        prev = load_agent(a.previous, "Previous version")
        prev_keys = {(f.rule_id, f.current) for f in validate(prev, base)}
        new = [f for f in findings if (f["rule_id"], f["current"]) not in prev_keys]
        md += ["### What changed since the previous version", "", _diff_md(diff_docs(prev, doc)), ""]
        md += ["### Findings introduced by this change", ""]
        md += [f"- **{f['severity']}** {f['rule_id']} (line {_lines(f)}): {_cell(f['title'])}. Users see: {_cell(f['user_sees'])}"
               for f in new] or ["- None. The findings below were already present in the previous version."]
        md.append("")
    if findings:
        md += ["### Findings", "", "| Severity | Rule | Lines | What's wrong | Users see | Fix |", "| --- | --- | --- | --- | --- | --- |"]
        for f in findings:
            origin = " (also in template)" if f["origin"] == "template" else ""
            md.append(f"| {f['severity']}{origin} | {f['rule_id']} | {_lines(f)} | {_cell(f['title'])} | {_cell(f['user_sees'])} | {_cell(f['fix'])} |")
        md.append("")
        tests = sorted({p for f in findings if f["origin"] != "template" for p in f.get("test_prompts") or []})
        if tests:
            md += ["### Questions to test after fixing", ""] + [f"- {t}" for t in tests[:12]] + [""]
    else:
        md += ["No rule findings.", ""]
    md += ["### ESS core blocks", ""]
    md += [f"- {b['title']}: present (line {presence[b['id']][0]})" if presence[b["id"]] else f"- {b['title']}: **missing**"
           for b in CORE_BLOCKS]
    md += ["", f"### Authoring practices ({card['followed']} of {card['applicable']} followed)", ""]
    for g in gaps:
        d = g.to_dict()
        tg = " (also a gap in the ESS template)" if d["rule_id"] in base_gaps else ""
        md.append(f"- {d['rule_id'][3:]} {_cell(d['title'].replace('Not following: ', ''))}{tg}: {_cell(d['fix'])}")
    if not gaps:
        md.append("- All applicable practices are followed.")
    if a.json:
        _write(a.json, json.dumps({"persona": persona, "findings": findings, "practices": card,
                                   "chars": {"raw": raw, "studio": studio, "limit": DA_INSTRUCTION_LIMIT}}, indent=2, default=list))
    return "\n".join(md)


def cmd_fix(a) -> str:
    doc = load_agent(a.instructions)
    base = baseline(_persona(a.persona, doc))
    rule_ids = [r.strip() for r in a.rules.split(",") if r.strip()] if a.rules else None
    r = fix(doc, base, rule_ids)
    _write(a.out, r["new_instructions"])
    c = r["chars"]
    md = ["## ESS Doctor fix", "",
          f"Applied {len(r['applied'])} fix(es); {len(r['manual'])} need a manual change. Maker findings: "
          f"{r['counts']['before']} → {r['counts']['after']}. Length {c['before']:,} → {c['after']:,} of {c['limit']:,}.",
          f"The fixed instructions are in `{a.out}`.", ""]
    if r["applied"]:
        md += ["### Applied", ""] + [f"- {x['rule_id']} (line {_lines(x)}): {_cell(x['change'])}" for x in r["applied"]] + [""]
    if r["manual"]:
        md += ["### Needs your decision", ""] + [f"- {x['rule_id']} (line {_lines(x)}) {_cell(x['title'])}: {_cell(x['change'])}" for x in r["manual"]] + [""]
    md += ["### What changed", "", _diff_md(r["diff"]), ""]
    if r["remaining"]:
        md += ["### Still open", ""] + [f"- **{x['severity']}** {x['rule_id']}: {_cell(x['title'])}" for x in r["remaining"][:10]] + [""]
    if r["tests"]:
        md += ["### Re-test with", ""] + [f"- {t}" for t in r["tests"][:10]]
    return "\n".join(md)


def cmd_condense(a) -> str:
    doc = load_agent(a.instructions)
    r = condense(doc, baseline(_persona(a.persona, doc)), a.target)
    _write(a.out, r["new_text"])
    c = r["chars"]
    md = ["## ESS Doctor shorten", "",
          f"Length as Copilot Studio counts it: {c['before']:,} → {c['after']:,} (target {r['target']:,}). "
          + ("Target reached." if r["reached"] else f"Still {r['still_needed']:,} characters over; see the candidates below."),
          f"The shortened instructions are in `{a.out}`.", ""]
    if r["steps"]:
        md += ["### What was shortened", ""] + [f"- {_cell(s if isinstance(s, str) else s.get('what') or s)}" for s in r["steps"][:20]] + [""]
    if r["abilities_kept"]:
        md += ["### Abilities kept", "", ", ".join(f"{k}: {v}" for k, v in r["abilities_kept"].items()), ""]
    if not r["reached"] and r["candidates"]:
        md += ["### Candidates to cut or move into topics or knowledge", ""] + [f"- {_cell(x if isinstance(x, str) else json.dumps(x))[:300]}" for x in r["candidates"]]
    return "\n".join(md)


def _run_sync(coro):
    """Run a coroutine that never suspends (the drafter without an LLM) without creating an event loop.

    Some sandboxes block the local socket pair asyncio needs for its loop, so asyncio.run would hang.
    """
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("The drafter tried to wait on I/O; the Cowork build runs without an LLM.")


def cmd_draft(a) -> str:
    doc = load_agent(a.instructions)
    base = baseline(_persona(a.persona, doc))
    fields = json.loads(_text(_read(a.intake))) if a.intake else {}
    fields.setdefault("change_type", a.type)
    fields.setdefault("intent", a.intent or "")
    if fields["change_type"] not in CHANGE_TYPES:
        raise InputError(f"--type must be one of: {', '.join(CHANGE_TYPES)}")
    known = set(Intake.__dataclass_fields__)
    intake = Intake(**{k: v for k, v in fields.items() if k in known})
    r = _run_sync(draft(intake, doc, base, use_llm=False))
    if r.get("needs_input"):
        md = ["## ESS Doctor draft: more detail needed", ""]
        md += [f"- {_cell(q if isinstance(q, str) else q.get('question') or q)}" for q in r.get("questions") or r.get("needs_input") or []]
        return "\n".join(md)
    _write(a.out, r["new_instructions"])
    md = ["## ESS Doctor draft", "", _cell(r.get("summary") or f"Drafted a {CHANGE_TYPES[intake.change_type]} change."),
          f"The new instructions are in `{a.out}`.", ""]
    if r.get("clause"):
        md += ["### Added text", "", "```text", r["clause"].strip(), "```", ""]
    if r.get("diff"):
        md += ["### What changed", "", _diff_md(r["diff"]), ""]
    for key, title in (("new_findings", "New findings this change introduces"), ("conflicts", "Conflicts"),
                       ("warnings", "Warnings")):
        items = r.get(key) or []
        if items:
            md += [f"### {title}", ""] + [f"- {_cell(x if isinstance(x, str) else x.get('title') or x.get('message') or json.dumps(x))}" for x in items] + [""]
    tests = r.get("tests") or r.get("test_prompts") or []
    if tests:
        md += ["### Test with", ""] + [f"- {_cell(t if isinstance(t, str) else t.get('prompt') or t)}" for t in tests[:12]]
    return "\n".join(md)


def cmd_xray(a) -> str:
    data = _read(a.evidence)
    text = _text(data).lstrip("\ufeff")
    current = load_agent(a.instructions, "Current instructions") if a.instructions else None
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and "requests" in obj and "messages" in obj:
            parsed = devui.parse(obj)
            if not parsed["turns"]:
                raise InputError("The DevUI export has no user turns to analyze (only commands such as /debug).")
            turns = parsed["turns"]
            idx = len(turns) - 1
            if a.question:
                idx = next((i for i in range(len(turns) - 1, -1, -1) if a.question.lower() in turns[i]["question"].lower()), idx)
            if a.turn:
                idx = max(0, min(a.turn - 1, len(turns) - 1))
            t = turns[idx]
            served = from_instructions(t["instructions"], "Instructions that ran (DevUI)", "devui") if t["instructions"] else None
            basis = served or current
            findings = validate(basis, baseline(guess_persona(current or basis))) if basis else []
            head = ""
            if len(turns) > 1:
                head = "Turns in this export: " + "; ".join(f"{i + 1}. {x['question'][:80]}" for i, x in enumerate(turns)) + \
                       f"\nAnalyzing turn {idx + 1}. Pass --turn N for another.\n\n"
            report = devui.build(parsed, idx, current=current, findings=findings).replace(
                "Apply these in the Agent check-up (or copy the fixed instructions into Copilot Studio)",
                "Ask ESS Doctor to apply these fixes (or edit the instructions in Copilot Studio > Overview > Instructions)")
            return head + "```text\n" + report + "\n```"
    if re.search(r'(^|,|")\s*AuditData\s*("|,|$)', text.splitlines()[0] if text else "", re.I):
        r = evidence.parse_purview_csv(text, a.agent or "")
        if not r.get("ok"):
            raise InputError(r.get("error", "Couldn't read the Purview export."))
        md = ["## Purview audit: Copilot interactions", "", f"{r['rows']} rows ({r['skipped']} skipped).", "",
              "### Agents", ""] + [f"- {n}: {c} interactions" for n, c in r["agents"]] + ["", "### By agent version", ""]
        for v in r["versions"]:
            md.append(f"- **{_cell(v.get('agent') or '')} {v.get('version', '')}**: {v.get('interactions')} interactions, "
                      f"{v.get('first')} → {v.get('last')}; resources: {_cell(v.get('resources'))[:300]}; plugins: {_cell(v.get('plugins'))[:200]}")
        md += ["", "Compare when each version started with when the instructions changed, then upload a DevUI export of a "
                   "failing question for a turn-by-turn X-ray."]
        return "\n".join(md)
    from ess_maker.doctor import devmode, devmode_report

    if not devmode.looks_like_devmode(text):
        raise InputError("Couldn't recognise this evidence. Upload a DevUI conversation export (JSON), a Purview audit "
                         "export (CSV with an AuditData column), or paste the developer mode (-developer on) card text.")
    return "```text\n" + devmode_report.build(text, question=a.question, response=a.response, current=current) + "\n```"


def _evidence_files(paths: list[str]) -> list[tuple[str, bytes]]:
    out = []
    for path in paths:
        data = _read(path)
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                out += [(f"{Path(path).name}/{i.filename}", z.read(i)) for i in z.infolist()
                        if not i.is_dir() and re.search(r"(?i)\.(json|txt|md)$", i.filename)]
        else:
            out.append((Path(path).name, data))
    return out


def cmd_sorry(a) -> str:
    from ess_maker.doctor import devmode, sorry

    sources, skipped = [], []
    for name, data in _evidence_files(a.evidence):
        text = _text(data).lstrip("\ufeff")
        obj = None
        if text.lstrip().startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
        if isinstance(obj, dict) and "requests" in obj and "messages" in obj:
            sources.append(("devui", name, devui.parse(obj)))
        elif devmode.looks_like_devmode(text):
            q = re.search(r"(?im)^\s*(?:question|user question|prompt)\s*[:=]\s*(.+)$", text)
            r = re.search(r"(?im)^\s*(?:response|answer|agent response|agent said)\s*[:=]\s*(.+)$", text)
            q, r = (q.group(1).strip() if q else ""), (r.group(1).strip() if r else "")
            sources.append(("devmode", name, (devmode.diagnose(text, q, r), q, r)))
        else:
            skipped.append(name)
    if not sources:
        raise InputError("No conversation evidence found. Upload DevUI conversation exports (JSON) or developer mode cards "
                         "(text), one file per conversation or several in a .zip.")
    instructions = load_agent(a.instructions).instruction_text if a.instructions else ""
    md = sorry.build(sorry.triage(sources, instructions))
    if skipped:
        md += "\n\nSkipped (not a DevUI export or developer mode card): " + ", ".join(skipped[:20])
    return md


def cmd_eval_triage(a) -> str:
    doc = load_agent(a.instructions)
    r = evaltriage.triage(_text(_read(a.csv)), doc)
    if not r.get("has_results", True) or not r.get("total"):
        raise InputError("No evaluation results found. Upload the results CSV exported from the agent's Evaluation page.")
    return "## Evaluation triage\n\n" + (r.get("summary") or "").strip() + "\n\n" + _triage_table(r)


def _triage_table(r: dict) -> str:
    items = r.get("items") or []
    if not items:
        return "No failed questions."
    md = ["### Failed questions", "", "| Question | What happened | Who fixes it | Fix |", "| --- | --- | --- | --- |"]
    for x in items:
        md.append(f"| {_cell(x.get('question'))[:160]} | {_cell(x.get('headline'))[:220]} | {_cell(x.get('owner'))} | {_cell(x.get('fix'))[:260]} |")
    return "\n".join(md)


def cmd_eval_compare(a) -> str:
    r = evaltriage.compare(_text(_read(a.before)), _text(_read(a.after)))
    if not r["rows"]:
        raise InputError("No questions found. Upload two Copilot Studio evaluation results CSVs.")
    counts = ", ".join(f"{k} {v}" for k, v in r["counts"].items() if v)
    md = ["## Evaluation comparison", "",
          f"{r['total']} questions: {r['before_pass']} passed before, {r['after_pass']} pass after ({counts}).", "",
          "| Question | Status | Before | After |", "| --- | --- | --- | --- |"]
    order = {"Regressed": 0, "Still failing": 1, "Fixed": 2, "Not comparable": 3, "Still passing": 4}
    for x in sorted(r["rows"], key=lambda x: order.get(x.get("status"), 9)):
        b, af = x.get("before") or {}, x.get("after") or {}
        mark = lambda s: ("pass" if s.get("pass") else "fail") + (f": {s.get('why')}" if s.get("why") else "")
        md.append(f"| {_cell(x.get('question'))[:160]} | **{_cell(x.get('status'))}** | {_cell(mark(b))} | {_cell(mark(af))} |")
    if r["counts"].get("Regressed"):
        md += ["", "Regressed questions passed before the change and fail now: check the instruction lines the change added."]
    return "\n".join(md)


def cmd_setup(a) -> str:
    from ess_maker.doctor.setup_servicenow import run

    answers = _text(_read(a.answers)) if a.answers else "{}"
    return run(answers)["markdown"]


def cmd_practices(a) -> str:
    md = ["## Authoring practices ESS Doctor checks", ""]
    for p in practices.PRACTICES:
        md.append(f"- **{p.id}** {_cell(p.title)}")
    return "\n".join(md)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="essdoctor", description="ESS Doctor: check, fix and troubleshoot Employee Self-Service agents.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    persona = {"choices": ["auto", "hr", "it", "hub", "none"], "default": "auto", "help": "ESS template to compare with"}

    p = sub.add_parser("check", help="Check instructions against the ESS template, known issues and practices")
    p.add_argument("instructions")
    p.add_argument("--previous", help="Previous version, to show what the change introduced")
    p.add_argument("--persona", **persona)
    p.add_argument("--json", help="Also write the full result as JSON to this path")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("fix", help="Apply the safe fixes and write the fixed instructions")
    p.add_argument("instructions")
    p.add_argument("--rules", help="Comma-separated rule IDs to fix (default: all auto-fixable)")
    p.add_argument("--persona", **persona)
    p.add_argument("--out", default="outputs/fixed_instructions.txt")
    p.set_defaults(fn=cmd_fix)

    p = sub.add_parser("condense", help="Shorten instructions to a character target without losing abilities")
    p.add_argument("instructions")
    p.add_argument("--target", type=int, default=DA_INSTRUCTION_LIMIT)
    p.add_argument("--persona", **persona)
    p.add_argument("--out", default="outputs/shortened_instructions.txt")
    p.set_defaults(fn=cmd_condense)

    p = sub.add_parser("draft", help="Draft an instruction change from the maker's intent")
    p.add_argument("instructions")
    p.add_argument("--type", choices=list(CHANGE_TYPES), default="restricted")
    p.add_argument("--intent", default="")
    p.add_argument("--intake", help="JSON file with the change details (subject, allowed, terms, format_spec, ...)")
    p.add_argument("--persona", **persona)
    p.add_argument("--out", default="outputs/drafted_instructions.txt")
    p.set_defaults(fn=cmd_draft)

    p = sub.add_parser("xray", help="Explain one conversation from a DevUI export, Purview export or developer mode card")
    p.add_argument("evidence")
    p.add_argument("--instructions", help="Current instructions, to detect whether the conversation ran on another version")
    p.add_argument("--question", default="", help="Pick the turn whose question contains this text")
    p.add_argument("--turn", type=int, default=0, help="1-based turn number")
    p.add_argument("--agent", default="", help="Purview: only this agent name or ID")
    p.add_argument("--response", default="", help="Developer mode card: the agent's answer, to check the answer stage")
    p.set_defaults(fn=cmd_xray)

    p = sub.add_parser("sorry-triage", help="Why the agent said sorry across many conversations (DevUI exports, developer mode cards)")
    p.add_argument("evidence", nargs="+", help="DevUI exports (.json), developer mode cards (.txt) or a .zip of them")
    p.add_argument("--instructions", help="Current instructions, to show which implicated rules are already fixed")
    p.set_defaults(fn=cmd_sorry)

    p = sub.add_parser("eval-triage", help="Explain why each failed evaluation question failed")
    p.add_argument("csv")
    p.add_argument("--instructions", required=True)
    p.set_defaults(fn=cmd_eval_triage)

    p = sub.add_parser("eval-compare", help="Compare two evaluation runs of the same questions")
    p.add_argument("before")
    p.add_argument("after")
    p.set_defaults(fn=cmd_eval_compare)

    p = sub.add_parser("setup-servicenow", help="ServiceNow Knowledge connector setup checklist")
    p.add_argument("--answers", help="JSON file with the answers collected so far")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("practices", help="List the authoring practices that are checked")
    p.set_defaults(fn=cmd_practices)

    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    try:
        print(a.fn(a))
        return 0
    except InputError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
