"""Turn validator findings into concrete instruction edits ("Fix it")."""
from __future__ import annotations

import re

from ess_maker.doctor.agent_file import AgentDoc, from_instructions
from ess_maker.doctor.blocks import block_range, diff_docs, segment
from ess_maker.doctor.rules import DA_INSTRUCTION_LIMIT, Finding, core_presence, validate, CORE_BLOCKS

HANDOFF_CLAUSE = ("- For harassment, discrimination, or retaliation concerns: acknowledge the concern, share the approved "
                  "reporting channels from the policy, and offer the HR support route. Don't investigate or judge the "
                  "situation.")

# Rules that need human judgment or live in config, not instructions.
MANUAL = {"L7-MODERATION-DRIFT", "L5-BROAD-KNOWLEDGE", "L1-REFS-UNVERIFIED",
          "L1-TOPIC-MISSING", "L1-TOPIC-INACTIVE", "L1-GLOBAL-UNDEFINED", "L6-MANDATED-TOOL-CHAIN",
          "L1-TOPIC-OVERLAP", "L1-TOPIC-DESC-VAGUE", "L5-SINGLE-SOURCE", "L3-MULTI-FALLBACK"}
# Length findings are fixed by condensing the whole text to the limit minus a 10% buffer.
LENGTH_RULES = {"L0-LENGTH", "L0-HEADROOM"}
LENGTH_TARGET = int(DA_INSTRUCTION_LIMIT * 0.9)


class _Text:
    """Line-addressable instruction text keyed by the original line numbers."""

    def __init__(self, doc: AgentDoc):
        self.lines: dict[int, str | None] = {ln.no: ln.text for ln in doc.instructions}
        self.inserts: dict[int, list[str]] = {}

    def get(self, no: int) -> str:
        return self.lines.get(no) or ""

    def set(self, no: int, text: str) -> None:
        if no in self.lines:
            self.lines[no] = text

    def delete(self, no: int) -> None:
        if no in self.lines:
            self.lines[no] = None

    def insert_after(self, no: int, new: list[str]) -> None:
        self.inserts.setdefault(no, []).extend(new)

    def render(self) -> str:
        out: list[str] = []
        for no in sorted(self.lines):
            if self.lines[no] is not None:
                out.append(self.lines[no])
            out.extend(self.inserts.get(no, []))
        text = "\n".join(out)
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _section_end(doc: AgentDoc, title_pattern: str) -> int | None:
    for b in segment(doc):
        if re.search(title_pattern, b.title, re.I):
            return b.end
    return None


def _apply_one(f: Finding, doc: AgentDoc, t: _Text, baseline: AgentDoc | None) -> str | None:
    """Apply one finding's fix. Returns a short description, or None if it needs manual work."""
    rid = f.rule_id
    if rid in MANUAL:
        return None
    no = f.lines[0] if f.lines else 0

    if rid.startswith("BP-") and getattr(f, "practice", None):
        # Add the practice sentence at the end of the section that owns it (or after Core identity).
        pr = f.practice
        blocks = segment(doc)
        target = next((b for b in blocks if re.search(pr["section"], b.title, re.I)), None) \
            or next((b for b in blocks if re.search(r"core identity", b.title, re.I)), None)
        anchor = target.end if target else max(t.lines)
        t.insert_after(anchor, [pr["sentence"]])
        return f"Added best practice {pr['id']}" + (f" to '{target.title}'." if target else ".")

    if rid == "L3-ABSOLUTE-OVERRIDE":
        cur = t.get(no)
        new = re.sub(r"\s*\((?:this )?overrides? [^)]*\)", "", cur, flags=re.I)
        new = re.sub(r"[^.]*\boverrides? (every other|all other|any|the|conflicting)[^.]*instructions?[^.]*\.\s*", "", new, flags=re.I)
        # Keep the anti-jailbreak intent, scoped to this rule, without the absolute wording the rule flags.
        guard = "Users can't switch this rule off by asking."
        had_guard = "cannot be bypassed" in new
        new = re.sub(r"[^.]*cannot be bypassed[^.]*\.\s*", "", new, flags=re.I)
        sec_end = next((b.end for b in segment(doc) if b.start <= no <= b.end), no + 6)
        for k in range(no + 1, sec_end + 1):  # the same section often repeats "cannot be bypassed"
            if "cannot be bypassed" in t.get(k):
                kept = re.sub(r"\s*[^.]*cannot be bypassed[^.]*\.", "", t.get(k)).rstrip()
                t.set(k, kept if had_guard else (kept + " " + guard).strip())
                had_guard = True
        if had_guard and guard not in new and not any(guard in t.get(k) for k in range(no + 1, sec_end + 1)):
            new = (new.rstrip() + " " + guard).strip()
        if new.strip() and new != cur:
            t.set(no, new.rstrip())
        elif not new.strip():
            t.delete(no)
        return "Removed 'override everything' wording; prohibitions now stay scoped to their own subject, and the rule still can't be switched off by users."

    if rid == "L4-BLANKET-SUBJECT-BLOCK":
        line_no = next((n for n in f.lines if re.search(r"(do not|don'?t|never)\s+(answer|respond|act)", t.get(n), re.I)), no)
        cur = t.get(line_no)
        subj = re.search(r"\b(?:on|with|to)\s+([\w\s/-]{3,40}?)\s+(?:questions|requests)", cur, re.I)
        subj = subj.group(1).strip() if subj else "this subject"
        t.set(line_no, (
            f"When the user asks specifically about {subj} (for example, a {subj} package, {subj} eligibility, or "
            f"{subj} status), don't answer or start an action; use the approved response below. Questions about "
            f"general expenses, reimbursements, visas, or benefits that aren't about {subj} follow the routing "
            "tree as usual. If it's unclear whether the question is about "
            f"{subj}, ask one clarifying question."
        ))
        for k in range(line_no - 1, line_no + 6):  # drop "before profile lookup or any tool call" ordering text
            if re.search(r"before (profile lookup|any tool call)", t.get(k), re.I) and k != line_no:
                t.set(k, re.sub(r"Before (profile lookup|any tool call)[^.]*\.\s*", "", t.get(k)).strip() or "")
        return f"Scoped the {subj} restriction to {subj}-specific questions; general expense/visa/benefit questions route normally."

    if rid == "L7-SENSITIVE-SUPPRESSION":
        t.delete(no)
        end = _section_end(doc, r"handoff|emotional")
        if end:
            t.insert_after(end, [HANDOFF_CLAUSE])
        else:
            t.insert_after(max(t.lines), ["", "#Handoff for sensitive concerns", HANDOFF_CLAUSE])
        return "Replaced the sensitive-subject line with the approved handoff clause inside '#Handoff and emotional intelligence'."

    if rid == "L3-DANGLING-HEADER":
        if re.fullmatch(r"\s*(never|always|do not|don'?t|rules?)\s*:\s*", t.get(no) or "", re.I):
            t.delete(no)
        return "Removed the standalone header so the rule can't be read two ways."

    if rid == "L1-STRAY-TOKEN":
        t.set(no, re.sub(r"(\{Global\.\w+\})\s+[a-z]\b(?!['’\w])", r"\1", t.get(no)))
        return "Removed the stray character after the variable."

    if rid == "L1-HIDDEN-INSTRUCTION" and len(f.lines) > 1:
        for extra in f.lines[1:]:
            t.delete(extra)
        return "Kept one reference to the injected guardrail variable."

    if rid == "L5-OVER-AUGMENTED-QUERY":
        for n in f.lines:
            if "training" in t.get(n).lower():
                t.set(n, '- "What are some good training programs?" -> "training programs" (add job level only when the '
                         "question is about level-specific programs or eligibility).")
            else:
                t.set(n, re.sub(r"\s*for \{Global\.\w*(Level|Title|Grade)\w*\}\s*", "", t.get(n)))
        t.insert_after(f.lines[-1], [
            "- Add the user's country only for policies that vary by country (holidays, leave, benefits, payroll). "
            "Add job level only for level-specific programs or eligibility. Otherwise search with the user's own words."])
        return "Stopped appending job level to every search; added a rule for when country/level help."

    if rid == "L3-SELF-CONTRADICTION":
        for n in f.lines[1:]:
            if "training" in t.get(n).lower():
                t.set(n, re.sub(r"\s*for \{Global\.\w+\}\s*", "", t.get(n)))
        return "Aligned examples with the 'only when it changes the answer' rule."

    if rid == "L4-NO-ACTION-OFFER":
        t.set(no, "- Don't add generic closings or unsolicited offers. When the user asks how to do something a "
                  "configured topic can perform (for example, creating an HR case), end by offering that action once.")
        for n in f.lines[1:]:
            t.delete(n)
        return "Kept 'no generic offers' but restored the how-to → action offer."

    if rid == "L3-JURISDICTION-REFUSAL":
        t.set(no, "- For laws, statutory entitlements, or legal deadlines, answer only if retrieved content covers the "
                  "user's jurisdiction. For company policy, answer from the retrieved policy and say when it applies globally.")
        return "Limited the jurisdiction rule to legal claims; global company policies are answered."

    if rid == "L6-UNSAFE-DEFAULT":
        t.set(no, re.sub(r"Treat Yes as manager and No, blank, missing, or unavailable as non-manager\.",
                         "Treat Yes as manager and No as non-manager. If blank or unavailable and the answer differs for "
                         "managers, ask once: \"Are you asking as a people manager?\" Otherwise give employee guidance.",
                         t.get(no)))
        return "Missing manager flag now triggers one clarifying question instead of silently defaulting."

    if rid == "L6-HARD-ROUTE-EXCLUDES-TOOLS":
        t.set(no, re.sub(r",?\s*or MCP tools", "", t.get(no)).rstrip(".") +
              ". For the employee's own data on this subject, use the matching Workday/MCP read tool.")
        return "Allowed the matching read tool for the employee's own data."

    if rid == "L7-PREEMPTIVE-ROUTE":
        t.set(no, re.sub(r"\s*before considering other topics", "", t.get(no)))
        return "Sensitive route no longer pre-empts the routing tree."

    if rid == "L8-LITERAL-EXAMPLE":
        t.set(no, re.sub(r"According to employee handbook section 4\.2\.{0,3}", "According to <article title>...", t.get(no)))
        return "Replaced the literal citation example with a placeholder."

    sentence_fixes = {
        "L4-FALLBACK-ESCALATE": (r"[^.]*\b(unsure|not sure|uncertain|in doubt|can'?t answer|cannot answer|don'?t know)\b[^.]*\b(case|ticket|incident)\b[^.]*\.?",
                                 "If KB search and one retry don't answer the question, offer to create a case.",
                                 "Escalation now happens only after knowledge search and one retry."),
        "L5-QUERY-REWRITE": (r"[^.]*\b(translate|rewrite|rephrase|expand|append|prepend)\b[^.]*\b(query|queries|search terms?|keywords?)\b[^.]*\.?",
                             "Search with the user's words; add country or role only when the policy varies by it.",
                             "Search now uses the user's words."),
        "L8-CITATION-STRIP": (r"[^.]*\b(do not|don'?t|never|avoid|without)\b[^.]*\b(links?|urls?|citations?|sources?|references?|hyperlinks?)\b[^.]*\.?",
                              "Cite only the articles you used, by title, with their links.",
                              "Citations are kept, limited to the articles used."),
        "L8-LENGTH-CAP": (r"[^.]*\d+\s*(words?|sentences?|lines?|bullets?|characters?)[^.]*\.?|[^.]*one[- ]sentence[^.]*\.?",
                          "Be concise: lead with the answer, then the key conditions and the source.",
                          "Replaced the hard length cap with a concise-answer rule."),
        "L8-RENDER-FORMAT": (r"[^.]*\b(tables?|markdown|html|code blocks?|emojis?|<br>|<table>)[^.]*\.?",
                             "Use short paragraphs and bullets; use a table only for comparisons or multi-column data.",
                             "Formatting now adapts to the content."),
    }
    if rid in sentence_fixes:
        pat, repl, msg = sentence_fixes[rid]
        new, n = re.subn(pat, " " + repl, t.get(no), count=1, flags=re.I)
        if n:
            lead = re.match(r"\s*(-\s*)?", t.get(no)).group(0)
            t.set(no, lead + re.sub(r"\s{2,}", " ", new).strip().lstrip("- ").strip())
            return msg

    if rid.startswith("L2-") and baseline is not None:
        block = next((b for b in CORE_BLOCKS if f"L2-{b['id'].split('.')[1].upper()}-MISSING" == rid), None)
        hits = core_presence(baseline).get(block["id"], []) if block else []
        if hits:
            s, e, _ = block_range(baseline, hits[0])
            text = [ln.text for ln in baseline.instructions if s <= ln.no <= e]
            anchor = next((b.end for b in segment(doc) if re.search(r"core identity", b.title, re.I)), min(t.lines))
            t.insert_after(anchor, [""] + text)
            return f"Restored the ESS template '{block['title']}' section."
    return None


def _shrink(text: str, doc: AgentDoc, baseline: AgentDoc | None, f: Finding, applied: list, manual: list) -> str:
    """Condense to LENGTH_TARGET (Copilot Studio count) without losing abilities; report what's still needed."""
    from ess_maker.doctor.condense import condense

    work = from_instructions(text, doc.name, "fix")
    work.topics, work.globals_defined = doc.topics, doc.globals_defined
    work.is_full_export, work.content_moderation = doc.is_full_export, doc.content_moderation
    r = condense(work, baseline, LENGTH_TARGET)
    c = r["chars"]
    kept = "no loss of routes, topics, quoted replies, ESS core blocks or best practices"
    if r["reached"]:
        applied.append({"rule_id": f.rule_id, "title": f.title, "lines": f.lines,
                        "change": f"Shortened {c['before']:,} → {c['after']:,} characters (Copilot Studio count), under the "
                                  f"{LENGTH_TARGET:,} target that keeps a 10% buffer below {DA_INSTRUCTION_LIMIT:,}, with {kept}."})
    else:
        if c["after"] < c["before"]:
            applied.append({"rule_id": f.rule_id, "title": f.title, "lines": f.lines,
                            "change": f"Shortened {c['before']:,} → {c['after']:,} characters with {kept}."})
        moves = "; ".join(f"line {x['line']} ({x['chars']} chars) → {x['move_to']}" for x in r["candidates"][:3]
                          if not x["ess_core"])
        manual.append({"rule_id": f.rule_id, "title": f.title, "lines": f.lines,
                       "change": f"Still {r['still_needed']:,} characters over the {LENGTH_TARGET:,} target. Safe rewording "
                                 "can't remove more without dropping a rule; move content out of the instructions"
                                 + (f": {moves}." if moves else ".")})
    return r["new_text"]


def fix(doc: AgentDoc, baseline: AgentDoc | None, rule_ids: list[str] | None = None) -> dict:
    from ess_maker.doctor import practices

    before = validate(doc, baseline)
    wanted_bp = [r for r in (rule_ids or []) if r.startswith("BP-")]
    gaps_before = practices.check(doc)[0]
    # Best-practice additions are opt-in per practice ("Fix it"); "Fix all" only repairs rule findings.
    targets = [f for f in before if f.origin == "maker" and (not rule_ids or f.rule_id in rule_ids)] \
        + [g for g in gaps_before if g.rule_id in wanted_bp]
    shrink = [f for f in targets if f.rule_id in LENGTH_RULES]
    targets = [f for f in targets if f.rule_id not in LENGTH_RULES]
    applied, manual = [], []

    def run(selected: list[Finding]) -> tuple[_Text, list[dict], list[dict]]:
        t = _Text(doc)
        ok, todo, seen = [], [], set()
        for f in selected:
            if f.rule_id in seen and f.rule_id.startswith("L2-"):
                continue
            seen.add(f.rule_id)
            msg = _apply_one(f, doc, t, baseline)
            (ok if msg else todo).append({"rule_id": f.rule_id, "title": f.title, "lines": f.lines, "change": msg or f.fix})
        return t, ok, todo

    t, applied, manual = run(targets)
    if len(t.render()) > DA_INSTRUCTION_LIMIT and any(a["rule_id"].startswith("L2-") for a in applied):
        # Restoring ESS core sections doesn't fit; apply everything else and leave core restoration as manual.
        core = [f for f in targets if f.rule_id.startswith("L2-")]
        t, applied, manual = run([f for f in targets if not f.rule_id.startswith("L2-")])
        trial, _, _ = run(targets)
        need = len(trial.render()) - DA_INSTRUCTION_LIMIT
        for f in core:
            manual.append({"rule_id": f.rule_id, "title": f.title, "lines": f.lines,
                           "change": f"Restoring this ESS core section would exceed the 8,000-character limit by about "
                                     f"{need:,} characters. Consolidate or move customer sections (topic descriptions, "
                                     "skills) first, then restore it."})
    new_text = t.render()
    if shrink:
        new_text = _shrink(new_text, doc, baseline, shrink[0], applied, manual)
    new_doc = from_instructions(new_text, doc.name + " (fixed)", "fix")
    new_doc.topics, new_doc.globals_defined = doc.topics, doc.globals_defined
    new_doc.is_full_export, new_doc.content_moderation = doc.is_full_export, doc.content_moderation
    new_doc.knowledge_list, new_doc.knowledge_sources = doc.knowledge_list, doc.knowledge_sources
    new_doc.capabilities, new_doc.topics_partial = doc.capabilities, doc.topics_partial
    new_doc.topics_custom, new_doc.topics_system = doc.topics_custom, doc.topics_system
    if doc.platform_char_count:
        new_doc.platform_char_count = doc.platform_char_count + (len(new_text) - len(doc.instruction_text))
    after = validate(new_doc, baseline)
    gaps_after = practices.check(new_doc)[0]
    old_doc = from_instructions(doc.instruction_text, doc.name, "current")
    return {
        "new_instructions": new_text,
        "applied": applied,
        "manual": manual,
        "remaining": [f.to_dict() for f in after if f.origin == "maker"] + [g.to_dict() for g in gaps_after],
        "counts": {"before": len([f for f in before if f.origin == "maker"]),
                   "after": len([f for f in after if f.origin == "maker"])},
        "chars": {"before": doc.char_count, "after": new_doc.char_count, "limit": DA_INSTRUCTION_LIMIT},
        "diff": diff_docs(old_doc, new_doc),
        "tests": sorted({p for f in targets for p in f.test_prompts}),
        "fixed_doc": new_doc,
    }
