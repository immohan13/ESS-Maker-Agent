"""Sorry triage: why an ESS agent said "sorry" (or gave no answer) across many conversations.

Feeds on what makers can export themselves: Microsoft 365 Copilot DevUI conversation exports (every turn is
classified) and developer mode (-developer on) debug cards. Each "no answer" turn gets one cause, an owner and a
fix; causes are grouped so the team can work on the biggest one first, and the instruction lines behind the
maker-owned causes are counted across conversations.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ess_maker.doctor.agent_file import from_instructions
from ess_maker.doctor.report import _REFUSAL, instruction_check
from ess_maker.doctor.rules import validate

# Wider than the report's refusal pattern: ESS apologies and abstain messages seen in production.
SORRY = re.compile(
    _REFUSAL.pattern + r"|\bsorry\b|i (couldn['’]t|could not|can['’]t|cannot) find|couldn['’]t find a definitive answer|"
    r"no (relevant )?information (was )?found|i (don['’]t|do not) have (that|this|any) information|something went wrong|"
    r"unable to (find|provide|answer)", re.I)
SEARCH_TOOL = re.compile(r"search|find|retriev|knowledge|grounding", re.I)


@dataclass(frozen=True)
class Cause:
    id: str
    title: str
    owner: str
    fix: str
    stages: tuple[str, ...]  # instruction finding stages that can produce this cause


CAUSES = {c.id: c for c in [
    Cause("rule-decline", "Declined by an instruction rule", "Agent maker (instructions)",
          "The agent followed a restriction in the instructions. If employees should get an answer, narrow the rule to "
          "the exact subject and say what is still allowed.", ("routing", "rai", "synthesis")),
    Cause("no-search-decline", "Declined without searching", "Agent maker (instructions)",
          "No rule asks for this refusal and no tool ran. Check the routing and grounding rules: info questions must "
          "search knowledge or call a topic before answering.", ("routing", "tool", "synthesis")),
    Cause("no-tool-match", "No topic, action or knowledge matched", "Agent maker (descriptions and instructions)",
          "Rewrite the description of the topic or action that should handle this question using employees' words, and "
          "check the instructions don't steer it away from tools.", ("routing", "tool")),
    Cause("search-empty", "Knowledge search found nothing", "Knowledge owner (content), then maker",
          "Check a document that answers this exists and is indexed. If it does, compare the search query with the "
          "document title; remove query enrichment (job level, country) and add synonyms to the terminology rules.",
          ("search",)),
    Cause("found-but-declined", "Search found content but the agent still declined", "Agent maker (instructions)",
          "Either the kept documents are the wrong ones (fix the query) or the abstain rule is too strict: let the agent "
          "answer from what search returned and cite it.", ("synthesis", "search")),
    Cause("action-failed", "Topic, action or connector failed", "Connection or backend admin",
          "Open the failing connection (ServiceNow, Workday, ...) and check sign-in, permissions and the instance URL; "
          "the developer mode card shows the HTTP status.", ()),
    Cause("safety-block", "Safety filter blocked the answer", "Agent maker (moderation) and content owner",
          "Review content moderation and instruction wording that resembles a jailbreak ('ignore', 'override', 'never "
          "refuse'), then retest the question.", ("rai",)),
    Cause("throttled", "Copilot throttled the request", "Platform (no maker fix)",
          "Ask again later. If it keeps happening, report it with thumbs down.", ()),
    Cause("unknown", "Not enough evidence", "Collect more evidence",
          "Export the same conversation from DevUI; it shows the instructions that ran, the tools and the search funnel.",
          ()),
]}


@dataclass
class Item:
    source: str
    conversation: str
    question: str
    response: str
    sorry: bool
    cause: str = ""
    detail: str = ""
    agent: str = ""
    version: str = ""
    rules: list[str] = field(default_factory=list)


def _devui_items(parsed: dict, name: str, seen: dict) -> list[Item]:
    out = []
    for t in parsed["turns"]:
        resp = t["response"] or ""
        sorry = not resp.strip() or bool(SORRY.search(resp[:800])) or bool(t["rai"]["triggered"])
        it = Item(name, parsed["conversation_id"], t["question"], resp, sorry,
                  agent=t["agent_name"] or (t["agent"] or {}).get("compliantAgentName", ""),
                  version=(t["agent"] or {}).get("version", ""))
        out.append(it)
        if not sorry:
            continue
        served = from_instructions(t["instructions"], "served", "devui") if t["instructions"] else None
        check = instruction_check(t["question"], resp, served)
        searched = [c for c in t["tool_calls"] if SEARCH_TOOL.search(c["name"])]
        runs = t.get("searches") or []
        kept = sum(r.get("kept") or 0 for r in runs) if runs else (t["citations"].get("search_results") or 0)
        failed_steps = [e for e in t["errors"] if re.search(r"Mcs|Plugin|Connector|Action|Topic", e.get("service") or "", re.I)]
        if t["rai"]["triggered"]:
            it.cause, it.detail = "safety-block", ", ".join(t["rai"]["triggered"])
        elif check and check["observed"] == "declined":
            it.cause = "rule-decline"
            it.detail = "; ".join(f"line {r['line']}: {r['text'][:140]}" for r in check["rules"][:2])
        elif failed_steps:
            it.cause, it.detail = "action-failed", "; ".join(f"{e['service']} ({e['status']})" for e in failed_steps[:3])
        elif not t["tool_calls"]:
            it.cause = "no-search-decline"
            it.detail = f"{len(t['tools_offered'])} tools offered, none called"
        elif searched and kept == 0 and not t["sources"]:
            it.cause = "search-empty"
            qs = [q for c in searched for q in c.get("queries") or []] or [q for r in runs for q in r.get("queries") or []]
            it.detail = ("query: " + " | ".join(qs[:2])) if qs else "no results kept"
        elif searched:
            it.cause = "found-but-declined"
            titles = [d.get("title") or d.get("ref") for r in runs for d in r.get("kept_docs") or []]
            it.detail = ("kept: " + ", ".join(titles[:3])) if titles else f"{kept} result(s) kept"
        else:
            it.cause, it.detail = "unknown", "tools ran: " + ", ".join(c["name"] for c in t["tool_calls"][:3])
        if served and CAUSES[it.cause].stages:
            stages = set(CAUSES[it.cause].stages)
            hits = [f for f in validate(served) if f.origin == "maker" and f.stage in stages and f.severity in ("high", "medium")]
            for f in hits:
                seen.setdefault(f.rule_id, f)
            it.rules = sorted({f.rule_id for f in hits})
    return out


def _devmode_item(diag: dict, name: str, question: str, response: str) -> Item:
    card = diag.get("card") or {}
    resp = response or ""
    st = diag.get("stages") or {}
    it = Item(name, card.get("conversation_id") or "", question, resp,
              sorry=not resp or bool(SORRY.search(resp[:800])) or "failed" in st.values(),
              agent=card.get("agent_id") or "", version=card.get("agent_version") or "")
    if not it.sorry:
        return it
    cause_text = diag.get("likely_cause") or ""
    failed_actions = [a for a in card.get("exec_actions") or [] if a.get("ok") is False]
    if card.get("throttled") and not (card.get("exec_caps") or card.get("exec_actions")):
        it.cause = "throttled"
    elif failed_actions or re.search(r"HTTP (401|403|404|429|5\d\d)|timed out|didn't run", cause_text):
        it.cause = "action-failed"
    elif st.get("planner") == "failed" and (card.get("no_selected") or card.get("no_matched")):
        it.cause = "no-tool-match"
    elif st.get("planner") == "failed":
        it.cause = "no-search-decline"
    elif st.get("search") == "failed":
        it.cause = "search-empty"
    elif st.get("synthesis") == "failed":
        it.cause = "found-but-declined"
    else:
        it.cause = "unknown"
    it.detail = cause_text[:220]
    return it


def triage(sources: list[tuple[str, str, object]], instructions: str = "") -> dict:
    """sources: (kind, name, payload) with kind "devui" (parsed export) or "devmode" (diagnosis dict, question, response)."""
    items: list[Item] = []
    seen: dict = {}
    for kind, name, payload in sources:
        if kind == "devui":
            items += _devui_items(payload, name, seen)
        else:
            diag, q, r = payload
            items.append(_devmode_item(diag, name, q, r))
    sorry = [i for i in items if i.sorry]
    counts = Counter(i.cause for i in sorry)
    current = from_instructions(instructions, "current") if instructions.strip() else None
    current_list = validate(current) if current else []
    current_findings = {f.rule_id: f for f in current_list}
    # Evidence without the instructions that ran (developer mode cards): link the current instructions' rules.
    for i in sorry:
        stages = set(CAUSES[i.cause].stages)
        if i.rules or not stages or not current_list:
            continue
        words = {w for w in re.findall(r"[a-z]{5,}", i.question.lower())}
        for f in current_list:
            if f.origin != "maker" or f.stage not in stages or f.severity not in ("high", "medium"):
                continue
            restriction = re.search(r"BLOCK|RESTRICT|REFUSAL|OVERRIDE|JURISDICTION", f.rule_id)
            if restriction and not any(w in (f.current or "").lower() for w in words):
                continue
            i.rules.append(f.rule_id)
    rule_hits: dict[str, set] = defaultdict(set)
    for i in sorry:
        for r in i.rules:
            rule_hits[r].add((i.conversation, i.question))
    return {"turns": len(items), "sorry": len(sorry), "counts": counts, "items": items,
            "rules": sorted(((r, len(v)) for r, v in rule_hits.items()), key=lambda x: -x[1]),
            "current_findings": current_findings, "served_findings": seen}


def _cell(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "\\|").strip()


def build(result: dict) -> str:
    items, sorry, counts = result["items"], [i for i in result["items"] if i.sorry], result["counts"]
    n = len(sorry)
    L = ["## ESS Doctor sorry triage", ""]
    L.append(f"{result['turns']} turn(s) analyzed; {n} ended in a sorry, an error or no answer"
             + (f" ({n * 100 // max(1, result['turns'])}%)." if result["turns"] else "."))
    if not n:
        L += ["", "No sorry answers in this evidence. Upload DevUI exports or developer mode cards of conversations "
                  "where the agent apologized or gave no answer."]
        return "\n".join(L)
    L += ["", "### Causes, biggest first", "", "| Cause | Turns | Share | Who fixes it | Fix |", "| --- | --- | --- | --- | --- |"]
    for cid, c in counts.most_common():
        cause = CAUSES[cid]
        L.append(f"| {cause.title} | {c} | {c * 100 // n}% | {cause.owner} | {_cell(cause.fix)} |")
    maker = sum(c for cid, c in counts.items() if CAUSES[cid].owner.startswith("Agent maker"))
    L += ["", f"**{maker} of {n}** sorry answers are caused by the agent's instructions or descriptions, which the maker can fix "
              "without waiting for content or backend changes."]
    if result["rules"]:
        L += ["", "### Instruction rules behind the maker-owned sorries", "",
              "| Rule | Sorry turns | What's wrong | Fix |", "| --- | --- | --- | --- |"]
        cf = result["current_findings"]
        ordered = sorted(result["rules"], key=lambda x: (bool(cf) and x[0] not in cf, -x[1]))
        for rid, k in ordered[:10]:
            f = cf.get(rid) or result["served_findings"].get(rid)
            title = f.title if f else "Found in the instructions that ran"
            if f is not None and f.lines:
                title += " (line " + ", ".join(map(str, f.lines[:3])) + ")"
            fix = f.fix if f else "Run the instruction check on the current instructions to see the line and fix."
            L.append(f"| {rid} | {k} | {_cell(title)[:160]} | {_cell(fix)[:220]} |")
        if cf:
            gone = [rid for rid, _ in result["rules"] if rid not in cf]
            if gone:
                L.append(f"\nAlready fixed in the current instructions: {', '.join(gone)}.")
    L += ["", "### Every sorry turn", "", "| # | Question | Agent said | Cause | Evidence |", "| --- | --- | --- | --- | --- |"]
    for k, i in enumerate(sorry, 1):
        L.append(f"| {k} | {_cell(i.question)[:120]} | {_cell(i.response)[:120] or '(no answer)'} | {CAUSES[i.cause].title} | {_cell(i.detail)[:180]} |")
    versions = sorted({(i.agent, i.version) for i in items if i.version})
    if versions:
        L += ["", "Agent versions in this evidence: " + ", ".join(f"{a or 'agent'} {v}" for a, v in versions) + "."]
    L += ["", "### Next steps", "",
          "1. Fix the biggest maker-owned cause first: run the instruction check on the current instructions and apply the fixes for the rules above.",
          "2. Send the content-owned questions (search found nothing) to the knowledge owner with the queries listed.",
          "3. Send connector failures to the connection admin with the developer mode card.",
          "4. Re-run the same questions after publishing and triage the new exports to confirm the sorry share dropped."]
    return "\n".join(L)
