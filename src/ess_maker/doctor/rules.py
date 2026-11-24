"""Instruction validator: contract checks + known-issue rules.

Every finding carries the exact source lines, the runtime stage it affects, a paste-ready fix,
and test prompts that should expose the problem in an eval run.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ess_maker.doctor.agent_file import AgentDoc, Line
from ess_maker.doctor.blocks import block_range, line_in

DA_INSTRUCTION_LIMIT = 8000

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

STAGES = {
    "platform": "Platform limit",
    "routing": "Routing / topic selection",
    "search": "Knowledge search query",
    "tool": "Tool / action call",
    "synthesis": "Answer generation",
    "rai": "Content safety (RAI)",
    "render": "Rendering",
}

# The four pipeline stages from the ESS "customer customizations shape response quality" model.
PIPELINE = {
    "routing": "1 Planner / tool selection",
    "tool": "1 Planner / tool selection",
    "search": "2 Search / tool execution",
    "synthesis": "3 Synthesis",
    "rai": "3 Synthesis",
    "render": "4 Render",
    "platform": "All stages",
}
PIPELINE_ORDER = ["1 Planner / tool selection", "2 Search / tool execution", "3 Synthesis", "4 Render", "All stages"]

# What the employee sees when the rule's problem occurs.
USER_SEES = {
    "L0-LENGTH": "Incomplete answer", "L0-HEADROOM": "Incomplete answer",
    "L1-REFS-UNVERIFIED": "Fallback / escalation", "L1-TOPIC-MISSING": "Fallback / escalation",
    "L1-TOPIC-INACTIVE": "Fallback / escalation", "L1-TOPIC-OVERLAP": "Wrong answer path",
    "L1-TOPIC-DESC-VAGUE": "Wrong answer path",
    "L1-GLOBAL-UNDEFINED": "Ungrounded / poor answer", "L1-STRAY-TOKEN": "Ungrounded / poor answer",
    "L1-HIDDEN-INSTRUCTION": "Overly cautious",
    "L2-ROUTING-MISSING": "Wrong answer path", "L2-GROUNDING-MISSING": "Ungrounded / poor answer",
    "L2-RETRY-MISSING": "Fallback / escalation",
    "L3-ABSOLUTE-OVERRIDE": "Overly cautious", "L3-JURISDICTION-REFUSAL": "Overly cautious",
    "L3-SELF-CONTRADICTION": "Ungrounded / poor answer", "L3-DANGLING-HEADER": "Inconsistent answers",
    "L3-MULTI-FALLBACK": "Fallback / escalation",
    "L4-NO-ACTION-OFFER": "Incomplete answer", "L4-BLANKET-SUBJECT-BLOCK": "Overly cautious",
    "L4-FALLBACK-ESCALATE": "Fallback / escalation",
    "L5-OVER-AUGMENTED-QUERY": "Ungrounded / poor answer", "L5-QUERY-REWRITE": "Ungrounded / poor answer",
    "L5-SINGLE-SOURCE": "Incomplete answer", "L5-WEB-SEARCH-ON": "Ungrounded / poor answer",
    "L5-BROAD-KNOWLEDGE": "Ungrounded / poor answer",
    "L6-HARD-ROUTE-EXCLUDES-TOOLS": "Incomplete answer", "L6-MANDATED-TOOL-CHAIN": "Slow answer",
    "L6-UNSAFE-DEFAULT": "Wrong answer for the user", "L7-PREEMPTIVE-ROUTE": "Wrong answer path",
    "L7-SENSITIVE-SUPPRESSION": "Wrong answer path", "L7-MODERATION-DRIFT": "Overly cautious",
    "L8-LITERAL-EXAMPLE": "Wrong citations", "L8-CITATION-STRIP": "Wrong citations",
    "L8-LENGTH-CAP": "Incomplete answer", "L8-RENDER-FORMAT": "Broken formatting",
}
_USER_SEES_BY_STAGE = {"routing": "Wrong answer path", "tool": "Wrong answer path", "search": "Ungrounded / poor answer",
                       "synthesis": "Ungrounded / poor answer", "rai": "Overly cautious", "render": "Broken formatting",
                       "platform": "Incomplete answer"}


@dataclass
class Finding:
    rule_id: str
    severity: str
    layer: str
    stage: str
    title: str
    lines: list[int] = field(default_factory=list)
    current: str = ""
    impact: str = ""
    fix: str = ""
    test_prompts: list[str] = field(default_factory=list)
    known_issue: str = ""
    confidence: str = "rule"
    # "maker" = introduced by the customer; "template" = also present in the ESS baseline
    origin: str = "maker"

    def to_dict(self) -> dict:
        from ess_maker.doctor.fixes import MANUAL

        d = asdict(self)
        d["stage_label"] = STAGES.get(self.stage, self.stage)
        d["pipeline"] = PIPELINE.get(self.stage, "All stages")
        d["user_sees"] = USER_SEES.get(self.rule_id) or _USER_SEES_BY_STAGE.get(self.stage, "")
        d["auto_fixable"] = self.rule_id not in MANUAL and self.severity != "info"
        return d


# --- ESS core contract --------------------------------------------------------------------
# Each protected block is detected by signatures; a block is "present" if any signature matches.
CORE_BLOCKS: list[dict] = [
    {
        "id": "core.routing",
        "title": "Routing decision tree",
        "signatures": [
            r"routing decision tree",
            r"classify (each requested task|intent)",
            r"self data read",
            r"personal-data read",
            r"read intent never routes",
            r"\bclassify (state|the request|the user'?s? (request|intent))",
        ],
        "severity": "high",
        "stage": "routing",
        "impact": "Without intent routing the orchestrator guesses: personal-data questions go to knowledge "
        "search, read questions can fire write/manage topics, and how-to questions never offer the action.",
        "fix": "Restore the ESS template '#Routing decision tree' block (read / write / how-to / policy / mixed) "
        "directly after '#Core identity'. Keep customer-specific routes below it.",
        "tests": [
            "Who is my manager?",
            "How do I update my home address?",
            "What is the parental leave policy and how many days have I used?",
        ],
        "ki": "KI-001",
    },
    {
        "id": "core.grounding",
        "title": "Tool-first grounding",
        "signatures": [
            r"tool-first grounding",
            r"completed (request )?tool call",
            r"completed retrieval supports",
            r"answer only from (provided context|retrieved|tool output|approved)",
            r"grounding guard",
        ],
        "severity": "high",
        "stage": "synthesis",
        "impact": "The model may answer from training data instead of approved sources, or reply with a generic "
        "'sorry' because nothing tells it to search first and abstain cleanly.",
        "fix": "Restore the ESS template '#Tool-first grounding' block: require a completed tool/KB call before "
        "any substantive answer, and use the standard abstain message when sources don't cover the question.",
        "tests": ["What does PTO stand for?", "Is the office closed on Juneteenth?"],
        "ki": "KI-002",
    },
    {
        "id": "core.retry",
        "title": "Knowledge retry budget",
        "signatures": [r"kb retry", r"at most one (additional|follow-up)", r"one synonym retry",
                       r"do not repeat a tool call with the same inputs"],
        "severity": "medium",
        "stage": "search",
        "impact": "A single near-miss search ends in 'I couldn't find that' instead of one retry with synonyms.",
        "fix": "Add: 'If the first knowledge search lacks a close match, make at most one more search with "
        "synonyms or broader terms. If still unsupported, say so and offer the approved support route.'",
        "tests": ["How do I get reimbursed for my home internet?", "What's the policy on bringing pets to work?"],
        "ki": "KI-003",
    },
]


def _match(lines: list[Line], pattern: str) -> list[Line]:
    rx = re.compile(pattern, re.IGNORECASE)
    return [ln for ln in lines if rx.search(ln.text)]


def _first(lines: list[Line], pattern: str) -> Line | None:
    hits = _match(lines, pattern)
    return hits[0] if hits else None


def core_presence(doc: AgentDoc) -> dict[str, list[int]]:
    """Line numbers matching each core block, ordered by signature priority (best match first)."""
    out: dict[str, list[int]] = {}
    for block in CORE_BLOCKS:
        hits: list[int] = []
        for sig in block["signatures"]:
            for ln in _match(doc.instructions, sig):
                if ln.no not in hits:
                    hits.append(ln.no)
        out[block["id"]] = hits
    return out


# --- Rules ---------------------------------------------------------------------------------
def _rule_length(doc: AgentDoc) -> list[Finding]:
    n = doc.char_count
    hidden = [g for g, _ in doc.global_refs() if re.search(r"guardrail|instruction|rules|policy", g, re.I)]
    note = (
        f" Plus runtime text injected by {{Global.{hidden[0]}}}, which isn't counted here." if hidden else ""
    )
    if n > DA_INSTRUCTION_LIMIT:
        return [
            Finding(
                "L0-LENGTH", "high", "L0 Platform limits", "platform",
                f"Instructions are {n:,} characters — over the 8,000-character declarative agent limit",
                [doc.instructions[0].no, doc.instructions[-1].no], f"{n:,} chars",
                "The DA manifest rejects or truncates instructions over 8,000 characters; whatever falls past "
                "the cut-off (usually the customer additions at the end) silently stops applying." + note,
                "Move procedures into DA skills or topic descriptions, move fixed answers into topics/editorial "
                "answers, and remove duplicated guidance. Do not offload instructions into knowledge documents.",
                [], "KI-040",
            )
        ]
    if n > DA_INSTRUCTION_LIMIT * 0.9:
        return [
            Finding(
                "L0-HEADROOM", "medium", "L0 Platform limits", "platform",
                f"Instructions are {n:,} characters — within 10% of the 8,000 limit",
                [doc.instructions[0].no], f"{n:,} chars",
                "The next customer edit or template update will push the agent over the DA limit." + note,
                "Reserve headroom: move long procedures into skills or topic descriptions before adding more.",
                [], "KI-040",
            )
        ]
    return []


def _rule_references(doc: AgentDoc) -> list[Finding]:
    out: list[Finding] = []
    refs = doc.topic_refs()
    if refs and not doc.is_full_export:
        out.append(
            Finding(
                "L1-REFS-UNVERIFIED", "info", "L1 References", "routing",
                f"{len(refs)} topic reference(s) can't be verified from an instructions-only export",
                sorted({ln for _, ln in refs}), "; ".join(sorted({r.split('.topic.')[-1] for r, _ in refs})),
                "Topic references that don't resolve make the orchestrator ignore the routing rule.",
                "Upload the full bot content export so references can be checked against installed topics.",
            )
        )
    elif refs:
        for ref, ln in refs:
            t = doc.topics.get(ref)
            if t is None:
                out.append(
                    Finding(
                        "L1-TOPIC-MISSING", "high", "L1 References", "routing",
                        f"Instruction references a topic that isn't in the agent: {ref.split('.topic.')[-1]}",
                        [ln], ref,
                        "The routing rule points at nothing, so the orchestrator falls back to generic behavior.",
                        "Fix the schema name or install the topic, then re-validate.", [], "", "rule",
                    )
                )
            elif t.state and t.state.lower() != "active":
                out.append(
                    Finding(
                        "L1-TOPIC-INACTIVE", "medium", "L1 References", "routing",
                        f"Referenced topic is {t.state}: {t.display_name or ref}", [ln], ref,
                        "Routing to an inactive topic never executes.", "Activate the topic or remove the reference.",
                    )
                )
    if doc.is_full_export:
        for g, ln in doc.global_refs():
            if g not in doc.globals_defined:
                out.append(
                    Finding(
                        "L1-GLOBAL-UNDEFINED", "high", "L1 References", "search",
                        f"Variable Global.{g} is used in instructions but never defined or set", [ln], g,
                        "The placeholder renders empty, so augmented search queries and user context are wrong.",
                        "Define the global variable or remove it from the instructions.",
                    )
                )
    for ln in doc.instructions:
        m = re.search(r"\{Global\.(\w+)\}\s+([a-z])\b(?!['’\w])", ln.text)
        if m and m.group(2) not in ("a",):
            out.append(
                Finding(
                    "L1-STRAY-TOKEN", "medium", "L1 References", "search",
                    f"Stray character '{m.group(2)}' after {{Global.{m.group(1)}}}", [ln.no], ln.text.strip(),
                    f"Examples are copied literally by the model, so search queries can end with a stray "
                    f"'{m.group(2)}' (e.g. 'public holidays in US {m.group(2)}'), lowering retrieval quality.",
                    f"Remove the stray '{m.group(2)}' after {{Global.{m.group(1)}}}.",
                    ["What are the public holidays?"], "KI-006",
                )
            )
    counts: dict[str, list[int]] = {}
    for g, ln in doc.global_refs():
        counts.setdefault(g, []).append(ln)
    for g, lns in counts.items():
        if re.search(r"guardrail|instruction|rules|policy", g, re.I):
            out.append(
                Finding(
                    "L1-HIDDEN-INSTRUCTION", "medium" if len(lns) > 1 else "low", "L1 References", "synthesis",
                    f"Instruction text injected via Global.{g}" + (f" ({len(lns)} times)" if len(lns) > 1 else ""),
                    lns, f"{{Global.{g}}}",
                    "Behavior changes made inside this variable are invisible in instruction diffs and length "
                    "checks; injecting it more than once doubles its token cost and weight.",
                    "Reference it once, and version its value with the agent (or move it into a DA skill) so "
                    "changes are reviewable.",
                    [], "KI-030",
                )
            )
    return out


def _rule_contract(doc: AgentDoc, baseline: AgentDoc | None) -> list[Finding]:
    out: list[Finding] = []
    present = core_presence(doc)
    base_present = core_presence(baseline) if baseline else {}
    for block in CORE_BLOCKS:
        if present[block["id"]]:
            continue
        in_base = bool(base_present.get(block["id"])) if baseline else True
        if not in_base:
            continue
        where = ""
        if baseline and base_present.get(block["id"]):
            s, e, title = block_range(baseline, base_present[block["id"]][0])
            where = f" (template '{title}', lines {s}–{e})"
        out.append(
            Finding(
                f"L2-{block['id'].split('.')[1].upper()}-MISSING", block["severity"], "L2 ESS core contract",
                block["stage"], f"ESS core block missing: {block['title']}{where}",
                [doc.instructions[0].no, doc.instructions[-1].no], "(not present)",
                block["impact"], block["fix"], block["tests"], block["ki"],
            )
        )
    return out


def _pattern_rules(doc: AgentDoc) -> list[Finding]:
    out: list[Finding] = []
    L = doc.instructions

    hit = _first(L, r"override(s)? every other instruction|overrides? all other instructions|take precedence over (all|every)"
                    r"|overrides? (any |all |the )?(conflicting|other|previous|preceding)\b.{0,25}instructions|cannot be bypassed")
    if hit:
        out.append(Finding(
            "L3-ABSOLUTE-OVERRIDE", "high", "L3 Conflicts", "synthesis",
            "'Override everything' language makes prohibitions beat routing, grounding and helpfulness",
            [hit.no], hit.text.strip(),
            "The model resolves every ambiguity toward refusal, so supported questions get declined and how-to "
            "flows stop before offering an action.",
            "Delete the override sentence. Scope each prohibition to the specific claim it guards against.",
            ["How many vacation days do I get?", "How do I report a payroll issue?"], "KI-011",
        ))

    hit = _first(L, r"(country|region|state)[-, ]+.{0,40}specific question unless")
    if hit:
        out.append(Finding(
            "L3-JURISDICTION-REFUSAL", "high", "L3 Conflicts", "synthesis",
            "Region rule refuses company-policy answers when the source document is global",
            [hit.no], hit.text.strip(),
            "Employees outside the HQ country get 'I couldn't find that' for policies that are global and "
            "documented once.",
            "For laws, statutory entitlements, or legal deadlines, answer only if retrieved content covers the "
            "user's jurisdiction. For company policy, answer from the retrieved policy and say when it applies "
            "globally.",
            ["What is the bereavement leave policy?", "Can I work from another country for two weeks?"], "KI-012",
        ))

    for ln in _match(L, r"(do not|don't|never)\s+(append|add|include|end with)[^.]{0,80}\b(offer|invitation|next step)"):
        if re.search(r"\bunless\b|\bexcept\b|when the user asks how|\blinks?\b", ln.text, re.I):
            continue
        extra = [x.no for x in _match(L, r"rewording a prohibited ending|offer commits")]
        out.append(Finding(
            "L4-NO-ACTION-OFFER", "high", "L4 Scope collisions", "tool",
            "Blanket 'never offer' rule blocks the how-to → action path",
            sorted({ln.no, *extra}), ln.text.strip(),
            "After 'How do I…' answers the agent can never offer a supported action, so Create Case and "
            "Workday update topics are under-used and employees leave without completing the task.",
            "Don't add generic closings or unsolicited offers. When the user asks how to do something a "
            "configured topic can perform (for example, creating an HR case), end by offering that action once.",
            ["How do I report a payroll issue?", "How do I change my direct deposit?"], "KI-014",
        ))
        break

    for ln in _match(L, r"\b(do not|don'?t|never)\s+(answer|respond to|act on|help with|assist with)\b[^.]{0,60}\b(questions|requests)\b"):
        if re.search(r"advice|opinion|personal (financial|legal|medical)|yourself", ln.text, re.I):
            continue
        broad = re.findall(r"\b(polic(?:y|ies)|eligibility|benefits?|expenses?|reimbursements?|visas?|status|paraphrases|hypotheticals|follow-ups)\b", ln.text, re.I)
        subject = re.search(r"\b(?:on|with|to)\s+([\w\s/-]{3,40}?)\s+(?:questions|requests)", ln.text, re.I)
        if not subject:
            continue
        subj = subject.group(1).strip()
        pre = next((x for x in L if abs(x.no - ln.no) <= 3 and re.search(r"before (profile lookup|any tool call|routing)", x.text, re.I)), None)
        collateral = {
            "expenses": "How do I get reimbursed for travel expenses?",
            "reimbursements": "How do I submit an expense reimbursement?",
            "visas": "Does the company sponsor work visas?",
            "benefits": "What benefits do I get when I join?",
            "paraphrases": "I'm moving to a new team next month — what changes for me?",
            "status": "What's the status of my HR case?",
        }
        tests = [f"What is the {subj} policy?"] + [q for k, q in collateral.items()
                                                  if any(k.rstrip("s") in b.lower() for b in broad)][:4]
        out.append(Finding(
            "L4-BLANKET-SUBJECT-BLOCK", "high" if len(broad) >= 3 else "medium", "L4 Scope collisions", "routing",
            f"Blanket block on '{subj}' reaches beyond the subject",
            sorted({ln.no, *([pre.no] if pre else [])}), ln.text.strip()[:260],
            f"The trigger list ({', '.join(sorted({b.lower() for b in broad})) or 'broad wording'}) also matches questions "
            f"that aren't about {subj}, so the agent refuses them with no search, citation, or action offer."
            + (" Running it 'before profile lookup or any tool call' puts it ahead of the routing tree." if pre else ""),
            f"Keep the intent but scope it: 'If the question is specifically about {subj} (for example, a {subj} package "
            f"or {subj} status), reply: <approved message + support route>. Questions about general expenses, "
            f"reimbursements, visas, or benefits that aren't part of the {subj} follow the routing tree as usual.' "
            "Add the must-not-change questions below to your test set.",
            tests, "KI-012",
        ))
        break

    aug = [ln for ln in L if "->" in ln.text and re.search(r"\{Global\.\w*(Level|Title|Grade)\w*\}", ln.text)]
    if aug:
        out.append(Finding(
            "L5-OVER-AUGMENTED-QUERY", "medium", "L5 Search impact", "search",
            "Job level/title is appended to search queries",
            [x.no for x in aug], aug[0].text.strip(),
            "Queries like 'training programs for L5' don't match knowledge pages that aren't organized by "
            "level, causing knowledge misses and generic answers.",
            "Add the user's country only for policies that vary by country (holidays, leave, benefits, payroll). "
            "Add job level only for level-specific programs or eligibility. Otherwise search with the user's words.",
            ["What are some good training programs?", "Where can I find leadership courses?"], "KI-005",
        ))

    rule = _first(L, r"(apply|add|use) (role and )?country context only when")
    varies = r"holiday|leave|benefit|payroll|tax|pension|vacation|time off"
    always = [ln for ln in L if "->" in ln.text and "Country" in ln.text and "{Global." in ln.text
              and not re.search(varies, ln.text, re.I)]
    if rule and always:
        out.append(Finding(
            "L3-SELF-CONTRADICTION", "medium", "L3 Conflicts", "search",
            "Rule says add country only when it changes the answer; examples always add it",
            [rule.no, *[x.no for x in always]], rule.text.strip()[:200],
            "Conflicting rule and examples make query augmentation non-deterministic across runs.",
            "Make the examples match the rule: keep country for holidays/leave, drop it for global topics.",
            ["What are the public holidays?", "What does PTO stand for?"], "KI-004",
        ))

    for ln in _match(L, r"\broute all\b.{0,200}\bnot\b.{0,120}\b(mcp|tools?)\b"):
        out.append(Finding(
            "L6-HARD-ROUTE-EXCLUDES-TOOLS", "medium", "L6 Tool-call impact", "tool",
            "Hard route excludes tools that follow-up questions need",
            [ln.no], ln.text.strip(),
            "Follow-ups that need the employee's own data (start date, cost center, case status) can't reach "
            "Workday/MCP tools and fall back to policy text.",
            "Route policy questions to the named source, and allow the matching Workday/MCP read tool for the "
            "employee's own data.",
            ["When does my relocation start?", "What is my new cost center after relocation?"], "KI-016",
        ))

    mandated = _match(L, r"\balways invoke\b")
    if len(mandated) >= 3:
        out.append(Finding(
            "L6-MANDATED-TOOL-CHAIN", "medium", "L6 Tool-call impact", "tool",
            f"{len(mandated)} tools are mandated before answering",
            [x.no for x in mandated], mandated[0].text.strip()[:200],
            "Every turn pays for the full tool chain (latency, throttling, tool budget) even for simple how-to "
            "questions.",
            "Gate each mandatory tool on the intent that needs it (e.g., device list only for device issues).",
            ["How do I set up my out-of-office reply?", "My laptop won't connect to Wi-Fi"], "KI-018",
        ))

    hit = _first(L, r"(blank|missing|unavailable)[^.]{0,40}\bas (non-manager|no|false)\b")
    if hit:
        out.append(Finding(
            "L6-UNSAFE-DEFAULT", "medium", "L6 Tool-call impact", "routing",
            "Missing profile value silently defaults to 'not a manager'",
            [hit.no], hit.text.strip(),
            "When the profile lookup returns empty, real managers get employee-only answers with no signal "
            "that anything went wrong.",
            "Treat Yes as manager and No as non-manager. If blank and the answer differs for managers, ask once: "
            "'Are you asking as a people manager?' Otherwise give employee guidance.",
            ["When do I involve HR in a performance improvement plan?", "How do I approve my team's time off?"],
            "KI-015",
        ))

    hit = _first(L, r"before considering other topics")
    if hit:
        out.append(Finding(
            "L7-PREEMPTIVE-ROUTE", "medium", "L7 Safety & RAI", "routing",
            "Sensitive-topic route runs before all routing with a very broad trigger",
            [hit.no], hit.text.strip()[:220],
            "'Indirect expressions of distress' also match ordinary stress about leave, pay, or deadlines, "
            "hijacking routine HR questions into the sensitive-topic flow.",
            "Keep the ESS sensitive-topic block unchanged and add customer resources inside it; trigger on "
            "explicit self-harm signals, not general stress.",
            ["I'm stressed about my leave balance, how many days do I have?", "This deadline is killing me, how do I request an extension?"],
            "KI-008",
        ))

    hit = _first(L, r"handbook section \d")
    if hit:
        out.append(Finding(
            "L8-LITERAL-EXAMPLE", "low", "L8 Style", "synthesis",
            "Citation example is likely copied literally",
            [hit.no], hit.text.strip(),
            "The model imitates 'According to employee handbook section 4.2…' even when no such section exists.",
            "Replace with a placeholder: 'Cite the article title you used, e.g. According to <article title>…'.",
            ["What is the dress code?"], "KI-010",
        ))

    sensitive = r"har+as+|discriminat|retaliat|self[- ]?harm|suicid|abuse|threat|unsafe|assault|whistle"
    for ln in L:
        t = ln.text
        if re.search(sensitive, t, re.I) and re.search(r"\b(don'?t|do not|never|avoid|refuse|decline)\b.{0,40}\b(answer|respond|reply|discuss|help)|^\s*-?\s*answer\b", t, re.I) \
                and not re.search(r"hand ?off|escalat|route|people team|hr support|compliance", t, re.I):
            out.append(Finding(
                "L7-SENSITIVE-SUPPRESSION", "high", "L7 Safety & RAI", "routing",
                "Rule changes how the agent handles a sensitive-subject disclosure",
                [ln.no], t.strip(),
                "Harassment, discrimination, and safety disclosures need the approved handoff path. A rule that "
                "suppresses or scripts the reply (e.g. 'say I can help with that') can skip reporting channels and "
                "anti-retaliation guidance, or send the planner into unrelated tools (profile lookup) first.",
                "Remove this line. If the goal is to route these disclosures, add inside '#Handoff and emotional "
                "intelligence': 'For harassment, discrimination, or retaliation concerns: acknowledge, share the "
                "approved reporting channels from the policy, and offer the HR support route. Don't investigate or "
                "judge the situation.'",
                ["My manager is harassing me", "I'm being treated unfairly because of my religion",
                 "I reported an issue and now I'm being excluded"], "KI-008",
            ))
            break

    for k, ln in enumerate(L):
        if re.fullmatch(r"\s*(never|always|do not|don'?t|rules?)\s*:\s*", ln.text, re.I):
            nxt = next((x for x in L[k + 1:] if x.text.strip()), None)
            prev_head = next((x for x in reversed(L[:k]) if x.text.lstrip().startswith("#")), None)
            ambiguous = nxt is not None and re.match(r"\s*-\s*(answer|say|respond|reply|help|provide)\b", nxt.text, re.I)
            if nxt is None or nxt.text.lstrip().startswith("#") or not nxt.text.lstrip().startswith("-") or ambiguous:
                where = f" under '{prev_head.text.strip()}'" if prev_head else ""
                out.append(Finding(
                    "L3-DANGLING-HEADER", "medium", "L3 Conflicts", "synthesis",
                    f"Standalone '{ln.text.strip()}' rule{where} reads two ways",
                    [ln.no] + ([nxt.no] if nxt else []), ln.text.strip() + (" ⏎ " + nxt.text.strip()[:160] if nxt else ""),
                    "A bare 'Never:' followed by a positive verb ('- answer … and say …') can be read as a prohibition "
                    "or as an instruction, so behavior flips between runs. Placed outside the section it belongs to, it "
                    "also competes with the existing 'Never:' list in that section.",
                    "Delete the standalone header and write one complete sentence inside the section it belongs to "
                    "(for conduct concerns: '#Handoff and emotional intelligence').",
                    [], "KI-004",
                ))
    return out


def _rule_config(doc: AgentDoc, baseline: AgentDoc | None) -> list[Finding]:
    out: list[Finding] = []
    web_on = [k for k, v in (doc.capabilities or {}).items() if v and re.search(r"search the whole web|web search", k, re.I)]
    if web_on:
        out.append(Finding(
            "L5-WEB-SEARCH-ON", "high", "L5 Search impact", "search",
            "Web search is on for an HR agent that must use only approved sources",
            [], f"Capability: {web_on[0]}",
            "Answers can be grounded on public websites instead of company policy, contradicting the grounding rules "
            "and producing confident but non-authoritative HR answers.",
            "Turn off 'Search the whole web' on the Configure page (Knowledge → Web search). If specific public sites "
            "are needed, add only those URLs as knowledge sources.",
            ["What is the parental leave policy?", "How many vacation days do I get?"], "KI-002",
        ))
    mod = (doc.content_moderation or "").lower()
    base = (baseline.content_moderation if baseline else "Medium") or "Medium"
    if mod and mod != base.lower():
        risk = (
            "High moderation on HR content (termination, harassment, conduct) blocks benign answers mid-generation "
            "and the user sees a generic 'sorry' (ContentFilteredException)."
            if mod == "high"
            else "Low moderation reduces filtering on sensitive HR content."
        )
        out.append(Finding(
            "L7-MODERATION-DRIFT", "medium", "L7 Safety & RAI", "rai",
            f"Content moderation is {doc.content_moderation} (template: {base})",
            [doc.content_moderation_line], f"contentModeration: {doc.content_moderation}", risk,
            f"Use {base} unless policy requires otherwise; if High is required, narrow knowledge sources so simple "
            "questions don't retrieve conduct/termination policy text.",
            ["How do I recognize coworkers?", "What happens if I report harassment?"], "KI-020",
        ))
    if doc.knowledge_sources >= 20:
        out.append(Finding(
            "L5-BROAD-KNOWLEDGE", "low", "L5 Search impact", "rai",
            f"{doc.knowledge_sources} knowledge sources are searched together",
            [], f"{doc.knowledge_sources} KnowledgeSourceComponent entries",
            "Simple questions pull in unrelated policy text (ethics, security, conduct), diluting answers and "
            "raising content-filter risk.",
            "Write specific knowledge source descriptions and scope rarely-needed sources to topics.",
            ["How do I recognize coworkers?"], "KI-020",
        ))
    return out


_DESC_STOP = {"this", "that", "with", "from", "user", "users", "when", "will", "topic", "their", "about", "employee",
              "employees", "information", "should", "used", "using", "have", "into", "other", "they", "them", "only",
              "such", "also", "request", "requests", "asks"}


def _rule_pipeline_gaps(doc: AgentDoc) -> list[Finding]:
    """Planner, search, synthesis and render gotchas from the ESS customer-customization model."""
    out: list[Finding] = []
    L = doc.instructions

    # 1 Planner: topic descriptions the planner can't tell apart.
    words = lambda s: {w for w in re.findall(r"[a-z]{4,}", s.lower())} - _DESC_STOP
    active = [t for t in doc.topics.values() if t.description and (t.state or "Active") == "Active"]
    pairs = []
    for i, a in enumerate(active):
        A = words(a.description)
        for b in active[i + 1:]:
            B = words(b.description)
            if A and B and len(A & B) / len(A | B) >= 0.8:
                pairs.append((a, b, len(A & B) / len(A | B)))
    if pairs:
        pairs.sort(key=lambda p: -p[2])
        shown = pairs[:5]
        out.append(Finding(
            "L1-TOPIC-OVERLAP", "medium", "L1 References", "tool",
            f"{len(pairs)} pair(s) of topics have near-identical descriptions",
            sorted({x for a, b, _ in shown for x in (a.description_line, b.description_line) if x}),
            "; ".join(f"{a.display_name} ≈ {b.display_name} ({round(j * 100)}% same words)" for a, b, j in shown),
            "The planner picks topics from their descriptions. When two read the same, it picks one at random or the "
            "wrong one (for example, the US time-off topic for a Philippines employee), so the answer path is wrong "
            "from the start.",
            "Make each description say what is unique: who it's for, which data it reads or writes, and when NOT to "
            "use it (e.g. 'US employees only; for other countries use …').",
            [], "KI-001",
        ))
    vague = [t for t in active if len(t.description.split()) < 8]
    if vague:
        out.append(Finding(
            "L1-TOPIC-DESC-VAGUE", "low", "L1 References", "tool",
            f"{len(vague)} topic description(s) are too short for the planner to choose correctly",
            [t.description_line for t in vague[:8] if t.description_line],
            "; ".join(f"{t.display_name}: '{t.description}'" for t in vague[:4]),
            "A few words don't tell the planner when to use the topic, so it under-triggers or fires on unrelated questions.",
            "Describe the user intent, the data involved, and an example question in one or two sentences.",
            [],
        ))

    # 1 Planner: fallback escalation that skips knowledge search.
    hit = _first(L, r"\b(if|when)\b[^.]{0,40}\b(unsure|not sure|uncertain|in doubt|can'?t answer|cannot answer|don'?t know)\b"
                    r"[^.]{0,80}\b(create|open|raise|submit|log)\b[^.]{0,20}\b(case|ticket|incident)")
    if hit and not re.search(r"after (searching|a (kb|knowledge) search|retry)|no (kb|knowledge) result", hit.text, re.I):
        out.append(Finding(
            "L4-FALLBACK-ESCALATE", "medium", "L4 Scope collisions", "routing",
            "'When unsure, open a ticket' sends questions to escalation before knowledge search",
            [hit.no], hit.text.strip()[:240],
            "The planner treats any uncertainty as a reason to create a case, so answerable questions become "
            "'Please open a ticket' and case volume rises.",
            "Escalate only after the knowledge search and one retry find nothing: 'If KB search and one retry don't "
            "answer it, offer to create a case.'",
            ["How do I enroll in benefits?", "What is the travel expense limit?"], "KI-003",
        ))

    # 2 Search: query rewriting / forced single source.
    hit = _first(L, r"\b(translate|rewrite|rephrase|expand|append|prepend)\b[^.]{0,40}\b(the |every |each |all )?(search )?(query|queries|search terms?|keywords?)\b")
    if hit:
        out.append(Finding(
            "L5-QUERY-REWRITE", "medium", "L5 Search impact", "search",
            "Instruction rewrites the search query",
            [hit.no], hit.text.strip()[:240],
            "Rewritten or translated queries drift from the words in the source documents, so the right document "
            "isn't returned and the answer is built on weaker evidence.",
            "Search with the user's words. Add context (country, role) only when the policy varies by it.",
            ["What is the parental leave policy?"], "KI-005",
        ))
    hit = _first(L, r"\b(only|exclusively)\s+(search|use|query|consult)\b[^.]{0,60}\b(site|source|knowledge|sharepoint|kb|page)")
    if hit and not re.search(r"organi[sz]ation-approved|approved sources|retrieved", hit.text, re.I):
        out.append(Finding(
            "L5-SINGLE-SOURCE", "medium", "L5 Search impact", "search",
            "All questions are forced to a single knowledge source",
            [hit.no], hit.text.strip()[:240],
            "Questions whose answer lives in another connected source return nothing, so users get incomplete "
            "answers or the fallback message.",
            "Scope the rule to the subject that source owns ('For benefits questions, search <source>'), and let other "
            "questions use all knowledge sources.",
            [], "KI-016",
        ))

    # 3 Synthesis: citations, length caps, conflicting fallbacks.
    for ln in _match(L, r"\b(do not|don'?t|never|avoid)\s+(include|add|show|provide|share|display|use|give)\s+(any\s+)?"
                        r"(links?|urls?|citations?|sources?|references?|hyperlinks?)\b|\bwithout (links|citations|sources)\b"):
        if re.search(r"(did not|didn'?t|you did not) use|not used|invent|made[- ]up|fabricat", ln.text, re.I):
            continue
        out.append(Finding(
            "L8-CITATION-STRIP", "high", "L8 Style", "synthesis",
            "Instruction removes links or citations from answers",
            [ln.no], ln.text.strip()[:240],
            "Employees can't verify the policy or open the source, and the answer looks ungrounded even when it "
            "isn't. It also conflicts with the ESS 'cite each contributing article' rule.",
            "Keep citations. If links are noisy, ask to 'cite only the articles used, by title'.",
            ["What is the bereavement leave policy?"], "KI-010",
        ))
        break
    hit = _first(L, r"\b(under|less than|no more than|at most|maximum of|limit (responses|answers) to|keep (it|responses|answers) "
                    r"(under|to|within))\s+\d+\s*(words?|sentences?|lines?|bullets?|characters?)\b|\bone[- ]sentence (answers?|responses?)\b")
    if hit:
        out.append(Finding(
            "L8-LENGTH-CAP", "medium", "L8 Style", "synthesis",
            "Hard length cap on every answer",
            [hit.no], hit.text.strip()[:240],
            "Policy answers with eligibility, steps, and exceptions get cut, so the model drops conditions or the "
            "citation to fit the limit.",
            "Prefer 'be concise; lead with the answer, then the key conditions and the source'. Apply hard limits only "
            "to specific answer types.",
            ["How does parental leave work for adoptive parents?"], "KI-010",
        ))
    fallback = r"couldn'?t find|could not find|unable to find|no (information|results|answer)|don'?t have (that|information)|open a (ticket|case)"
    msgs = {}
    for ln in L:
        for m in re.finditer(r"[\"“]([^\"”]{20,})[\"”]", ln.text):
            if re.search(fallback, m.group(1), re.I):
                msgs.setdefault(m.group(1).strip().lower(), ln.no)
    if len(msgs) >= 2:
        out.append(Finding(
            "L3-MULTI-FALLBACK", "medium", "L3 Conflicts", "synthesis",
            f"{len(msgs)} different 'not found' messages",
            sorted(msgs.values()), " | ".join(list(msgs)[:3]),
            "The same knowledge miss produces different replies ('I'm sorry…', 'Please open a ticket…'), so users "
            "can't tell a gap from an error and support can't measure it.",
            "Keep one abstain message, in '#Tool-first grounding', and reference it from other sections.",
            ["What's the policy on bringing pets to work?"], "KI-003",
        ))

    # 4 Render.
    hit = _first(L, r"\b(always|must)\b[^.]{0,40}\b(tables?|markdown|html|code blocks?|emojis?)\b|\b(use|format)[^.]{0,20}(\bhtml\b|<br>|<table>)")
    if hit:
        out.append(Finding(
            "L8-RENDER-FORMAT", "medium", "L8 Style", "render",
            "Forced output format may not render in Teams or M365 Copilot",
            [hit.no], hit.text.strip()[:240],
            "Forced tables, HTML or code blocks render badly on some surfaces and can break citations and adaptive "
            "cards.",
            "Ask for tables only for comparisons or multi-column data; otherwise use short paragraphs and bullets.",
            ["How do I request time off?"], "",
        ))
    return out


def validate(doc: AgentDoc, baseline: AgentDoc | None = None) -> list[Finding]:
    findings = (
        _rule_length(doc)
        + _rule_references(doc)
        + _rule_contract(doc, baseline)
        + _pattern_rules(doc)
        + _rule_pipeline_gaps(doc)
        + _rule_config(doc, baseline)
    )
    if baseline is not None:
        base_rules = {f.rule_id for f in _pattern_rules(baseline) + _rule_references(baseline) + _rule_pipeline_gaps(baseline)}
        for g in findings:
            if g.rule_id in base_rules or (g.current and line_in(baseline, g.current)):
                g.origin = "template"
    findings.sort(key=lambda f: (
        f.origin == "template", SEVERITY_ORDER.get(f.severity, 9), f.lines[0] if f.lines else 0,
    ))
    return findings
