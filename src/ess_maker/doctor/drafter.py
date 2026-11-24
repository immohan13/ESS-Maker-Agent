"""Mode 2: help makers draft an instruction change that does what they want and nothing else.

Drafts follow the authoring practices in `practices.py` (observed in the ESS templates and the customer agents
reviewed). Each draft lists the practices it applied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ess_maker.doctor.agent_file import AgentDoc, from_instructions
from ess_maker.doctor.blocks import diff_docs
from ess_maker.doctor.practices import BY_ID
from ess_maker.doctor.rules import DA_INSTRUCTION_LIMIT, validate
from ess_maker.settings import settings

CHANGE_TYPES = {
    "terminology": "Terminology / dictionary",
    "restricted": "Restrict a subject",
    "format": "Response format",
    "routing": "Route a subject to a knowledge source",
    "escalation": "Escalation / handoff",
    "persona": "Persona / tone",
    "disclaimer": "Disclaimer on responses",
    "condense": "Condense to a character target",
}

# Where each change type belongs
SECTION_FOR = {
    "terminology": "# Terminology",
    "restricted": "# Restricted subjects",
    "format": "# Response format",
    "routing": "# Knowledge routing",
    "escalation": "# Escalation",
    "persona": "# Persona",
    "disclaimer": "# Formatting rules",
}
_HOME = {
    "routing": r"routing",
    "restricted": r"routing",
    "terminology": r"core identity",
    "persona": r"core identity",
    "escalation": r"hand ?off|escalation",
    "format": r"formatting",
    "disclaimer": r"formatting|response format",
}


@dataclass
class Intake:
    change_type: str
    intent: str
    should_affect: list[str] = field(default_factory=list)
    must_not_affect: list[str] = field(default_factory=list)
    content_sample: str = ""
    # Type-specific slots
    terms: list[list[str]] = field(default_factory=list)  # [[user term, source term], ...]
    subject: str = ""
    allowed: str = ""
    scope: str = ""
    format_spec: str = ""
    source_name: str = ""
    route: str = ""
    evals: list[dict] = field(default_factory=list)  # [{"prompt": ..., "expect": ...}] uploaded by the maker
    mode: str = ""  # routing: "only" (use this source) or "exclude" (don't use it)
    unused: list[str] = field(default_factory=list)  # parts of the description that didn't map to a rule
    target_chars: int = 0  # condense: requested instruction length
    disclaimer_text: str = ""  # disclaimer: exact text to append
    disclaimer_style: str = ""  # disclaimer: "", "bold", "italic" or "bold italic"


_STOPWORDS = {"questions", "question", "requests", "request", "about", "anything", "related", "employee", "employees"}


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" .;,:'\"")
    return re.sub(r"\s+(questions?|requests?|queries)$", "", s, flags=re.I).strip()


_GENERIC_SOURCE = re.compile(r"^(the\s+)?(sharepoint|kb|knowledge( base)?|knowledge source|site|source)s?$", re.I)


def _match_source(name: str, agent: AgentDoc | None) -> str:
    """Map what the maker typed to a configured knowledge source name when possible."""
    name = re.sub(r"\s+(knowledge\s+source|knowledge|source|site)s?$", "", name.strip(), flags=re.I).strip(" '\"")
    configured = [k.get("name", "") for k in (agent.knowledge_list if agent else []) if k.get("name")]
    hits = [c for c in configured if name and (name.lower() in c.lower() or c.lower() in name.lower())]
    if len(hits) == 1:
        return hits[0]
    if _GENERIC_SOURCE.match(name or "") and len(configured) == 1:
        return configured[0]
    return name


_DISCLAIMER_LINE = re.compile(r'^\s*-\s*(?:End every response|Every [^"]{0,60}ends with this line)[^"]*"([^"]+)"', re.I)


def _existing_disclaimer(agent: AgentDoc) -> tuple[int, str] | None:
    """(index, text) of a disclaimer rule the Doctor drafted earlier, so a new one replaces it."""
    for i, ln in enumerate(agent.instructions):
        m = _DISCLAIMER_LINE.match(ln.text)
        if m:
            return i, m.group(1).strip().strip("*_").strip()
    return None


def interpret(intake: Intake, agent: AgentDoc | None = None) -> Intake:
    """Fill empty slots from the maker's plain-language description and note parts that didn't map to a rule."""
    text = intake.intent
    spans: list[tuple[int, int]] = []

    def take(m):
        spans.append(m.span())
        return m

    t = intake.change_type
    if t == "restricted" and not intake.subject:
        m = re.search(r"\b(?:don'?t|do not|never|stop|shouldn'?t|should not)\s+(?:answer|discuss|talk about|provide|respond to|"
                      r"help with|give|share|cover)\s+(?:any\s+)?(?:questions?\s+(?:about|on)\s+)?(.+?)(?=[;.]|,\s|\s+(?:and|but)\s|$)", text, re.I)
        if m:
            intake.subject = _clean(take(m).group(1))
    if t == "routing" and not (intake.subject and intake.source_name):
        src_pat = r"(?:the\s+)?(?P<src>[\w .&'/-]+?)(?:\s+knowledge\s+sources?|\s+site|\s+source)?"
        m = re.search(r"\b(?:not|don'?t|never|stop|shouldn'?t)\s+(?:to\s+)?(?:use|search|query|pull\s+from)\s+" + src_pat +
                      r"\s+for\s+(?P<subj>[^.;,]+?)(?=\s+and\s|[.;,]|$)", text, re.I)
        if m:
            intake.mode = intake.mode or "exclude"
        else:
            m = (re.search(r"\b(?:use|search|query)\s+(?:only\s+)?" + src_pat + r"\s+(?:only\s+)?for\s+(?P<subj>[^.;,]+?)(?=\s+and\s|[.;,]|$)", text, re.I)
                 or re.search(r"\banswer\s+(?P<subj>[^.;,]+?)\s+(?:questions\s+)?(?:only\s+)?from\s+" + src_pat + r"(?=\s+and\s|[.;,]|$)", text, re.I))
            if m:
                intake.mode = intake.mode or "only"
        if m:
            take(m)
            intake.subject = intake.subject or _clean(m.group("subj"))
            intake.source_name = intake.source_name or _match_source(m.group("src"), agent)
    if t == "routing" and intake.source_name:
        intake.source_name = _match_source(intake.source_name, agent)
    if t == "terminology" and not intake.terms:
        for m in re.finditer(r"([\w'/-][\w\s'/-]{0,38}?)\s*(?:=|->|\bmeans\b|\brefers to\b|\bis the same as\b)\s*([\w'/-][\w\s'/-]{0,58}?)(?=[,;.]|\s+and\s|$)", text, re.I):
            take(m)
            intake.terms.append([_clean(m.group(1)), _clean(m.group(2))])
    if t == "escalation" and not (intake.scope and intake.route):
        m = re.search(r"\b(?:when|if)\s+(?P<scope>.+?),?\s+(?:hand\s?off|escalate|route|send|offer|refer)\s+(?:it\s+|them\s+|the\s+user\s+)?"
                      r"(?:to\s+)?(?P<route>[^.;]+)", text, re.I)
        if m:
            take(m)
            intake.scope = intake.scope or _clean(m.group("scope"))
            intake.route = intake.route or _clean(m.group("route"))
    if t == "format" and not intake.format_spec:
        m = re.search(r"\b(?:format|answer|respond to|show)\s+(?P<scope>.+?)\s+(?:as|in|with|using)\s+(?P<spec>[^.;]+)", text, re.I)
        if m:
            take(m)
            intake.scope = intake.scope or _clean(m.group("scope"))
            intake.format_spec = _clean(m.group("spec"))
    if not intake.route:
        m = re.search(r"\b(?:send|refer|direct|point|redirect|route)\s+(?:them|people|users|employees|the (?:user|employee))?\s*"
                      r"to\s+(.+?)(?=[;.]|,\s|$)", text, re.I)
        if m:
            intake.route = _clean(take(m).group(1))
    if not intake.allowed:
        m = (re.search(r"([^.;]+?)\s+(?:should|must|can)\s+still\s+be\s+answered", text, re.I)
             or re.search(r"\bstill\s+answer\s+([^.;]+)", text, re.I))
        if m:
            intake.allowed = _clean(take(m).group(1))
    if t == "persona":
        spans.append((0, len(text)))
    if t == "disclaimer":
        if not intake.disclaimer_text:
            m = re.search(r'"([^"]{4,400})"|“([^”]{4,400})”', text) or \
                re.search(r"\bdisclaimer\b[^:]{0,40}:\s*(.+)$", text, re.I)
            if m:
                intake.disclaimer_text = next(g for g in m.groups() if g).strip()
            elif agent is not None:
                # Restyling an existing disclaimer: reuse the text already in the instructions.
                existing = _existing_disclaimer(agent)
                if existing:
                    intake.disclaimer_text = existing[1]
        intake.disclaimer_text = intake.disclaimer_text.strip().strip("*_").strip()
        if not intake.disclaimer_style:
            bold, italic = re.search(r"\bbold\b", text, re.I), re.search(r"\bitalics?\b", text, re.I)
            intake.disclaimer_style = " ".join(s for s, on in (("bold", bold), ("italic", italic)) if on)
        if not intake.scope:
            m = re.search(r"\b(?:only\s+)?(?:to|on|for|in)\s+((?:policy|benefits?|payroll|leave|knowledge|kb|hr)[\w\s/-]{0,40}?"
                          r"(?:answers?|responses?|questions?))", text, re.I)
            intake.scope = _clean(m.group(1)) if m else ""
        spans.append((0, len(text)))
    if t == "condense":
        m = re.search(r"(\d[\d,]{2,})\s*(?:chars?|characters?)?", text, re.I)
        if m and not intake.target_chars:
            intake.target_chars = int(m.group(1).replace(",", ""))
        spans.append((0, len(text)))
    # Report pieces of the description that no rule used, so nothing silently disappears.
    pos = 0
    for part in re.split(r"(?<=[.;])\s+|\s+and\s+(?=(?:it|they|the agent|also|should|must)\b)", text):
        start = text.find(part, pos)
        end = start + len(part)
        pos = max(pos, end)
        if len(part.strip()) > 15 and not any(s < end and e > start for s, e in spans):
            intake.unused.append(part.strip(" .;"))
    if intake.evals:
        subj = _words(intake.subject or intake.intent) - _STOPWORDS
        for e in intake.evals:
            q = (e.get("prompt") or "").strip()
            if not q or q in intake.should_affect or q in intake.must_not_affect:
                continue
            (intake.should_affect if subj & _words(q) else intake.must_not_affect).append(q)
    return intake


REQUIRED = {"routing": ["subject", "source_name"], "restricted": ["subject"], "terminology": ["terms"],
            "escalation": ["scope", "route"], "format": ["format_spec"], "persona": [], "condense": ["target_chars"],
            "disclaimer": ["disclaimer_text"]}
SLOT_LABEL = {"subject": "Which questions (the subject)", "source_name": "Knowledge source name, as configured",
              "terms": "Term pairs (user term = source term)", "scope": "When does this apply",
              "route": "Where to send people", "format_spec": "The format to use",
              "target_chars": "Target length in characters", "disclaimer_text": "Disclaimer text, exactly as it should appear",
              "disclaimer_style": "Style (bold, italic, bold italic)"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{3,}", s.lower())}


def placement(intake: Intake, agent: AgentDoc) -> dict:
    """Decide where the change should live. Most changes shouldn't go into global instructions."""
    subj = _words(intake.subject or intake.intent)
    owners = [
        t.display_name for t in agent.topics.values()
        if t.display_name and len(subj & _words(t.display_name)) >= 1 and (t.state or "Active") == "Active"
    ][:6]
    if intake.change_type == "routing":
        return {
            "target": "knowledge_source_description",
            "why": "Routing is decided from knowledge source descriptions first. Put the scope there; add a one-line "
                   "instruction only if the orchestrator still picks the wrong source in tests.",
            "also_instruction": True, "owners": owners,
        }
    if intake.change_type == "restricted" and owners:
        return {
            "target": "topic_description",
            "why": f"Topics already own this subject ({', '.join(owners[:3])}). Add the exclusion to those topic "
                   "descriptions so the rule doesn't block them; keep a scoped global line for knowledge answers.",
            "also_instruction": True, "owners": owners,
        }
    if intake.change_type == "format" and intake.scope and owners:
        return {
            "target": "topic_response_template",
            "why": "The format applies to answers produced by specific topics; change their response templates "
                   "instead of global instructions.",
            "also_instruction": False, "owners": owners,
        }
    if intake.change_type == "disclaimer":
        return {"target": "instructions",
                "why": "Generated answers are written by the model, so the disclaimer goes in Formatting rules. Text "
                       "that topics send (adaptive cards, topic messages) doesn't pass through instructions; add the "
                       "line to those topics' message nodes if it must appear there too.",
                "also_instruction": True, "owners": []}
    return {"target": "instructions", "why": "Applies across answers, so a scoped instruction clause is appropriate.",
            "also_instruction": True, "owners": owners}


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def draft_clause(intake: Intake) -> tuple[str, list[str]]:
    """Write one instruction bullet using the authoring practices; returns (clause, practice ids applied)."""
    t = intake.change_type
    if t == "terminology":
        pairs = [p for p in intake.terms if len(p) == 2 and p[0].strip() and p[1].strip()]
        defs = "; ".join(f"\"{a}\" means {b}" for a, b in pairs)
        which = "these terms" if len(pairs) > 1 else "this term"
        return (f"- {_cap(defs)}. When employees use {which}, search with the source term and answer using "
                "the employee's term.", ["P03", "P18", "P43"])
    if t == "restricted":
        subject = intake.subject
        allowed = intake.allowed or "related company policies, programs, and the employee's own records"
        if re.search(r"\badvice|opinions?\b", subject, re.I):
            return (f"- Don't provide {subject}. This covers advice, opinions, and comparisons only; still answer "
                    f"questions about {allowed} from approved sources and supported topics, and say what you can help "
                    "with instead.", ["P04", "P05", "P24", "P34", "P43"])
        route = intake.route or "the HR team"
        return (f"- Questions whose main intent is {subject} (check this before every other route): don't search "
                f"knowledge or call tools. Reply: \"I can't help with questions about {subject}. Please contact {route}.\" "
                f"Questions about {allowed}, and questions that only mention {subject}, follow the routes below.",
                ["P04", "P05", "P24", "P25", "P43"])
    if t == "routing":
        subj, src = intake.subject, intake.source_name
        if intake.mode == "exclude":
            return (f"- Questions whose main intent is {subj}: don't search {src}; answer them from the other "
                    f"knowledge sources. {_cap(src)} still answers all other questions.", ["P04", "P05", "P43"])
        return (f"- Questions whose main intent is {subj}: search {src} first; if it doesn't cover the question, "
                "search the other knowledge sources once, then use the standard abstain message. Other questions "
                f"aren't limited to {src}.", ["P04", "P05", "P19", "P25", "P43"])
    if t == "escalation":
        return (f"- When {intake.scope}, say what you can help with and offer {intake.route} only if a configured "
                "topic supports it. Don't say a handoff happened unless the topic confirms it.",
                ["P29", "P30", "P33", "P43"])
    if t == "format":
        scope = intake.scope or "policy answers"
        return (f"- For {scope}: {intake.format_spec}. Keep every citation and exact URL, and don't change topic "
                "cards or notices.", ["P36", "P37", "P38", "P43"])
    if t == "persona":
        return (f"- Tone: {intake.intent.strip().rstrip('.')}. Keep answers factual and grounded in approved sources.",
                ["P02", "P43"])
    if t == "disclaimer":
        text = intake.disclaimer_text.strip().strip('"“”')
        style = intake.disclaimer_style.lower()
        mark = "***" if "bold" in style and "italic" in style else "**" if "bold" in style else "*" if "italic" in style else ""
        styled = f"{mark}{text}{mark}"
        how = f" in {style}, keeping the {mark} Markdown" if mark else ""
        end = "" if (styled[-1:] in ".!?") else "."
        text = styled
        if intake.scope:
            who = f"Every {intake.scope.rstrip('s') if intake.scope.endswith('answers') else intake.scope}"
            return (f"- {who} ends with this line, exactly as written{how}, after the citations: \"{text}\"{end} Add it once per "
                    "message; don't reword, translate or shorten it, and don't add it to other responses.",
                    ["P37", "P38", "P41", "P43"])
        return (f"- End every response you write (answers, clarifying questions and the abstain message) with this line, "
                f"exactly as written{how}, after the citations: \"{text}\"{end} Add it once per message; don't reword, translate "
                "or shorten it.", ["P37", "P38", "P41", "P43"])
    raise ValueError(f"Unknown change type {t}")


def _gate(clause: str) -> list[str]:
    """Block drafts that read like the maker's notes instead of an instruction (P43)."""
    problems = []
    if re.search(r"\b(I|we)\s+(want|need|would|'d|like|think)\b|\bI'd\b|\bI'm\b", clause):
        problems.append("it contains first-person wording from your description")
    if re.search(r"<[^>]{2,40}>", clause):
        problems.append("a required detail is still a placeholder")
    if len(clause) > 450:
        problems.append(f"it's {len(clause)} characters; split it into separate changes")
    return problems


async def polish_with_llm(intake: Intake, clause: str, agent: AgentDoc) -> str | None:
    if not (settings.azure_openai_endpoint and settings.azure_openai_api_key):
        return None
    from ess_maker.llm.azure_openai import chat_json

    system = (
        "You edit instructions for an enterprise HR/IT Copilot Studio agent. Rewrite the draft clause so it achieves "
        "the maker's intent with the narrowest possible scope. Rules: no 'override everything' or absolute language; "
        "never block supported topics or the how-to -> offer-action path; never ask to search without the user's "
        "words; keep it under 450 characters; bullet style starting with '- '. Return JSON {\"clause\": str}."
    )
    user = (
        f"Intent: {intake.intent}\nShould affect: {intake.should_affect}\nMust NOT affect: {intake.must_not_affect}\n"
        f"Content sample: {intake.content_sample[:1500]}\nDraft clause: {clause}\n"
        f"Existing section titles: {[ln.text for ln in agent.instructions if ln.text.startswith('#')]}"
    )
    try:
        out = await chat_json(system, user)
        c = (out.get("clause") or "").strip()
        return c or None
    except Exception:
        return None


def insert_clause(agent: AgentDoc, change_type: str, clause: str) -> str:
    """Place the clause in the section the ESS template uses for this kind of rule (P43)."""
    lines = [ln.text for ln in agent.instructions]
    if change_type == "disclaimer":
        existing = _existing_disclaimer(agent)
        if existing:
            lines[existing[0]] = clause
            return "\n".join(lines)
    heads = [i for i, s in enumerate(lines) if s.lstrip().startswith("#")]
    home = next((i for i in heads if re.search(_HOME[change_type], lines[i], re.I)), None)
    if home is None:
        return "\n".join(lines + ["", SECTION_FOR[change_type], clause])
    end = next((i for i in heads if i > home), len(lines))
    while end > home + 1 and not lines[end - 1].strip():
        end -= 1
    if change_type == "restricted":
        # Restrictions must win over the routing tree's "policy -> KB search" rule, so they go first in it.
        return "\n".join(lines[:home + 1] + [clause] + lines[home + 1:])
    if change_type == "routing":
        # Sit next to the routing tree's knowledge-search bullet, with the same indentation.
        kb = next((i for i in range(home + 1, end) if re.search(r"\bKB search\b|knowledge search", lines[i], re.I)
                   and lines[i].lstrip().startswith("-")), None)
        if kb is not None:
            indent = lines[kb][: len(lines[kb]) - len(lines[kb].lstrip())]
            return "\n".join(lines[:kb + 1] + ["", indent + clause] + lines[kb + 1:])
    sep = [] if change_type in ("disclaimer", "format") and lines[end - 1].lstrip().startswith("-") else [""]
    return "\n".join(lines[:end] + sep + [clause] + lines[end:])


def collisions(intake: Intake, agent: AgentDoc, clause: str) -> list[dict]:
    out = []
    restricted = _words(intake.subject) if intake.change_type == "restricted" else set()
    for q in intake.must_not_affect:
        overlap = restricted & _words(q)
        if overlap:
            out.append({"query": q, "terms": sorted(overlap),
                        "risk": "Shares words with the restricted subject; the model may refuse it. Keep it in the test set."})
    if restricted:
        for t in agent.topics.values():
            overlap = restricted & _words(t.display_name)
            if overlap and (t.state or "Active") == "Active":
                out.append({"topic": t.display_name, "terms": sorted(overlap),
                            "risk": "Active topic covers this subject; the restriction could suppress it."})
    return out[:12]


def generate_tests(intake: Intake) -> list[dict]:
    expected = {(e.get("prompt") or "").strip(): (e.get("expect") or "").strip() for e in intake.evals}
    tests = [{"prompt": q, "expect": "changed", "why": expected.get(q) or "Should reflect the new instruction"}
             for q in intake.should_affect]
    tests += [{"prompt": q, "expect": "unchanged", "why": expected.get(q) or "Must behave exactly as before"}
              for q in intake.must_not_affect]
    golden = {
        "restricted": ["Where is my most recent paystub?", "How does the 401(k) match work?"],
        "terminology": ["How do I open a requisition?"],
        "routing": ["What is the parental leave policy?"],
        "format": ["How do I request time off?"],
        "escalation": ["I want to file a complaint about my manager"],
        "persona": ["How many vacation days do I have left?"],
        "disclaimer": ["What is the parental leave policy?", "What does PTO stand for?",
                       "Can I bring my dog to the office?", "Show my open HR cases"],
        "condense": ["How many vacation days do I have left?", "How do I update my home address?",
                     "What is the parental leave policy?", "Show my open HR cases",
                     "How do I report a payroll issue?", "What does PTO stand for?"],
    }.get(intake.change_type, [])
    tests += [{"prompt": q, "expect": "unchanged", "why": "ESS golden regression prompt"} for q in golden]
    if intake.change_type == "disclaimer":
        scope = _words(intake.scope) - {"answer", "answers", "response", "responses", "questions", "question"}
        seen, out = set(), []
        for t in tests:
            if t["prompt"].lower() in seen:
                continue
            seen.add(t["prompt"].lower())
            hit = not intake.scope or bool(scope & _words(t["prompt"]))
            out.append({"prompt": t["prompt"], "expect": "changed" if hit else "unchanged",
                        "why": (f"Ends with \"{intake.disclaimer_text}\"; the answer itself is unchanged" if hit
                                else "Outside the disclaimer's scope; no disclaimer, answer unchanged")})
        return out
    return tests


def _draft_condense(intake: Intake, agent: AgentDoc, baseline: AgentDoc | None) -> dict:
    from ess_maker.doctor.condense import condense

    r = condense(agent, baseline, intake.target_chars)
    old_doc = from_instructions(agent.instruction_text, agent.name)
    new_doc = from_instructions(r["new_text"], agent.name + " (condensed)")
    for d in (old_doc, new_doc):
        d.topics, d.globals_defined = agent.topics, agent.globals_defined
        d.is_full_export, d.content_moderation = agent.is_full_export, agent.content_moderation
    before = {f.rule_id for f in validate(old_doc, baseline)}
    introduced = [f.to_dict() for f in validate(new_doc, baseline) if f.rule_id not in before]
    saved = r["chars"]["before"] - r["chars"]["after"]
    summary = (f"Condensed {r['chars']['before']:,} → {r['chars']['after']:,} characters as Copilot Studio counts them (−{saved:,}) with no loss of "
               "routes, topics, quoted replies, variables, sections, ESS core blocks or best practices.")
    if not r["reached"]:
        summary += (f" The {intake.target_chars:,}-character target needs {r['still_needed']:,} more; that requires "
                    "moving content out of the instructions (see below), which isn't done automatically.")
    return {
        "placement": {"target": "instructions", "why": "Whole-instruction edit; nothing was added.", "owners": []},
        "clause": summary, "template_clause": summary, "llm_polished": False,
        "practices": [{"id": k, "text": f"{BY_ID[k].title}: {BY_ID[k].guidance}", "source": BY_ID[k].seen_in}
                      for k in ("P13", "P43")],
        "unused": intake.unused, "source_note": "", "condense": r,
        "new_instructions": r["new_text"],
        "char_count": {"before": r["chars"]["before"], "after": r["chars"]["after"], "limit": DA_INSTRUCTION_LIMIT},
        "introduced_findings": introduced, "collisions": [],
        "tests": generate_tests(intake), "diff": diff_docs(old_doc, new_doc),
    }


async def draft(intake: Intake, agent: AgentDoc, baseline: AgentDoc | None, use_llm: bool = True) -> dict:
    intake = interpret(intake, agent)
    missing = [k for k in REQUIRED.get(intake.change_type, []) if not getattr(intake, k)]
    if missing:
        return {"needs_input": [{"slot": k, "label": SLOT_LABEL[k]} for k in missing], "unused": intake.unused,
                "change_type": intake.change_type,
                "message": "I couldn't turn your description into a precise rule. Fill in: "
                           + ", ".join(SLOT_LABEL[k].lower() for k in missing) + "."}
    if intake.change_type == "condense":
        return _draft_condense(intake, agent, baseline)
    place = placement(intake, agent)
    clause, applied = draft_clause(intake)
    polished = await polish_with_llm(intake, clause, agent) if use_llm else None
    final_clause = polished or clause
    problems = _gate(final_clause)
    if problems:
        return {"needs_input": [{"slot": k, "label": SLOT_LABEL[k]} for k in REQUIRED.get(intake.change_type, [])],
                "unused": intake.unused, "change_type": intake.change_type,
                "message": "The draft isn't ready: " + "; ".join(problems) + ". Fill in the details below."}
    new_text = insert_clause(agent, intake.change_type, final_clause)
    new_doc = from_instructions(new_text, agent.name + " (draft)")
    new_doc.topics, new_doc.globals_defined = agent.topics, agent.globals_defined
    new_doc.is_full_export, new_doc.content_moderation = agent.is_full_export, agent.content_moderation
    old_doc = from_instructions(agent.instruction_text, agent.name)
    old_doc.topics, old_doc.globals_defined = agent.topics, agent.globals_defined
    old_doc.is_full_export, old_doc.content_moderation = agent.is_full_export, agent.content_moderation

    before = {f.rule_id for f in validate(old_doc, baseline)}
    after_findings = validate(new_doc, baseline)
    introduced = [f.to_dict() for f in after_findings if f.rule_id not in before]
    configured = [k.get("name", "") for k in agent.knowledge_list or []]
    source_note = ""
    if intake.change_type == "routing" and configured and intake.source_name not in configured:
        source_note = (f"'{intake.source_name}' doesn't match a configured knowledge source "
                       f"({', '.join(configured[:5])}). Use the exact name so the planner can tell them apart.")
    if intake.change_type == "disclaimer":
        source_note = ("Instructions make the disclaimer very likely, not guaranteed: the model can still drop it on long "
                       "answers. Microsoft 365 Copilot already shows its own 'AI-generated content may be incorrect' "
                       "notice, so keep yours specific to your policies. Check it on every question in the eval set below.")
    return {
        "placement": place,
        "clause": final_clause,
        "template_clause": clause,
        "llm_polished": bool(polished),
        "practices": [{"id": k, "text": f"{BY_ID[k].title}: {BY_ID[k].guidance}", "source": BY_ID[k].seen_in}
                      for k in applied],
        "unused": intake.unused,
        "source_note": source_note,
        "new_instructions": new_text,
        "char_count": {"before": old_doc.char_count, "after": new_doc.char_count, "limit": DA_INSTRUCTION_LIMIT},
        "introduced_findings": introduced,
        "collisions": collisions(intake, agent, final_clause),
        "tests": generate_tests(intake),
        "diff": diff_docs(old_doc, new_doc),
    }
