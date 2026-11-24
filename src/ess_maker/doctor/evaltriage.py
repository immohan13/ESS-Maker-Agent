"""Triage Copilot Studio evaluation results: why each failed question failed, and who fixes it.

Input is the CSV Copilot Studio exports from an evaluation run (question, expectedResponse, actualResponse,
result_N, explanation_N, conversationId). Each failed question gets one cause:

- ``instruction``: the agent had what it needed but the instructions sent it the wrong way (e.g. searched the
  knowledge base for a personal-data question, or answered when it should have declined).
- ``scenario_off``: the topic that handles the question exists but is turned off.
- ``scenario_missing``: no topic in the agent handles the question.
- ``content_gap``: the right documents were searched, but they don't contain the answer.
- ``review``: the evidence doesn't point to a single cause.

The cause is decided from the expected route for the question (ESS routing: personal data and HR cases go to
Workday/ServiceNow topics, everything else to knowledge), the agent's topics and their on/off state, and what the
response shows (abstain message, cited documents, partial answer).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from ess_maker.doctor.agent_file import AgentDoc

# Scenarios ESS routes to a system topic, with how to recognise the question and the topic that serves it.
SCENARIOS = [
    {"key": "time_off_balance", "label": "Time-off / vacation balance", "system": "Workday",
     "ask": r"\b(vacation|time[- ]?off|pto|leave|sick)\b.{0,40}\b(left|balance|remaining|have|available|accrued)\b"
            r"|\bhow many\b.{0,30}\b(vacation|leave|pto|days off|sick)\b",
     "topic": r"vacation|time ?off|timeoff|leave balance|absence|pto",
     "ess_topic": "Workday Get Vacation Balance (ESS Workday HCM template)"},
    {"key": "hr_cases_list", "label": "List my HR cases", "system": "ServiceNow",
     "ask": r"\b(my|open|show|list|how many)\b.{0,30}\b(hr )?(cases?|tickets?|requests?)\b",
     "topic": r"get user cases|cases list", "ess_topic": "ServiceNow HRSD Get User Cases"},
    {"key": "hr_case_create", "label": "Open an HR case", "system": "ServiceNow",
     "ask": r"\b(open|create|raise|log|submit)\b.{0,20}\b(a |an )?(hr )?(case|ticket)\b",
     "topic": r"create case", "ess_topic": "ServiceNow HRSD Create Case"},
    {"key": "case_details", "label": "HR case status / updates", "system": "ServiceNow",
     "ask": r"\bHRC\d+|\b(status|update|updates)\b.{0,20}\bcase\b",
     "topic": r"case details|case updates", "ess_topic": "ServiceNow HRSD Get Case Details / Get Case Updates"},
    {"key": "profile_update", "label": "Update my personal details", "system": "Workday",
     "ask": r"\b(update|change)\b.{0,10}\bmy\b.{0,20}\b(phone|email|address|emergency contact|name)\b",
     "topic": r"update (phone|email|address|contact)|manage", "ess_topic": "Workday Update (phone / email / address)"},
    {"key": "workday_read", "label": "My Workday data", "system": "Workday",
     "ask": r"\bmy\b.{0,20}\b(cost center|employee id|compensation|salary|pay grade|anniversary|passport|visa|"
            r"certification|national id|company code|emergency contact)\b",
     "topic": r"workday get", "ess_topic": "Workday Get topics (ESS Workday HCM template)"},
]

_ABSTAIN = re.compile(r"couldn'?t find a definitive answer|could not find|contact the people team|"
                      r"i (don'?t|do not) have (that|this) information|unable to (find|provide)", re.I)
_PARTIAL = re.compile(r"\b(partly|partially|only (lists?|names|provides?)|cannot provide the specific|doesn'?t include|"
                      r"missing|not (include|contain)|directs the user to another source)\b", re.I)
_CLARIFY = re.compile(r"\b(do you mean|which (one|of these)|could you (clarify|specify))\b", re.I)
_CITE = re.compile(r'^\[\d+\]:\s*(\S+)(?:\s+"([^"]+)")?', re.M)
_INLINE = re.compile(r"<File>([^<]+)</File>")


@dataclass
class Triage:
    question: str
    expected: str
    actual: str
    result: str
    explanation: str
    conversation_id: str
    cause: str = ""
    owner: str = ""
    headline: str = ""
    evidence: list[str] = field(default_factory=list)
    fix: str = ""
    lines: list[dict] = field(default_factory=list)
    scenario: str = ""
    docs: list[str] = field(default_factory=list)
    secondary: str = ""


CAUSE_LABEL = {"instruction": "Instruction issue", "scenario_off": "Scenario turned off",
               "scenario_missing": "Scenario doesn't exist", "content_gap": "Content doesn't exist",
               "review": "Needs review"}
OWNER = {"instruction": "Maker — instructions", "scenario_off": "Maker / admin — topics and connections",
         "scenario_missing": "Maker / admin — add the scenario", "content_gap": "Content owner — knowledge source",
         "review": "Maker"}


def parse_results(text: str) -> list[dict]:
    """Rows from a Copilot Studio evaluation results CSV (or a plain question/expected CSV)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    out = []
    for r in rows:
        low = {k.strip().lower(): (v or "") for k, v in r.items() if k}
        q = next((v for k, v in low.items() if k in ("question", "prompt", "utterance")), "").strip()
        if not q:
            continue
        res = next((v for k, v in low.items() if k.startswith("result")), "")
        out.append({"question": q, "expected": low.get("expectedresponse") or low.get("expected response") or "",
                    "actual": low.get("actualresponse", ""), "result": res.strip(),
                    "explanation": next((v for k, v in low.items() if k.startswith("explanation")), ""),
                    "conversation_id": low.get("conversationid", "")})
    return out


def _topics(agent: AgentDoc) -> list[dict]:
    if agent.topic_list:
        return agent.topic_list
    return [{"name": t.display_name or t.schema, "schema": t.schema, "state": t.state or "Active",
             "description": t.description} for t in agent.topics.values()]


def _scenario(q: str) -> dict | None:
    return next((s for s in SCENARIOS if re.search(s["ask"], q, re.I)), None)


def _find_lines(agent: AgentDoc, *patterns: str) -> list[dict]:
    out = []
    for ln in agent.instructions:
        if any(re.search(p, ln.text, re.I) for p in patterns):
            out.append({"line": ln.no, "text": ln.text.strip()[:220]})
    return out[:2]


def _docs(actual: str) -> list[str]:
    names = [m.group(2) or m.group(1).rsplit("/", 1)[-1].split("?")[0] for m in _CITE.finditer(actual)]
    names += _INLINE.findall(actual)
    seen, out = set(), []
    for n in names:
        n = re.sub(r"\.aspx$", "", n.strip())
        if n and n.lower() not in seen:
            seen.add(n.lower()); out.append(n)
    return out[:5]


def triage_row(r: dict, agent: AgentDoc) -> Triage:
    t = Triage(r["question"], r["expected"], r["actual"], r["result"], r["explanation"], r["conversation_id"])
    if r["result"].lower() == "pass":
        t.cause = "pass"
        return t
    a = r["actual"]
    abstained, docs = bool(_ABSTAIN.search(a[:400])), _docs(a)
    t.docs = docs
    sc = _scenario(r["question"])
    if sc:
        t.scenario = sc["label"]
        matches = [x for x in _topics(agent) if re.search(sc["topic"], x["name"] + " " + x["schema"], re.I)
                   and not re.search(r"\bsystem\b", x["name"], re.I)]
        on = [x for x in matches if x["state"] == "Active"]
        if not matches:
            t.cause = "scenario_missing"
            t.headline = f"No topic in this agent handles “{sc['label']}”, so the agent can't look it up in {sc['system']}."
            t.evidence.append(f"Expected route: {sc['system']} topic (ESS routes personal data and HR cases to topics, not knowledge).")
            t.evidence.append(f"Topics checked: none match “{sc['label']}” among {len(_topics(agent))} topics.")
            t.fix = (f"Add the {sc['ess_topic']} topic and its {sc['system']} connection. Until it exists, the abstain "
                     "is the correct answer; add the question to your expected-abstain list so the eval doesn't count it "
                     f"as a quality failure, or point users to {sc['system']} self-service in the abstain message.")
        elif not on:
            t.cause = "scenario_off"
            names = ", ".join(x["name"] for x in matches[:3])
            t.headline = f"The topic for “{sc['label']}” exists but is turned off ({names})."
            t.evidence.append(f"Expected route: {sc['system']} topic.")
            t.evidence.append(f"Topic state: {names} — Inactive.")
            t.fix = (f"Turn on {names} (and the {sc['system']} system topics it calls) in Copilot Studio → Topics, "
                     f"check the {sc['system']} connection, publish, then re-run this question.")
        elif docs and not re.search(r"pulled from (workday|servicenow)", a, re.I):
            t.cause = "instruction"
            t.headline = f"The {sc['system']} topic is on, but the agent searched the knowledge base instead."
            t.evidence.append(f"Expected route: {sc['system']} topic ({on[0]['name']}, Active).")
            t.evidence.append("Response cites knowledge documents: " + ", ".join(docs[:3]) + ".")
            t.lines = _find_lines(agent, re.escape(sc["system"]), r"self data read|hr cases")
            t.fix = (f"Make the routing rule for this question explicit: add the question's wording to the {sc['system']} "
                     f"route and check the topic description of {on[0]['name']} covers it.")
        else:
            t.cause = "review"
            t.headline = f"The {sc['system']} topic is on but the answer still failed; check the topic's output."
            t.fix = "Open the conversation in the Conversation analyzer (or Trace X-ray) to see whether the topic ran and what it returned."
        if docs and abstained and t.cause in ("scenario_missing", "scenario_off"):
            t.secondary = ("Instruction issue too: before abstaining, the agent searched the knowledge base and "
                           f"cited {', '.join(docs[:2])}. ESS rules say personal data and cases never fall back to knowledge.")
            t.lines = _find_lines(agent, r"cross-class fallback", r"fallback")
        return t
    # Knowledge questions.
    if abstained and not docs:
        t.cause = "content_gap"
        t.headline = "Knowledge search found nothing that answers this question."
        t.evidence.append("Response is the abstain message with no cited documents.")
        t.fix = ("Check that a document covering this exists in the knowledge source and is indexed; if it doesn't, "
                 "add it. If it does, compare its title with the user's wording and add a synonym to the terminology rules.")
    elif _PARTIAL.search(r["explanation"]) or _PARTIAL.search(a):
        t.cause = "content_gap"
        doc = docs[0] if docs else "the cited document"
        t.headline = f"The agent found {doc}, but it doesn't contain the detail asked for."
        t.evidence.append("Cited: " + (", ".join(docs[:3]) or "—") + ".")
        t.evidence.append("Grader: " + r["explanation"][:220])
        t.fix = (f"Add the missing detail to {doc} (or publish a dedicated page for it), then re-run. No instruction "
                 "change will fix this; the agent is correctly refusing to fill the gap from model knowledge.")
    elif abstained and docs:
        t.cause = "content_gap"
        t.headline = f"Documents were found ({', '.join(docs[:2])}) but none answered the question."
        t.evidence.append("Response is the abstain message with citations, so search ran and the results didn't match.")
        t.fix = ("Check whether the answer is in those documents. If it isn't, add it. If it is, the title or wording "
                 "doesn't match the question; add a synonym rule or rename the section.")
    elif _CLARIFY.search(a):
        t.cause = "instruction"
        t.headline = "The agent asked a clarifying question instead of answering."
        t.lines = _find_lines(agent, r"bare umbrella", r"clarif")
        t.fix = "Narrow the clarification rule so this wording is treated as specific enough to search."
    else:
        t.cause = "review"
        t.headline = "The answer was judged off-target; the cause isn't clear from the result alone."
        t.evidence.append("Grader: " + r["explanation"][:220])
        t.fix = "Open the conversation in the Conversation analyzer to see the search query and the documents kept."
    return t


def triage(text: str, agent: AgentDoc) -> dict:
    rows = parse_results(text)
    has_results = any(r["result"] for r in rows)
    items = [triage_row(r, agent) for r in rows] if has_results else []
    failed = [t for t in items if t.cause != "pass"]
    counts = {k: sum(1 for t in failed if t.cause == k) for k in CAUSE_LABEL}
    return {
        "has_results": has_results,
        "evals": [{"prompt": r["question"], "expect": r["expected"]} for r in rows],
        "total": len(rows), "passed": len(items) - len(failed), "failed": len(failed),
        "counts": counts,
        "items": [{**t.__dict__, "cause_label": CAUSE_LABEL.get(t.cause, ""), "owner": OWNER.get(t.cause, "")}
                  for t in failed],
        "summary": summary_text(agent, items, counts) if has_results else "",
    }


def summary_text(agent: AgentDoc, items: list[Triage], counts: dict) -> str:
    failed = [t for t in items if t.cause != "pass"]
    L = [f"ESS Doctor — evaluation triage for {agent.name}",
         f"{len(items)} questions: {len(items) - len(failed)} passed, {len(failed)} failed.",
         "Causes: " + ", ".join(f"{CAUSE_LABEL[k]} {v}" for k, v in counts.items() if v) + ".", ""]
    for i, t in enumerate(failed, 1):
        L.append(f"{i}. “{t.question}” — {CAUSE_LABEL[t.cause]} (owner: {OWNER[t.cause]})")
        L.append(f"   What happened: {t.headline}")
        for e in t.evidence:
            L.append(f"   Evidence: {e}")
        if t.secondary:
            L.append(f"   {t.secondary}")
        for ln in t.lines:
            L.append(f"   Instruction line {ln['line']}: {ln['text']}")
        L.append(f"   Fix: {t.fix}")
        if t.conversation_id:
            L.append(f"   Conversation: {t.conversation_id}")
        L.append("")
    return "\n".join(L).rstrip() + "\n"


# ---- Before vs after: the same prompts run on two instruction versions ----

_DECLINE = re.compile(r"\bunable to (assist|help)\b|\bcan'?t (help|assist) with\b|\bnot able to (help|assist)\b|"
                      r"\bI can'?t (answer|provide|discuss)\b", re.I)


def judge(row: dict) -> tuple[bool, str]:
    """Pass/fail against the expected behaviour, not just the grader.

    The Copilot Studio answer-quality grader fails every correct decline as "not answered", so prompts whose expected
    response says the agent declines are judged by whether it declined; prompts that should be answered fail if the
    agent declined or the grader failed them.
    """
    exp, act = row["expected"], row["actual"]
    declined = bool(_DECLINE.search(act))
    if re.search(r"\bdeclines only\b|\bdeclin\w* (only )?the [\w-]+ part\b", exp, re.I):
        partial = declined and len(_DECLINE.sub("", act).strip()) > 160
        return partial, ("answered the rest and declined only the restricted part" if partial else
                         "declined the whole question" if declined else "didn't decline the restricted part")
    if re.search(r"\bdeclin", exp, re.I) and not re.search(r"\b(does not|doesn'?t|never|not|without)\s+declin", exp, re.I):
        return declined, "declined as expected" if declined else "answered a question it should decline"
    if declined:
        return False, "declined a question it should answer"
    if row["result"]:
        ok = row["result"].lower() == "pass"
        return ok, "answered" if ok else (row["explanation"].split(":")[0] or "grader failed it")
    return bool(act.strip()), "answered" if act.strip() else "no response"


def compare(before_csv: str, after_csv: str) -> dict:
    b = {r["question"].strip().lower(): r for r in parse_results(before_csv)}
    a = {r["question"].strip().lower(): r for r in parse_results(after_csv)}
    rows = []
    for key in list(b) + [k for k in a if k not in b]:
        rb, ra = b.get(key), a.get(key)
        vb = judge(rb) if rb else (None, "not run")
        va = judge(ra) if ra else (None, "not run")
        status = ("Fixed" if vb[0] is False and va[0] else "Regressed" if vb[0] and va[0] is False else
                  "Still passing" if vb[0] and va[0] else "Still failing" if vb[0] is False and va[0] is False else
                  "Not comparable")
        rows.append({"question": (rb or ra)["question"], "expected": (rb or ra)["expected"],
                     "before": {"pass": vb[0], "why": vb[1], "response": (rb or {}).get("actual", "")[:400]},
                     "after": {"pass": va[0], "why": va[1], "response": (ra or {}).get("actual", "")[:400]},
                     "status": status})
    order = {"Fixed": 0, "Regressed": 1, "Still failing": 2, "Still passing": 3, "Not comparable": 4}
    rows.sort(key=lambda r: order[r["status"]])
    both = [r for r in rows if r["status"] != "Not comparable"]
    return {"total": len(both),
            "before_pass": sum(1 for r in both if r["before"]["pass"]),
            "after_pass": sum(1 for r in both if r["after"]["pass"]),
            "counts": {k: sum(1 for r in rows if r["status"] == k) for k in order},
            "rows": rows}