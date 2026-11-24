"""Conversation investigation report in the style of RAI_Event_Analysis.txt.

Built only from evidence: Copilot Studio runtime telemetry, the agent's current instructions and config,
validator findings, and the known-issue catalog. Anything the telemetry can't show is listed explicitly.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ess_maker.doctor.known_issues import lookup
from ess_maker.doctor.rules import PIPELINE_ORDER

RULE = "=" * 62

_KIND_LABEL = {
    "FunctionInvocation": "Function Invocation Prompt (tool planning / answer generation)",
    "LlmRankerOpenAIConfiguration": "Knowledge ranking (hybrid LLM ranker)",
    "KnowledgeFiltering": "Knowledge filtering",
}

_STAGE_TITLE = {
    "rai": "Content safety filter blocked the answer",
    "tool": "A tool or connector call failed",
    "search": "Knowledge search did not return usable content",
    "routing": "The agent answered without searching knowledge or calling a tool",
    "synthesis": "Runtime succeeded; answer quality is shaped by instructions",
    "runtime": "The runtime reported a failure",
    "orchestrator": "Answered by the M365 Copilot orchestrator (declarative agent)",
}

# Conversation verdict → pipeline stage from the ESS customer-customization model.
_CONV_PIPE = {
    "routing": "1 Planner / tool selection", "tool": "1 Planner / tool selection",
    "search": "2 Search / tool execution", "runtime": "2 Search / tool execution",
    "synthesis": "3 Synthesis", "rai": "3 Synthesis",
}
_CONV_SEES = {
    "routing": "Ungrounded / poor answer", "tool": "Fallback / escalation", "search": "Incomplete answer",
    "runtime": "Fallback / escalation", "rai": "Overly cautious", "synthesis": "",
}

_STOP = {"what", "which", "where", "when", "does", "about", "with", "from", "that", "this", "have", "your", "there",
         "their", "would", "could", "should", "please", "tell", "explain", "company", "employee", "employees",
         # Generic HR words appear in many rules; matching on them alone isn't evidence the rule targets the question.
         "policy", "policies", "benefit", "benefits", "process", "procedure", "procedures", "request", "requests",
         "information", "support", "guidance", "question", "questions", "details", "status", "change", "changes"}
_REFUSAL = re.compile(r"unable to assist|can['’]t help|cannot help|can['’]t assist|not able to (help|assist)|"
                      r"couldn['’]t find|please contact the people team", re.I)


def cited_sources(response: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r"According to ([A-Za-z0-9][\w\-\.&]{2,80})", response):
        found.append(m.group(1).rstrip(".,"))
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9]+(?:[_-][A-Za-z0-9]+){1,10})\b", response):
        found.append(m.group(1))
    out: list[str] = []
    for s in found:
        if s not in out and len(s) > 6:
            out.append(s)
    return out[:12]


def instruction_check(question: str, response: str, agent) -> dict | None:
    """Does the agent's answer match what the instructions require for this question?"""
    if not question or agent is None:
        return None
    qwords = {w for w in re.findall(r"[a-z]{5,}", question.lower()) if w not in _STOP}
    rules = []
    for ln in agent.instructions:
        t = ln.text
        if re.search(r"\b(don'?t|do not|never|decline)\b[^.]{0,40}\b(answer|act|provide|assist|start)|unable to assist", t, re.I):
            hits = {w for w in qwords if w in t.lower() or w.rstrip("s") in t.lower()}
            if hits:
                rules.append({"line": ln.no, "text": t.strip(), "terms": sorted(hits)})
    if not rules:
        return None
    observed = "declined" if response and _REFUSAL.search(response[:600]) else ("answered" if response else "unknown")
    return {"rules": rules[:3], "expected": "decline", "observed": observed,
            "followed": None if observed == "unknown" else observed == "declined"}


def _t(ts) -> str:
    s = str(ts or "")
    return s[11:23] if len(s) >= 23 else s


def _section(title: str) -> list[str]:
    return ["", RULE, title.upper(), RULE]


def build(trace: dict, *, question: str = "", agent=None, findings: list | None = None,
          moderation: str = "", agent_name: str = "", response: str = "") -> str:
    v = trace["verdict"]
    bot = trace.get("bot", {})
    findings = findings or []
    maker = [f for f in findings if getattr(f, "origin", "maker") == "maker"]
    stage = v["stage"]
    orchestrated = stage == "orchestrator"
    related = {
        "rai": {"rai", "synthesis"}, "tool": {"tool", "routing"}, "search": {"search", "routing"},
        "routing": {"routing", "synthesis", "tool"}, "synthesis": {"synthesis", "routing", "search", "tool"},
        "runtime": {"tool", "platform"}, "orchestrator": {"routing", "synthesis", "search"},
    }.get(stage, {stage})
    linked = [f for f in maker if f.stage in related]
    check = instruction_check(question, response, agent)
    sources = cited_sources(response) if response else []

    L: list[str] = []
    L.append(f"Conversation Investigation — {trace['conversation_id']}")
    L.append(f"Source: Copilot Studio runtime telemetry ({trace['region']}), agent instructions, ESS validator"
             + (", agent response (pasted)" if response else ""))
    L.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    L += _section("User question")
    L.append(f'"{question}"' if question else
             "Not recorded in runtime telemetry (prompt text isn't logged). Add the question to include it here.")

    L += _section("What happened")
    L.append(_STAGE_TITLE.get(stage, v["headline"]) + ".")
    L.append(v["detail"])
    if orchestrated and response:
        verb = "declined" if check and check["observed"] == "declined" else "answered"
        L.append(f"The agent {verb}" + (f", citing {', '.join(sources[:4])}." if sources else "."))

    failed = ("1 Planner / tool selection" if check and check["followed"] is False
              else _CONV_PIPE.get(stage, ""))
    top_sees = next((f.to_dict()["user_sees"] for f in linked if f.to_dict()["pipeline"] == failed), "")
    L += _section("Where it went wrong")
    for p in PIPELINE_ORDER[:4]:
        if p == failed:
            mark = "◀ FAILED HERE"
        elif orchestrated and p != "1 Planner / tool selection":
            mark = "not visible in runtime telemetry"
        elif failed and PIPELINE_ORDER.index(p) > PIPELINE_ORDER.index(failed):
            mark = "affected (downstream)"
        else:
            mark = "ok" if failed else "no failure recorded"
        L.append(f"  {p:<30} {mark}")
    if check and check["followed"] is False:
        sees = "Answered a question the instructions say to decline (wrong answer path)"
    else:
        sees = top_sees or _CONV_SEES.get(stage, "")
    if sees:
        L.append(f"What the user saw: {sees}")
    searches = trace.get("searches", [])
    totals = [s for s in searches if s["source"].startswith("UniversalSearch")]
    if totals:
        s = totals[-1]
        L.append(f"Knowledge search returned {s['results']} results; {s['kept']} passed filtering.")
    if trace.get("exceptions"):
        e = trace["exceptions"][0]
        L.append(f"Exception: {e.get('type')} in {e.get('activity')}.")

    if check:
        L += _section("Instruction check")
        for r in check["rules"]:
            L.append(f'Rule (line {r["line"]}): "{r["text"][:260]}"')
        L.append(f"Expected for this question: {check['expected']} (matches the rule on: {', '.join(check['rules'][0]['terms'])}).")
        if check["followed"] is None:
            L.append("Observed: unknown — paste the agent's response to check it.")
        else:
            L.append(f"Observed: {check['observed']}" + (f", citing {', '.join(sources[:4])}" if sources and check['observed'] == 'answered' else "") + ".")
            L.append("Result: " + ("INSTRUCTION FOLLOWED" if check["followed"] else "INSTRUCTION NOT FOLLOWED"))

    L += _section("Key details")
    rows = [
        ("Agent", agent_name or bot.get("schema", "")),
        ("Schema", bot.get("schema", "")),
        ("CDS Bot ID", bot.get("cds_bot_id", "")),
        ("Tenant ID", trace.get("tenant_id", "")),
        ("Environment ID", bot.get("environment_id", "")),
        ("Channel", f"{bot.get('channel', '')}" + (" (Design Mode — testing in studio)" if bot.get("design_mode") == "True" else "")),
        ("Cluster/Geo", trace.get("region", "")),
        ("Published version", ", ".join(trace.get("published_versions", [])) or "not reported"),
        ("Definition served", ", ".join(trace.get("definition_versions", [])) or "not reported"),
        ("Planner model", ", ".join(trace.get("planner_model_series", [])) or "not reported"),
        ("Models used", ", ".join(sorted({str(c['model']) for c in trace.get('llm_calls', []) if c.get('model')})) or "not reported"),
        ("RAI policies", ", ".join(sorted({str(c['rai_policy']) for c in trace.get('llm_calls', []) if c.get('rai_policy')})) or "not reported"),
        ("Content moderation", moderation or "not available (read the agent config to include it)"),
        ("Turns processed", str(len(trace.get("turns", [])))),
    ]
    w = max(len(k) for k, _ in rows) + 2
    L += [f"{k + ':':<{w}}{val}" for k, val in rows]

    llm = trace.get("llm_calls", [])
    usage = trace.get("usage", [])
    L += _section(f"How it unfolded — {len(llm)} LLM call{'s' if len(llm) != 1 else ''}" + (" in the Copilot Studio runtime" if orchestrated else ""))
    if not llm:
        L.append("Planning and answer generation ran in the M365 Copilot orchestrator; the Copilot Studio runtime "
                 "recorded no LLM calls." if orchestrated else
                 "No LLM calls were recorded by the Copilot Studio runtime for this conversation.")
    for k, c in enumerate(llm, 1):
        ok = c.get("outcome") == "Success" and (c.get("stop") not in ("No Completion",) or not c.get("drop_reason"))
        status = "PASSED" if ok and not c.get("drop_reason") else "BLOCKED" if c.get("drop_reason") else "CHECK"
        L.append("")
        L.append(f"LLM Call {k}: {_KIND_LABEL.get(c.get('kind'), c.get('kind'))} — {status}")
        L.append(f"  Model:         {c.get('model')}")
        L.append(f"  RAI Policy:    {c.get('rai_policy')}")
        L.append(f"  FinalStopCode: {c.get('stop')}" + ("  (streaming ended without a completion record)" if c.get("stop") == "No Completion" else ""))
        if c.get("drop_reason"):
            L.append(f"  DropReason:    {c.get('drop_reason')}")
        u = next((x for x in usage if str(x["t"]) >= str(c["t"])), None)
        if u:
            L.append(f"  Tokens:        {u['completion']} completion, {u['prompt']} prompt, {u['total']} total")
        L.append(f"  Elapsed:       {c.get('elapsed_ms')}ms")
        L.append(f"  Time:          {_t(c.get('t'))} UTC")
    fc = trace.get("filter_counts", [])
    if fc:
        L.append("")
        L.append("Content filter results received: " + ", ".join(f"{x['count']} {x['kind']}" for x in fc[:8]))
    for s in trace.get("content_filter_signals", [])[:3]:
        L.append(f"  Signal: {s['message'][:160]}")

    L += _section("Knowledge sources searched")
    ks = trace.get("knowledge_source_types", {})
    if ks:
        L.append(f"Available source types: {ks.get('provided', '—')}")
        L.append(f"Source types used:      {ks.get('leveraged', '—') or 'none'}")
    if searches:
        for s in searches:
            L.append(f"  {_t(s['t'])}  {s['source']:<26} results={s['results']:<4} kept={s['kept']}")
    elif orchestrated:
        L.append("Knowledge search ran in the M365 Copilot orchestrator (not in this telemetry).")
        if sources:
            L.append("Sources cited in the response:")
            L += [f"  - {s}" for s in sources]
        else:
            L.append("Paste the agent's response to list the sources it cited.")
    else:
        L.append("No knowledge search ran in the Copilot Studio runtime for this conversation.")

    calls = trace.get("tool_calls", [])
    L += _section("Tools and topics called")
    if calls:
        for c in calls:
            L.append(f"  {_t(c['t'])}  {str(c.get('name'))[:70]:<70} {c.get('status')}")
    elif trace.get("tools"):
        L.append("  " + ", ".join(f"{x['type']}{' (error)' if x['error'] else ''}" for x in trace["tools"]))
    else:
        L.append("No tool or topic calls recorded.")

    L += _section("Timeline of events")
    events: list[tuple[str, str]] = []
    for x in trace.get("turns", []):
        events.append((str(x["t"]), f"Turn processed ({x.get('duration_ms')}ms, {x.get('result')})"))
    for c in calls:
        events.append((str(c["t"]), f"Tool call: {c.get('name')} — {c.get('status')}"))
    for c in llm:
        events.append((str(c["t"]), f"LLM {c.get('kind')} ({c.get('model')}) stop={c.get('stop')}"))
    for s in searches:
        events.append((str(s["t"]), f"Search {s['source']}: {s['results']} results, {s['kept']} kept"))
    for e in trace.get("exceptions", []):
        events.append((str(e["t"]), f"Exception {e.get('type')}: {str(e.get('detail'))[:90]}"))
    for ts, label in sorted(events)[:60]:
        L.append(f"{_t(ts):<14}{label}")

    L += _section("Why it likely happened")
    if check and check["followed"] is False:
        dv = ", ".join(trace.get("definition_versions", [])) or "unknown"
        L.append("The instructions say to decline this question, but the agent answered it from knowledge. Likely causes, "
                 "most likely first:")
        L.append(f"  1. The conversation ran on a definition version without the rule (served: {dv}). The test chat uses "
                 "the last SAVED draft; unsaved edits in the editor don't apply. Check Diagnose → Versions & their conversations.")
        L.append("  2. The rule competes with the routing tree, which sends 'policy, benefits programs, eligibility' "
                 "questions to KB search. Restrictions placed after the tree lose that tie in some turns.")
        L.append("  3. The response shown was generated before the latest save (stale test session).")
        L.append("")
    L.append(v["detail"])
    if linked:
        L.append("")
        L.append("Instruction lines that affect this stage:")
        for f in linked[:5]:
            lines = ", ".join(str(n) for n in f.lines[:3])
            d = f.to_dict()
            L.append(f"  - [{f.severity.upper()}] {d['pipeline']} — {f.title} (line {lines}). Users see: {d['user_sees']}")
            L.append(f"      \"{f.current[:180]}\"")
            L.append(f"      {f.impact}")
    else:
        L.append("No maker instruction changes affect this stage; compare with the ESS template and platform behavior.")

    top = next((f for f in linked if f.severity == "high"), linked[0] if linked else None)
    if top:
        L += _section(f"Critical finding: {top.title}")
        L.append(f'Instruction (line {top.lines[0] if top.lines else "?"}): "{top.current[:300]}"')
        L.append(top.impact)
        hist = lookup(top.known_issue).get("history") if top.known_issue else ""
        if hist:
            L.append("")
            L.append(f"Seen before: {hist}")

    L += _section("Recommended next steps")
    n = 0
    if check and check["followed"] is False:
        n += 1
        L.append(f"{n}. CONFIRM THE RULE IS SAVED, THEN RE-TEST IN A NEW CHAT")
        L.append("   Save the instructions, start a new test chat, and ask the same question. Compare the served "
                 "definition version in this report with the version that contains the rule.")
        n += 1
        L.append(f"{n}. MAKE THE RESTRICTION PART OF ROUTING")
        L.append("   Add it as the first item in '#Routing decision tree': 'Relocation package, eligibility, or status "
                 "questions: reply with the approved relocation message and the People team route; don't search KB.' "
                 "This removes the tie with the 'policy → KB search' rule.")
    for f in linked[:4]:
        n += 1
        L.append(f"{n}. {f.title.upper()}")
        L.append(f"   {f.fix}")
        if f.test_prompts:
            L.append(f"   Verify with: {' | '.join(f.test_prompts[:4])}")
    if stage == "rai":
        n += 1
        L.append(f"{n}. REVIEW CONTENT MODERATION AND KNOWLEDGE SCOPE")
        L.append("   Lower moderation one level if policy allows, and narrow sources so benign questions don't retrieve "
                 "conduct/termination policy text.")
    n += 1
    L.append(f"{n}. RE-TEST BEFORE PUBLISHING")
    L.append("   Run the verification prompts on the current and the fixed instructions (Doctor → Tune → Download eval set, "
             "then run it in Copilot Studio Evaluations).")

    L += _section("What this report can't see")
    gaps = ["Prompt and response text (not logged in runtime telemetry)" + ("; the response here was pasted by the maker." if response else ".")]
    if bot.get("channel") == "m365copilot":
        gaps.append("Declarative agent orchestration (planner choice, search query, knowledge results, final answer) runs in "
                    "the M365 Copilot orchestrator; use DevUI from /debug or M365 Copilot telemetry for that layer.")
    if not trace.get("published_versions") or trace.get("published_versions") == ["null"]:
        gaps.append("Agent version wasn't reported; confirm which instruction version served this conversation.")
    L += [f"  - {g}" for g in gaps]
    return "\n".join(L) + "\n"
