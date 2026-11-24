"""Analyze a Copilot DevUI conversation export (Download → conversationHistory-<id>.json).

The export carries M365 Copilot orchestrator telemetry that Copilot Studio runtime logs don't have for
declarative agents: the instructions that actually ran, the tools offered to the planner and the calls it
made, model calls with tokens and latency, grounding/citation counts, RAI classifier results, and the
Copilot Studio topics that executed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ess_maker.doctor.agent_file import from_instructions
from ess_maker.doctor.report import RULE, cited_sources, instruction_check, _REFUSAL
from ess_maker.doctor.rules import PIPELINE_ORDER

_INSTR_MARK = re.compile(r"'s Instructions:\s*", re.I)
_SEARCH_TOOL = re.compile(r"search|find|retriev|knowledge|grounding", re.I)


def _json(s):
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s) if isinstance(s, str) and s[:1] in "{[" else None
    except ValueError:
        return None


def _text_parts(content: dict) -> str:
    vals = []
    for key in ("instructions", "parts"):
        for p in content.get(key) or []:
            if isinstance(p, dict) and isinstance(p.get("value"), str):
                vals.append(p["value"])
    return "\n".join(vals)


def _tool_call(t) -> dict:
    if isinstance(t, str):
        t = _json(t) or t
    if isinstance(t, list):
        t = t[0] if t else {}
    if not isinstance(t, dict):
        return {"name": str(t)[:80], "args": "", "queries": [], "domains": [], "ref_ids": []}
    fn = t.get("function") if isinstance(t.get("function"), dict) else {}
    name = t.get("name") or t.get("toolName") or t.get("pluginName") or fn.get("name") or t.get("recipient") or "?"
    args = t.get("arguments") or t.get("input") or t.get("parameters") or fn.get("arguments") or t.get("content") or ""
    parsed = _json(args) if isinstance(args, str) else args
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    queries, domains, ref_ids = [], [], []
    if isinstance(parsed, dict):
        for q in parsed.get("queries") or ([parsed["query"]] if parsed.get("query") else []):
            if isinstance(q, str):
                queries.append(q)
            elif isinstance(q, dict):
                queries.append(str(q.get("query") or q.get("q") or ""))
                if q.get("domain"):
                    domains.append(str(q["domain"]))
        ref_ids = [str(r.get("ref_id")) for r in parsed.get("results") or [] if isinstance(r, dict) and r.get("ref_id")]
    return {"name": str(name), "args": args[:600], "queries": [q for q in queries if q][:6],
            "domains": sorted(set(domains)), "ref_ids": ref_ids}


def _clean_title(s: str) -> str:
    s = re.sub(r"[\ue200-\ue2ff]*cite[\ue200-\ue2ff]*(?:turn\d+\w+?\d+[\ue200-\ue2ff]*)+", "", s or "")
    return re.sub(r"</?\w+>", "", s).strip()


def _search_runs(metrics: list[dict]) -> list[dict]:
    """Retrieval funnel per knowledge search, from the cross-plugin grounding reducer."""
    runs = []
    for m in metrics:
        if m.get("serviceName") != "CrossPluginGroundingDataReducer":
            continue
        g = _json(m.get("output")) or {}
        if (g.get("execution") or {}).get("iterationType") != "Search":
            continue
        snaps = {s.get("boundary"): s for s in g.get("snapshots") or [] if isinstance(s, dict)}
        begin, end = snaps.get("Begin") or {}, snaps.get("End") or {}
        stages = {s.get("stage"): s for s in g.get("stages") or [] if isinstance(s, dict)}
        found = {r.get("citationRefId"): _clean_title(r.get("title", "")) for r in begin.get("results") or []}
        kept = [{"ref": r.get("citationRefId"), "title": _clean_title(r.get("title", "")), "plugin": r.get("pluginName", "")}
                for r in end.get("results") or []]
        runs.append({
            "queries": [q.get("query") for q in g.get("queries") or [] if isinstance(q, dict) and q.get("query")],
            "found": begin.get("totalResults"), "kept": end.get("totalResults"),
            "admitted": (stages.get("SelectTopK") or {}).get("outputCount"),
            "by_plugin": begin.get("byPlugin") or {}, "kept_docs": kept, "titles": found,
            "notes": [s.get("actionReason", "") for k, s in stages.items() if k in ("SelectTopK", "FinalSelection")],
            "exit": (g.get("execution") or {}).get("exitPath", ""),
        })
    return runs


def _llm_call(m: dict) -> dict | None:
    inp, out = _json(m.get("input")), _json(m.get("output")) or {}
    am = (m.get("additionalMetrics") or {}).get("metrics") or {}
    model, purpose, tools, instructions = "", "LLM call", [], ""
    if isinstance(inp, dict):
        cfg = inp.get("configuration") or {}
        model = ((cfg.get("model") or {}).get("name") if isinstance(cfg.get("model"), dict) else "") or inp.get("model") or ""
        msgs = (inp.get("conversation") or {}).get("messages") or inp.get("messages") or []
        for msg in msgs:
            c = msg.get("content")
            text = _text_parts(c) if isinstance(c, dict) else (c if isinstance(c, str) else "")
            if isinstance(c, dict):
                for ns in c.get("tools_namespaces") or []:
                    tools += [f.get("name") for f in ns.get("functions") or ns.get("tools") or [] if isinstance(f, dict)]
            if not instructions and _INSTR_MARK.search(text or ""):
                instructions = _INSTR_MARK.split(text, 1)[1]
            if re.search(r"generating a title", text or "", re.I):
                purpose = "Chat name"
        if instructions or tools:
            purpose = "Reasoning loop (planner + answer)"
    if isinstance(out, dict) and not model:
        model = out.get("model") or ""
    response = out.get("modelResponse") if isinstance(out, dict) else ""
    if response is None and isinstance(out, dict) and out.get("choices"):
        response = ((out["choices"][0] or {}).get("message") or {}).get("content") or ""
    if purpose == "LLM call" and response and re.fullmatch(r"[a-z0-9_]+", response.strip()):
        purpose = "Model switcher"
    calls = [_tool_call(t) for t in (out.get("toolInvocations") or [])] if isinstance(out, dict) else []
    return {
        "purpose": purpose, "model": model, "latency_ms": m.get("latencyMilliseconds", 0), "status": m.get("status", ""),
        "prompt_tokens": am.get("PromptToken", out.get("promptTokenCount") if isinstance(out, dict) else None),
        "completion_tokens": am.get("CompletionToken", out.get("completionTokenCount") if isinstance(out, dict) else None),
        "cached_tokens": am.get("CachedToken"), "reasoning_tokens": am.get("ReasoningToken"),
        "response": (response or "")[:1200], "tool_calls": calls, "tools_offered": [t for t in tools if t],
        "thought": ((out.get("unprocessedChainOfThoughtSummary") or "") if isinstance(out, dict) else "")[:800],
        "instructions": instructions,
    }


def _mcs_topics(m: dict) -> tuple[list[str], list[str]]:
    data = _json(m.get("output")) or {}
    topics, messages = [], []
    for e in data.get("Events") or []:
        d = _json(e.get("Data")) or {}
        val = d.get("value") if isinstance(d.get("value"), dict) else {}
        for a in val.get("actions") or []:
            tid = a.get("topicId") or ""
            if tid and tid not in topics:
                topics.append(tid)
        for s in val.get("steps") or []:
            if ".topic." in str(s) and s not in topics:
                topics.append(s)
        if d.get("type") == "message" and d.get("text"):
            messages.append(d["text"][:300])
    return [t.split(".topic.")[-1] for t in topics], messages


def _rai(metrics: list[dict]) -> dict:
    ran, triggered = [], []
    for m in metrics:
        n = m.get("serviceName", "")
        if re.search(r"classifier|rai|aacs|safety", n, re.I):
            if n not in ran:
                ran.append(n)
            out = m.get("output") or ""
            if isinstance(out, str) and re.search(r'"isTriggered"\s*:\s*true|"JailbreakDetected"\s*:\s*true', out):
                triggered.append(n)
    return {"ran": ran, "triggered": sorted(set(triggered))}


def parse(data: dict) -> dict:
    msgs = data.get("messages") or []
    turns = []
    for req in data.get("requests") or []:
        rid = req.get("requestId", "")
        user = next((m for m in msgs if m.get("author") == "user" and m.get("requestId") == rid), None)
        question = (user or {}).get("text", "")
        if question.strip().startswith("/"):
            continue
        bot = next((m for m in msgs if m.get("author") == "bot" and m.get("requestId") == rid
                    and not m.get("messageType") and m.get("contentOrigin") not in ("annotation-provider",)), None)
        metrics = (req.get("telemetry") or {}).get("metrics") or []
        by = lambda name: [m for m in metrics if m.get("serviceName") == name]
        llm = [c for c in (_llm_call(m) for m in by("DeepLeoImprovedNetworking")) if c]
        loop = [c for c in llm if c["purpose"].startswith("Reasoning")]
        topics, mcs_msgs = ([], [])
        for m in by("McsOrchestratedClient.InvokeAsync"):
            topics, mcs_msgs = _mcs_topics(m)
        grounding = _json((by("CrossPluginGroundingDataReducer") or [{}])[0].get("output")) or {}
        cites = next((j for j in (_json(m.get("output")) for m in by("EventHandler")) if isinstance(j, dict) and j.get("serializationMode")), {})
        audit = _json((by("CopilotInteractionAuditRecordBuilder") or [{}])[0].get("output")) or {}
        ced = audit.get("CopilotEventData") or {}
        reports = [j for j in (_json(m.get("output")) for m in by("CacheabilityReport")) if isinstance(j, dict) and j.get("ResolvedModelName")]
        resolved = max(reports, key=lambda j: (j.get("CacheabilityMetrics") or {}).get("OverallCharSize", 0))["ResolvedModelName"] if reports else ""
        errors = [{"service": m.get("serviceName"), "status": m.get("status"), "detail": str(m.get("exception") or m.get("message") or "")[:200]}
                  for m in metrics if m.get("status") not in ("Success", "Canceled", "", None)]
        sources = []
        for s in (bot or {}).get("sourceAttributions") or []:
            src = ((_json(s.get("metadata")) or {}).get("Source") or {}) if isinstance(s, dict) else {}
            name = src.get("Title") or src.get("filename") or s.get("providerDisplayName") or s.get("seeMoreUrl") or ""
            if name and name not in sources:
                sources.append(str(name))
        refs = (bot or {}).get("references")
        if isinstance(refs, dict):
            for v in refs.values():
                title = _clean_title(str(v.get("title") or "")) if isinstance(v, dict) else ""
                if title and title not in sources:
                    sources.append(title)
        turns.append({
            "request_id": rid, "question": question, "response": _clean_title((bot or {}).get("text", "")),
            "searches": _search_runs(metrics),
            "offense": (bot or {}).get("offense", ""), "sources": sources,
            "start": req.get("startTime", ""), "service_version": req.get("serviceVersion", ""),
            "agent": next((g for g in (user or {}).get("gptIdentifiers") or []), {}),
            "agent_name": ced.get("TargetAgentName") or audit.get("AgentName") or "",
            "tenant": audit.get("OrganizationId", ""), "app_host": ced.get("AppHost", ""),
            "audit_models": [f"{x.get('ModelProviderName')}/{x.get('ModelName')}" for x in ced.get("ModelTransparencyDetails") or []],
            "llm_calls": llm, "loop": loop, "resolved_model": resolved,
            "tools_offered": sorted({t for c in loop for t in c["tools_offered"]}),
            "tool_calls": [t for c in loop for t in c["tool_calls"]],
            "instructions": next((c["instructions"] for c in loop if c["instructions"]), ""),
            "grounding": {"exit": (grounding.get("execution") or {}).get("exitPath", ""),
                          "queries": grounding.get("queries") or [], "stages": len(grounding.get("stages") or [])},
            "citations": {"search_results": cites.get("searchResultsCount"), "references": cites.get("referencesDataCount"),
                          "grounding_urls": cites.get("groundingUrlsCount")},
            "topics": topics, "mcs_messages": mcs_msgs, "rai": _rai(metrics), "errors": errors[:8],
            "latency": {k: max((m.get("latencyMilliseconds", 0) for m in by(k)), default=None)
                        for k in ("PreReasoningLatency", "AgentRuntimeTurnExecutor", "SydToolsTotalInitialReasoning",
                                  "TopicDiscoverySearcher", "McsOrchestratedClient.InvokeAsync")},
        })
    return {"conversation_id": data.get("conversationId", ""), "turns": turns}


def _section(title: str) -> list[str]:
    return ["", RULE, title.upper(), RULE]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def build(parsed: dict, turn_index: int = 0, *, current=None, findings: list | None = None) -> str:
    t = parsed["turns"][turn_index]
    q, resp = t["question"], t["response"]
    served = from_instructions(t["instructions"], "served") if t["instructions"] else None
    check = instruction_check(q, resp, served or current)
    refused = bool(resp and _REFUSAL.search(resp[:600]))
    search_calls = [c for c in t["tool_calls"] if _SEARCH_TOOL.search(c["name"])]
    other_calls = [c for c in t["tool_calls"] if c not in search_calls]
    runs = t.get("searches") or []
    n_results = sum(r.get("kept") or 0 for r in runs) if runs else (t["citations"].get("search_results") or 0)
    sources = t["sources"] or (cited_sources(resp) if resp else [])

    # Stage verdicts for the 1→4 pipeline.
    stage, why = {}, ""
    if check and check["followed"] is False:
        stage["1"] = "FAILED"; why = "The instructions say to decline this question, but the agent answered it."
    elif not t["tool_calls"] and not refused and resp:
        stage["1"] = "FAILED"; why = "The agent answered without calling any tool or search, so the answer isn't grounded in your sources."
    elif not t["tool_calls"] and refused and not check:
        stage["1"] = "FAILED"; why = "The agent declined without searching, and no instruction asks it to decline this question (overly cautious)."
    else:
        stage["1"] = "ok"
    if search_calls:
        stage["2"] = "FAILED" if n_results == 0 and not sources else "ok"
        if stage["2"] == "FAILED" and stage["1"] == "ok":
            why = "Knowledge search ran but returned nothing usable, so the right document wasn't found."
    else:
        stage["2"] = "not run"
    if t["rai"]["triggered"]:
        stage["3"] = "FAILED"; why = why or f"A safety classifier triggered ({', '.join(t['rai']['triggered'])})."
    else:
        stage["3"] = "ok" if resp else "no answer"
    stage["4"] = "ok" if resp else "no answer"

    L: list[str] = []
    L.append(f"Conversation Investigation (DevUI trace) — {parsed['conversation_id']}")
    L.append("Source: Copilot DevUI conversation export (M365 Copilot orchestrator telemetry)"
             + (", current agent instructions" if current is not None else ""))
    L.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    L += _section("User question"); L.append(f'"{q}"')
    L += _section("Agent response"); L.append(resp or "(no response)")

    L += _section("What happened")
    if check and check["followed"]:
        L.append("The agent declined, as its instructions require for this question.")
    elif why:
        L.append(why)
    else:
        L.append("The turn completed without a detected failure.")
    calls = ", ".join(c["name"] for c in t["tool_calls"]) or "none"
    L.append(f"Planner: {len(t['tools_offered'])} tool(s) offered; called: {calls}.")
    if runs:
        for r in runs:
            L.append(f"Search \"{' | '.join(r['queries'])}\": {r['found']} found → {r['admitted'] if r['admitted'] is not None else '?'} "
                     f"admitted → {r['kept']} kept for the answer.")
    elif search_calls:
        L.append(f"Search: {len(search_calls)} search call(s); {n_results} result(s) reached the answer.")
    L.append(f"Answer: {len(sources)} source(s) cited" + (f" ({', '.join(sources[:4])})." if sources else "."))

    L += _section("Where it went wrong")
    labels = {"1": PIPELINE_ORDER[0], "2": PIPELINE_ORDER[1], "3": PIPELINE_ORDER[2], "4": PIPELINE_ORDER[3]}
    first_fail = next((k for k in "1234" if stage[k] == "FAILED"), None)
    for k in "1234":
        mark = "◀ FAILED HERE" if k == first_fail else ("affected (downstream)" if first_fail and k > first_fail and stage[k] != "not run" else stage[k])
        L.append(f"  {labels[k]:<30} {mark}")

    if check:
        L += _section("Instruction check")
        for r in check["rules"]:
            L.append(f'Rule (line {r["line"]}): "{r["text"][:260]}"')
        L.append(f"Expected: {check['expected']}. Observed: {check['observed']}.")
        L.append("Result: " + ("INSTRUCTION FOLLOWED" if check["followed"] else "INSTRUCTION NOT FOLLOWED"))

    L += _section("Instructions that ran")
    if served:
        L.append(f"{served.char_count:,} characters were sent to the model for this turn (agent version: "
                 f"{t['agent'].get('version', '?')}).")
        if current is not None:
            same = _norm(served.instruction_text) == _norm(current.instruction_text)
            L.append("They match the instructions loaded in the Doctor." if same else
                     f"They DIFFER from the instructions loaded in the Doctor ({current.char_count:,} characters). "
                     "This conversation ran on an earlier or later version; check the version before acting on findings.")
    else:
        L.append("The trace doesn't include the agent instructions for this turn.")

    L += _section("Key details")
    rows = [
        ("Agent", t["agent_name"] or t["agent"].get("compliantAgentName", "")),
        ("Agent version", t["agent"].get("version", "")),
        ("Tenant ID", t["tenant"]), ("Surface", t["app_host"]),
        ("Request ID", t["request_id"]), ("Service version", t["service_version"]), ("Start time", t["start"]),
        ("Answer model", t["resolved_model"] or (t["loop"][-1]["model"] if t["loop"] else "")),
        ("Models (audit)", ", ".join(t["audit_models"])),
        ("Offense", t["offense"] if t["offense"] not in ("Unknown", "None") else ""),
    ]
    L += [f"{k + ':':<20}{v}" for k, v in rows if v]

    L += _section(f"How it unfolded — {len(t['llm_calls'])} LLM call(s)")
    for i, c in enumerate(t["llm_calls"], 1):
        L.append(f"{i}. {c['purpose']} — {c['model'] or '?'} — {c['latency_ms']} ms — "
                 f"{c['prompt_tokens'] or '?'} prompt / {c['completion_tokens'] or '?'} completion tokens")
        if c["tool_calls"]:
            for tc in c["tool_calls"]:
                detail = (f"  queries: {' | '.join(tc['queries'])}" if tc.get("queries") else "") + \
                         (f"  domain: {', '.join(tc['domains'])}" if tc.get("domains") else "") + \
                         (f"  opened: {', '.join(tc['ref_ids'])}" if tc.get("ref_ids") else "")
                L.append(f"     tool call: {tc['name']}{detail}")
        elif c["purpose"].startswith("Reasoning"):
            L.append("     no tool calls — answered directly")
        if c["thought"]:
            L.append(f"     reasoning summary: {c['thought'][:300]}")
        if c["response"] and c["purpose"] != "Reasoning loop (planner + answer)":
            L.append(f"     output: {c['response'][:120]}")

    L += _section("1 Planner / tool selection")
    L.append("Tools offered: " + (", ".join(t["tools_offered"]) or "none recorded"))
    L.append("Tools called: " + calls)
    if t["topics"]:
        L.append("Copilot Studio topics that ran: " + ", ".join(t["topics"]))
    L += _section("2 Search / tool execution")
    titles = {k: v for r in runs for k, v in r["titles"].items()}
    if search_calls:
        for c in search_calls:
            L.append(f"  {c['name']}: " + (" | ".join(c.get("queries") or []) or c["args"][:200])
                     + (f"  (domain: {', '.join(c['domains'])})" if c.get("domains") else ""))
    else:
        L.append("No knowledge search ran this turn.")
    for r in runs:
        L.append("")
        L.append(f"Query \"{' | '.join(r['queries'])}\" — {r['found']} found → "
                 f"{r['admitted'] if r['admitted'] is not None else '?'} admitted → {r['kept']} kept")
        for note in r["notes"]:
            if note:
                L.append(f"  · {note[:220]}")
        if r["kept_docs"]:
            L.append("  Kept for the answer:")
            for d in r["kept_docs"][:12]:
                L.append(f"    - {d['title'] or d['ref']}  ({d['ref']})")
    opened = [ref for c in t["tool_calls"] for ref in c.get("ref_ids") or []]
    if opened:
        L.append("Opened for full content: " + ", ".join(f"{titles.get(x, x)} ({x})" for x in opened))
    if sources:
        L.append("Cited in the answer: " + ", ".join(sources[:8]))
    g = t["grounding"]
    L.append(f"Grounding: {g['exit'] or 'n/a'}; search results serialized: {t['citations'].get('search_results')}; "
             f"references: {t['citations'].get('references')}")
    L += _section("3 Synthesis & safety")
    L.append(f"Safety classifiers run: {len(t['rai']['ran'])}; triggered: {', '.join(t['rai']['triggered']) or 'none'}")
    if t["errors"]:
        L.append("Non-success steps: " + "; ".join(f"{e['service']} ({e['status']})" for e in t["errors"][:5]))
    lat = {k: v for k, v in t["latency"].items() if v is not None}
    if lat:
        L.append("Latency (ms): " + ", ".join(f"{k}={v}" for k, v in lat.items()))

    findings = findings or []
    maker = [f for f in findings if getattr(f, "origin", "maker") == "maker"]
    linked, risky = [], []
    if first_fail or (check and not check["followed"]):
        want = {"1": {"routing", "tool"}, "2": {"search"}, "3": {"synthesis", "rai"}, "4": {"render"}}.get(first_fail or "1", set())
        linked = [f for f in maker if f.stage in want] or maker
        if linked:
            L += _section("Instruction lines likely responsible")
            for f in linked[:5]:
                d = f.to_dict()
                L.append(f"  - [{f.severity.upper()}] {d['pipeline']} — {f.title} (line {', '.join(map(str, f.lines[:3]))}). Users see: {d['user_sees']}")
                L.append(f"      \"{f.current[:180]}\"")
                L.append(f"      Fix: {f.fix[:300]}")
    else:
        risky = [f for f in maker if f.severity == "high"][:3]
        if risky:
            L += _section("Risks in the instructions that ran")
            L.append("This turn passed, but these lines can break other questions:")
            for f in risky:
                d = f.to_dict()
                L.append(f"  - [{f.severity.upper()}] {d['pipeline']} — {f.title} (line {', '.join(map(str, f.lines[:3]))}). Users see: {d['user_sees']}")
                if f.test_prompts:
                    L.append(f"      Test with: {' | '.join(f.test_prompts[:3])}")

    L += _section("Recommended next steps")
    n = 0
    if check and check["followed"] is False:
        n += 1; L.append(f"{n}. Put the restriction first in '#Routing decision tree' so 'policy → KB search' can't win, then re-test.")
    if stage["1"] == "FAILED" and not check:
        n += 1; L.append(f"{n}. Check the routing and grounding rules: this question should use a tool or KB search before answering.")
    if stage["2"] == "FAILED":
        n += 1; L.append(f"{n}. Check the search query above against your document titles; remove query enrichment that doesn't match how documents are written.")
    if t["rai"]["triggered"]:
        n += 1; L.append(f"{n}. Review content moderation and knowledge scope for the triggered classifier.")
    failed = bool(first_fail or (check and not check["followed"]))
    to_fix = (linked if failed else risky)[:3]
    for f in to_fix:
        n += 1
        L.append(f"{n}. Line {', '.join(map(str, f.lines[:3]))} — {f.title}.")
        L.append(f"   Change: {f.fix[:300]}")
    if to_fix:
        tests = [p for f in to_fix for p in (f.test_prompts or [])][:4]
        n += 1
        msg = (f"{n}. Apply these in the Agent check-up (or copy the fixed instructions into Copilot Studio), save, start a new "
               "chat, then " + ("re-run this question" if failed else "ask")
               + (f": {' | '.join(tests)}" if tests and not failed else "")
               + (f", plus {' | '.join(tests)}" if tests and failed else ""))
        L.append(msg if msg.endswith("?") else msg + ".")
    elif not failed:
        n += 1; L.append(f"{n}. No action needed for this question.")
    else:
        n += 1; L.append(f"{n}. Re-run this question in a new chat after changing the instructions, and compare the new trace.")

    L += _section("What this trace can't see")
    L.append("Memory and personalization content are redacted in the export. Full prompt text is included only for "
             "some model calls. Search result documents appear only when a search tool ran.")
    return "\n".join(L)
