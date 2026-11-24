"""Conversation investigation report from a developer mode (-developer on) debug card.

Same layout as the DevUI and Copilot Studio runtime investigation reports, so makers get one familiar format
whatever evidence they have. The card shows what Copilot planned and ran; the agent's current instructions
(optional) let the report point at the instruction lines behind the failing stage.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from ess_maker.doctor import devmode
from ess_maker.doctor.report import RULE, instruction_check
from ess_maker.doctor.rules import PIPELINE_ORDER, validate

_STAGE_KEYS = ["planner", "search", "synthesis", "render"]
_WANT = {"planner": {"routing", "tool"}, "search": {"search", "routing"}, "synthesis": {"synthesis", "rai"}, "render": {"render"}}
_LABEL = {"agentId": "Agent ID", "agentScope": "Agent scope", "agentVersion": "Agent version", "conversationId": "Conversation ID",
          "requestId": "Request ID", "capabilities": "Capabilities", "matchedFunctions": "Matched functions",
          "selectedFunctions": "Selected functions", "executedCapabilities": "Executed capabilities",
          "executedActions": "Executed actions"}


def _section(title: str) -> list[str]:
    return ["", RULE, title.upper(), RULE]


def _entry(e: dict) -> str:
    bits = [e.get("status") or ("ok" if e.get("ok") else "failed" if e.get("ok") is False else "")]
    if e.get("http"):
        bits.append(f"HTTP {e['http']}")
    if e.get("latency_ms") is not None:
        bits.append(f"{float(e['latency_ms']):.0f} ms")
    if e.get("results") is not None:
        bits.append(f"{e['results']} result(s)")
    return f"{e.get('name') or '?'} — " + ", ".join(b for b in bits if b)


def _relevant(f, question: str) -> bool:
    """Restriction findings only explain a turn when the question touches their subject."""
    if not re.search(r"BLOCK|RESTRICT|REFUSAL|OVERRIDE|JURISDICTION", f.rule_id):
        return True
    words = {w for w in re.findall(r"[a-z]{5,}", (question or "").lower())}
    text = (f.current or "").lower()
    return bool(words) and any(w in text or w.rstrip("s") in text for w in words)


def build(text: str, *, question: str = "", response: str = "", current=None) -> str:
    diag = devmode.diagnose(text, question, response)
    card, st = diag.get("card") or {}, diag["stages"]
    first_fail = next((k for k in _STAGE_KEYS if st.get(k) == "failed"), None)
    check = instruction_check(question, response, current) if current is not None else None

    L = [f"Conversation Investigation (developer mode card) — {card.get('conversation_id') or 'conversation ID not shown'}",
         "Source: Microsoft 365 Copilot developer mode debug card" + (", agent instructions" if current is not None else ""),
         f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"]
    L += _section("User question")
    L.append(f'"{question}"' if question else "Not shown on the card. Add the question to include it here.")
    L += _section("Agent response")
    L.append(response or "Not shown on the card. Add the agent's answer to check the answer stage.")

    L += _section("What happened")
    L.append(diag["likely_cause"])
    L += [f"- {h}" for h in diag["happened"]]

    L += _section("Where it went wrong")
    for i, k in enumerate(_STAGE_KEYS):
        v = st.get(k, "unknown")
        mark = "◀ FAILED HERE" if k == first_fail else (
            "affected (downstream)" if first_fail and i > _STAGE_KEYS.index(first_fail) and v not in ("not run", "unknown") else v)
        L.append(f"  {PIPELINE_ORDER[i]:<30} {mark}")

    if check:
        L += _section("Instruction check")
        for r in check["rules"]:
            L.append(f'Rule (line {r["line"]}): "{r["text"][:260]}"')
        L.append(f"Expected: {check['expected']}. Observed: {check['observed']}.")
        L.append("Result: " + ("INSTRUCTION FOLLOWED" if check["followed"] else "INSTRUCTION NOT FOLLOWED"
                               if check["followed"] is False else "UNKNOWN (add the agent's answer)"))

    L += _section("Key details")
    L += [f"{_LABEL.get(k, k) + ':':<24}{v}" for k, v in diag["key_details"].items() if v]

    L += _section("1 Planner / tool selection")
    L.append("Capabilities: " + (", ".join(card.get("capabilities") or []) or "none listed"))
    L.append("Actions: " + ("none enabled" if card.get("no_actions") else ", ".join(card.get("action_ids") or []) or "none listed"))
    L.append("Matched functions: " + (", ".join(card.get("matched") or []) or ("none" if card.get("no_matched") else "not shown")))
    L.append("Selected for execution: " + (", ".join(card.get("selected") or []) or ("none" if card.get("no_selected") else "not shown")))

    L += _section("2 Search / tool execution")
    caps, acts = card.get("exec_caps") or [], card.get("exec_actions") or []
    if not caps and not acts:
        L.append("Nothing was executed for this question." if not card.get("no_exec_details") else "No function execution details were returned.")
    for e in caps:
        L.append("  Knowledge: " + _entry(e) + (f"  search text: {e['search_text']}" if e.get("search_text") else ""))
    for e in acts:
        L.append("  Action: " + _entry(e) + (f"  {e.get('method') or ''} {e['url']}".rstrip() if e.get("url") else ""))
        if e.get("error"):
            L.append(f"      error: {str(e['error'])[:240]}")

    L += _section("3 Synthesis & safety")
    L.append("The card doesn't show safety classifiers or how the answer was written. "
             + ("The answer you added was checked for abstain wording." if response else "Add the agent's answer to check this stage."))

    findings = [f for f in validate(current) if f.origin == "maker"] if current is not None else []
    backend_failed = any(e.get("ok") is False for e in acts) or any(e.get("ok") is False for e in caps)
    linked = []
    if backend_failed:
        L += _section("Instruction lines likely responsible")
        L.append("None: the failure is in the connection or backend the action called, not in the agent's instructions.")
    elif findings and (first_fail or (check and check["followed"] is False)):
        want = _WANT.get(first_fail or "planner", set())
        linked = [f for f in findings if f.stage in want and _relevant(f, question)][:5]
        if linked:
            L += _section("Instruction lines likely responsible")
            for f in linked:
                d = f.to_dict()
                L.append(f"  - [{f.severity.upper()}] {d['pipeline']} — {f.title} (line {', '.join(map(str, f.lines[:3]))}). Users see: {d['user_sees']}")
                L.append(f"      \"{f.current[:180]}\"")
                L.append(f"      Fix: {f.fix[:300]}")
        else:
            L += _section("Instruction lines likely responsible")
            L.append("None found: no instruction line targets this question. Check the descriptions of the topics, "
                     "actions and knowledge sources that should handle it.")
    elif current is None and first_fail in ("planner", "synthesis"):
        L += _section("Instruction lines likely responsible")
        L.append("Upload the agent's instructions with the card to see which lines steered this question away from tools or into a refusal.")

    L += _section("Recommended next steps")
    steps = list(diag["next_steps"])
    for f in linked[:3]:
        steps.append(f"Line {', '.join(map(str, f.lines[:3]))} — {f.title}. Change: {f.fix[:300]}")
    steps.append("For the instructions that ran, every model call and the search funnel, export the same conversation from DevUI.")
    L += [f"{i}. {s}" for i, s in enumerate(steps, 1)]

    L += _section("What this card can't see")
    L += [f"- {c}" for c in diag["cannot_see"]]
    return "\n".join(L)
