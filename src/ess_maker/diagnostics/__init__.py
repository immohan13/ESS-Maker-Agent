"""Application Insights diagnostics for Copilot Studio runtime errors.

When the agent returns the generic "[System] - On Error" message, the *real*
exception is in App Insights (if the maker has connected one to the bot).

We query the App Insights REST API:
  POST https://api.applicationinsights.io/v1/apps/{app_id}/query
  Header: x-api-key: <key>
  Body:   { "query": "<KQL>" }

We use the conversation_id (captured from CopilotStudioClient) as the join key.
Copilot Studio writes it to customDimensions.conversationId on every trace/exception.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ess_maker.settings import settings

API_BASE = "https://api.applicationinsights.io/v1/apps"

# KQL: Copilot Studio writes to customEvents (not exceptions/traces).
# Key event names: OnErrorLog, TopicStart, TopicEnd, TopicAction, BotMessageReceived, BotMessageSend.
# customDimensions.ConversationId is the join key (also lowercase 'conversationId' is set).
#
# We aggressively filter out the [System] bookkeeping topics (UserContext-Validate,
# Init Variables, Logger, ConversationStart) — they fire dozens of TopicAction rows
# per turn and otherwise crowd out the actual user-facing topic + any OnErrorLog row.
KQL_TEMPLATE = """
let conv = "{conv_id}";
let win = ago({lookback_min}m);
customEvents
| where timestamp > win
| where tostring(customDimensions.ConversationId) == conv
   or tostring(customDimensions.conversationId) == conv
| extend topic = tostring(customDimensions.TopicName),
         actionName = tostring(customDimensions.ActionName),
         errorCode = tostring(customDimensions.ErrorCode),
         errorMessage = tostring(customDimensions.ErrorMessage)
// Always keep errors and message bookends, even if they came from a System topic.
| where name in ('OnErrorLog','BotMessageReceived','BotMessageSend')
     or isnotempty(errorCode) or isnotempty(errorMessage)
     or topic !startswith 'msdyn_' or not(topic contains 'System-' or topic contains 'ConversationStart')
| project timestamp, name,
          errorCode, errorMessage,
          topic, actionName,
          topicId = tostring(customDimensions.TopicId),
          actionType = tostring(customDimensions.ActionType),
          flowId = tostring(customDimensions.FlowId),
          channel = tostring(customDimensions.channelId),
          dims = customDimensions
| order by timestamp asc
| take 500
"""


def is_configured() -> bool:
    return bool(settings.appinsights_app_id and settings.appinsights_api_key)


async def fetch_errors_for_conversation(
    conv_id: str,
    *,
    lookback_min: int = 60,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    """Return {ok, configured, rows[], summary, query} for a conversation_id."""
    if not conv_id:
        return {"ok": False, "configured": is_configured(), "error": "no conversation_id"}
    if not is_configured():
        return {
            "ok": False,
            "configured": False,
            "error": "App Insights not configured (set APPINSIGHTS_APP_ID + APPINSIGHTS_API_KEY).",
        }

    query = KQL_TEMPLATE.format(conv_id=conv_id, lookback_min=lookback_min)
    url = f"{API_BASE}/{settings.appinsights_app_id}/query"
    headers = {
        "x-api-key": settings.appinsights_api_key,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as cx:
            r = await cx.post(url, headers=headers, json={"query": query})
        if r.status_code != 200:
            return {
                "ok": False, "configured": True,
                "error": f"App Insights API {r.status_code}: {r.text[:300]}",
                "query": query,
            }
        data = r.json()
        rows = _parse_rows(data)
        return {
            "ok": True, "configured": True,
            "rows": rows,
            "summary": _summarize(rows),
            "query": query,
        }
    except httpx.HTTPError as e:
        return {"ok": False, "configured": True, "error": f"network: {e}", "query": query}


def _parse_rows(payload: dict) -> list[dict[str, Any]]:
    tables = payload.get("tables") or []
    if not tables:
        return []
    t = tables[0]
    cols = [c["name"] for c in t.get("columns", [])]
    return [dict(zip(cols, row)) for row in t.get("rows", [])]


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a one-paragraph 'what really happened' for the maker."""
    if not rows:
        return {
            "headline": "No events found in App Insights for this conversation.",
            "details": "Either the conversation_id didn't match, or App Insights isn't connected to this agent. "
                       "Verify in Copilot Studio → Settings → Advanced → Application Insights.",
            "topic": None, "action": None,
        }

    errors = [r for r in rows if r.get("name") == "OnErrorLog" and (r.get("errorMessage") or r.get("errorCode"))]
    if errors:
        # Most-recent error in this conversation.
        e = errors[-1]
        msg = e.get("errorMessage") or ""
        code = e.get("errorCode") or ""
        # Find the topic/action we were in when the error fired (last TopicAction/TopicStart before this timestamp).
        ctx = _last_topic_context(rows, before_ts=e.get("timestamp"))
        topic = ctx.get("topic") or e.get("topic") or ""
        action = ctx.get("actionName") or e.get("actionName") or ""
        flow = e.get("flowId") or ctx.get("flowId") or ""
        parts = [code or "Error"]
        if topic: parts.append(f"in topic '{topic}'")
        if action: parts.append(f"at action '{action}'")
        headline = " ".join(parts) + (f": {msg[:200]}" if msg else "")
        return {
            "headline": headline,
            "details": msg,
            "topic": topic or None,
            "action": action or None,
            "flow": flow or None,
            "error_code": code or None,
            "fix_hint": _fix_hint(code, msg),
        }

    # No error event — just summarize the topics that ran.
    topics = [r.get("topic") for r in rows if r.get("name") in ("TopicStart", "TopicEnd") and r.get("topic")]
    if topics:
        return {
            "headline": f"Conversation ran through topics: {', '.join(dict.fromkeys(topics))}",
            "details": "No error events were logged. The agent may have responded normally, or the failure was upstream of Copilot Studio.",
            "topic": topics[-1] if topics else None,
            "action": None,
        }
    return {
        "headline": f"{len(rows)} events logged but no errors and no topic info.",
        "details": "Conversation events are present but contain no recognizable error or topic markers.",
        "topic": None, "action": None,
    }


def _last_topic_context(rows: list[dict[str, Any]], before_ts: Any) -> dict[str, Any]:
    """Walk backwards from the error to find the most recent TopicStart/TopicAction."""
    ctx: dict[str, Any] = {}
    for r in rows:
        if before_ts and str(r.get("timestamp")) > str(before_ts):
            break
        if r.get("name") in ("TopicStart", "TopicAction") and (r.get("topic") or r.get("actionName")):
            if r.get("topic"): ctx["topic"] = r.get("topic")
            if r.get("actionName"): ctx["actionName"] = r.get("actionName")
            if r.get("flowId"): ctx["flowId"] = r.get("flowId")
    return ctx


def _fix_hint(error_code: str, message: str) -> str:
    code = (error_code or "").lower()
    m = (message or "").lower()
    if code == "flowdisabled" or "is disabled" in m:
        return ("The Power Automate flow this topic calls is **turned off**. "
                "Open the flow in Power Automate (link via the action's 'Edit flow' button in Copilot Studio) "
                "and click **Turn on**. Then re-run the eval.")
    if code == "flownotfound" or "flow" in m and ("not found" in m or "deleted" in m):
        return ("The Power Automate flow has been deleted or moved out of the solution. "
                "Recreate the flow or update the action to point at the correct flow.")
    if "unauthorized" in m or "401" in m or "forbidden" in m or "403" in m:
        return ("The connector or downstream API rejected the call. Re-authorize the connection in "
                "Copilot Studio → Tools → Connections, or have the test user complete the auth flow.")
    if "timeout" in m or "timed out" in m:
        return ("A downstream call timed out. Increase the flow's timeout, or move the slow call to an async pattern.")
    if "null" in m and ("reference" in m or "value" in m or "expected" in m):
        return ("A variable was used before being set. Add a Question or Condition node to validate it, "
                "or set a default at topic start.")
    if "expression" in m or "powerfx" in code or "parse" in m:
        return ("A Power Fx expression failed. Check the failing node's expression — usually a missing field or wrong type.")
    return ("Open the topic in Copilot Studio and use the **Topic checker** to inspect the failing node, "
            "or check the connector's recent run history.")
