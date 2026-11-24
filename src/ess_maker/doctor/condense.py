"""Shorten instructions toward a character target without losing abilities.

Edits applied automatically: whitespace, a library of reviewed shorter rewordings of ESS instruction lines
(condense_library.json; matched line by line, ignoring case, spacing and contractions), contractions, wordy
phrases and duplicate lines — never inside quoted text, variables or code spans. After each edit the abilities
are compared (routes, topics, quoted replies, variables, headings, ESS core blocks, best practices followed,
validator findings); an edit that would lose any of them is rolled back. If the target still isn't reached, the
largest passages are listed with where they could move (topic descriptions, knowledge source descriptions), so
the maker decides instead of content silently disappearing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ess_maker.doctor import practices
from ess_maker.doctor.agent_file import AgentDoc, from_instructions
from ess_maker.doctor.rules import core_presence, validate

_PROTECTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|\{[^}\n]*\}|`[^`\n]*`')

_CONTRACTIONS = [
    (r"\bdo not\b", "don't"), (r"\bDo not\b", "Don't"), (r"\bdoes not\b", "doesn't"), (r"\bDoes not\b", "Doesn't"),
    (r"\bis not\b", "isn't"), (r"\bare not\b", "aren't"), (r"\bcannot\b", "can't"), (r"\bwill not\b", "won't"),
    (r"\bshould not\b", "shouldn't"), (r"\bdid not\b", "didn't"), (r"\bwas not\b", "wasn't"),
]
_WORDY = [
    (r"\bin order to\b", "to"), (r"\bmake sure (?:that |to )?", "ensure "), (r"\bfor example\b", "e.g."),
    (r"\bFor example\b", "E.g."), (r"\bat this time\b", "now"), (r"\bin the event that\b", "if"),
    (r"\bprior to\b", "before"), (r"\ba number of\b", "several"), (r"\bwith regard to\b", "about"),
    (r"\bas well as\b", "and"), (r"\bplease\s+", ""), (r"\bvery\s+", ""), (r"\breally\s+", ""),
    (r"\bin the case of\b", "for"), (r"\bdue to the fact that\b", "because"), (r"\bis able to\b", "can"),
]


def _outside_protected(text: str, fn) -> str:
    out, pos = [], 0
    for m in _PROTECTED.finditer(text):
        out += [fn(text[pos:m.start()]), m.group(0)]
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def _sub_all(pairs):
    def fn(s: str) -> str:
        for a, b in pairs:
            s = re.sub(a, b, s)
        return s
    return fn


def _whitespace(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            prev = out[-1] if out else ""
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            # Keep one blank line before headings; drop blanks between ordinary lines and list items.
            if not out or not prev.strip() or not nxt.lstrip().startswith("#"):
                continue
        out.append(re.sub(r"(?<=\S) {2,}", " ", ln))
    return "\n".join(out).strip() + "\n"


def _dedupe(text: str) -> str:
    seen, out = set(), []
    for ln in text.splitlines():
        key = re.sub(r"\W+", " ", ln).strip().lower()
        if len(key) > 40 and key in seen:
            continue
        seen.add(key)
        out.append(ln)
    return "\n".join(out) + "\n"


STEPS = [
    ("Remove extra blank lines and spaces", _whitespace),
    ("Use contractions (do not → don't)", lambda t: _outside_protected(t, _sub_all(_CONTRACTIONS))),
    ("Replace wordy phrases (in order to → to)", lambda t: _outside_protected(t, _sub_all(_WORDY))),
    ("Remove duplicate lines", _dedupe),
]

LIBRARY_STEP = "Merge repeated rules (reviewed shorter wording)"
_LIBRARY = Path(__file__).with_name("condense_library.json")


def _library() -> list[dict]:
    return json.loads(_LIBRARY.read_text(encoding="utf-8")) if _LIBRARY.exists() else []


def _apply_rewrite(text: str, old: list[str], new: list[str]) -> str | None:
    """Replace the first run of lines matching `old` (ignoring whitespace, case and contractions) with `new`."""
    lines = text.splitlines()
    want = [_norm(x) for x in old]
    body = [(i, _norm(ln)) for i, ln in enumerate(lines) if ln.strip()]
    for k in range(len(body) - len(want) + 1):
        if [n for _, n in body[k:k + len(want)]] == want:
            start, end = body[k][0], body[k + len(want) - 1][0]
            return "\n".join(lines[:start] + new + lines[end + 1:]) + "\n"
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _sub_all(_CONTRACTIONS + _WORDY)(s)).strip().lower()


def abilities(doc: AgentDoc, baseline: AgentDoc | None) -> dict[str, set]:
    t = doc.instruction_text
    return {
        "quoted replies": {m.strip('"“”') for m in re.findall(r'"[^"\n]{12,}"|“[^”\n]{12,}”', t)},
        "topics/actions routed to": {_norm(x) for x in re.findall(r"->\s*([A-Z][\w/]*(?: [A-Z][\w/]*)*)", t)},
        "routes": {_norm(x) for x in re.findall(r"^\s*-\s*([^:\n]{3,60}):", t, re.M)},
        "variables": set(re.findall(r"\{[^}\n]+\}", t)),
        "sections": {_norm(ln.lstrip("# ")) for ln in t.splitlines() if ln.lstrip().startswith("#")},
        "ESS core blocks": {k for k, v in core_presence(doc).items() if v},
        "best practices followed": {p["id"] for p in practices.check(doc)[1]["followed_list"]},
        "_rules": {f.rule_id for f in validate(doc, baseline)},
    }


def _lost(before: dict, after: dict) -> dict[str, list[str]]:
    lost = {k: sorted(before[k] - after[k]) for k in before if not k.startswith("_") and before[k] - after[k]}
    # Length findings are what condensing fixes, so they never count as a regression.
    new_rules = sorted(r for r in after["_rules"] - before["_rules"] if not r.startswith("L0-"))
    if new_rules:
        lost["new validator findings"] = new_rules
    return lost


def _doc(text: str, like: AgentDoc) -> AgentDoc:
    d = from_instructions(text, like.name + " (condensed)", "condense")
    d.topics, d.globals_defined = like.topics, like.globals_defined
    d.is_full_export, d.content_moderation = like.is_full_export, like.content_moderation
    return d


def _move_hint(text: str) -> tuple[str, str]:
    if len(re.findall(r"->", text)) >= 2 or re.search(r"\b(Get|Update|Create|Add) [A-Z]\w+", text):
        return ("topic descriptions", "Topic descriptions decide when each topic runs; the operation-by-operation "
                "mapping can live there (practice P13) with a one-line pointer left here.")
    if re.search(r"adaptive card|card up", text, re.I):
        return ("topic descriptions", "Card behaviour belongs to the topics that show cards.")
    if re.search(r"knowledge source|sharepoint|\bsite\b", text, re.I):
        return ("knowledge source descriptions", "Source scope is read from knowledge source descriptions first.")
    if re.search(r"bare umbrellas?|e\.g\.|examples?", text, re.I):
        return ("shorter examples", "Keep the rule and trim the example list to the 2-3 most common cases.")
    return ("shorter wording", "Keep the rule; tighten the sentence.")


def studio_len(text: str) -> int:
    """Length as Copilot Studio's instruction counter reports it: each line is a paragraph, so every line
    break counts twice."""
    return len("\n\n".join(text.strip().split("\n")))


def _library_step(text: str, doc: AgentDoc, baseline: AgentDoc | None, before_ab: dict, steps: list,
                  target: int) -> str:
    """Apply each reviewed rewrite that matches, one at a time; roll back any that would lose an ability."""
    start, applied, matched, rolled = studio_len(text), 0, 0, []
    for rw in _library():
        if studio_len(text) <= target:
            break
        new = _apply_rewrite(text, rw["old"], rw["new"])
        if new is None:
            continue
        matched += 1
        lost = _lost(before_ab, abilities(_doc(new, doc), baseline))
        if lost:
            rolled.append(next(iter(lost.values()))[0])
            continue
        text, applied = new, applied + 1
    if not matched:
        steps.append({"step": LIBRARY_STEP, "saved": 0, "applied": False,
                      "note": "no lines match the reviewed wording library"})
    else:
        note = f"{applied} of {matched} matching rewrites applied"
        if rolled:
            note += f"; {len(rolled)} rolled back (would lose {', '.join(rolled[:3])})"
        steps.append({"step": LIBRARY_STEP, "saved": start - studio_len(text), "applied": applied > 0, "note": note})
    return text


def condense(doc: AgentDoc, baseline: AgentDoc | None, target: int) -> dict:
    before_ab = abilities(doc, baseline)
    text, steps = doc.instruction_text.strip() + "\n", []
    for idx, (name, fn) in enumerate(STEPS):
        if idx == 1:
            text = _library_step(text, doc, baseline, before_ab, steps, target)
        new = fn(text)
        saved = studio_len(text) - studio_len(new)
        if saved <= 0:
            steps.append({"step": name, "saved": 0, "applied": False, "note": "nothing to change"})
            continue
        lost = _lost(before_ab, abilities(_doc(new, doc), baseline))
        if lost:
            steps.append({"step": name, "saved": saved, "applied": False,
                          "note": "rolled back: would lose " + "; ".join(f"{k}: {', '.join(v[:3])}" for k, v in lost.items())})
            continue
        steps.append({"step": name, "saved": saved, "applied": True, "note": ""})
        text = new
    final = _doc(text, doc)
    core_lines = {n for ids in core_presence(final).values() for n in ids}
    candidates = []
    if studio_len(text) > target:
        for ln in final.instructions:
            s = ln.text.strip()
            if len(s) < 180 or s.startswith("#"):
                continue
            move_to, why = _move_hint(s)
            keep = ln.no in core_lines
            candidates.append({"line": ln.no, "chars": len(s), "excerpt": s[:160], "move_to": move_to, "why": why,
                               "ess_core": keep})
        candidates.sort(key=lambda c: (c["ess_core"], -c["chars"]))
    need = max(0, studio_len(text) - target)
    movable = sum(max(0, c["chars"] - 90) for c in candidates if not c["ess_core"] and c["move_to"] != "shorter wording")
    return {
        "new_text": text, "steps": steps, "target": target,
        "chars": {"before": studio_len(doc.instruction_text), "after": studio_len(text),
                  "raw_before": len(doc.instruction_text), "raw_after": len(text)},
        "reached": studio_len(text) <= target, "still_needed": need,
        "movable_savings": movable,
        "abilities_kept": {k: len(v) for k, v in before_ab.items() if not k.startswith("_")},
        "candidates": candidates[:8],
    }
