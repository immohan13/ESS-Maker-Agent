"""Microsoft-side runtime traces (Copilot Studio engine telemetry in Kusto).

Turns a conversation ID into a stage-by-stage diagnosis: agent version that served it, planner
model, tools executed, knowledge search results vs. filtered, LLM calls with stop codes and
content-filter signals, exceptions, and a verdict about where the failure happened.

Access is gated by an allowlist of tenant IDs (DOCTOR_ALLOWED_TENANTS) so only test-tenant
conversations can be inspected from the UI.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ess_maker.settings import settings

def regions() -> dict[str, str]:
    """Region → cluster URL, from DOCTOR_KUSTO_CLUSTERS ("US=https://…,EU-WEU=https://…")."""
    out = {}
    for part in (settings.doctor_kusto_clusters or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out

_GUID = re.compile(r"^[0-9a-fA-F-]{36}(_[0-9a-fA-F-]{36})?$")
_token_cache: dict[str, tuple[str, datetime]] = {}


class KustoError(RuntimeError):
    pass


def allowed_tenants() -> set[str]:
    return {t.strip().lower() for t in (settings.doctor_allowed_tenants or "").split(",") if t.strip()}


def _token(cluster: str) -> str:
    cached = _token_cache.get(cluster)
    if cached and cached[1] > datetime.now(timezone.utc):
        return cached[0]
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        raise KustoError("Azure CLI (az) not found. Install it and run `az login`.")
    r = subprocess.run(
        [az, "account", "get-access-token", "--resource", cluster, "--tenant", settings.doctor_kusto_aad_tenant,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, timeout=90,
    )
    tok = r.stdout.strip()
    if not tok:
        raise KustoError(f"Could not get a Kusto token: {r.stderr.strip()[:300]}")
    _token_cache[cluster] = (tok, datetime.now(timezone.utc) + timedelta(minutes=40))
    return tok


async def query(region: str, csl: str, timeout_s: int = 240) -> list[dict[str, Any]]:
    cluster = regions().get(region)
    if not cluster:
        raise KustoError(f"No Kusto cluster configured for region {region}. Set DOCTOR_KUSTO_CLUSTERS in .env.")
    if not settings.doctor_kusto_db or not settings.doctor_kusto_aad_tenant:
        raise KustoError("Set DOCTOR_KUSTO_DB and DOCTOR_KUSTO_AAD_TENANT in .env.")
    tok = await asyncio.to_thread(_token, cluster)
    body = {"db": settings.doctor_kusto_db, "csl": csl, "properties": {"Options": {"servertimeout": "00:04:00"}}}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(f"{cluster}/v1/rest/query", json=body,
                              headers={"Authorization": f"Bearer {tok}"})
    if r.status_code != 200:
        raise KustoError(f"Kusto {r.status_code}: {r.text[:400]}")
    doc = r.json()
    for t in doc.get("Tables", []):
        if t.get("TableName") in ("@ExtendedProperties", "QueryStatus", "QueryCompletionInformation"):
            continue
        cols = [c["ColumnName"] for c in t.get("Columns", [])]
        return [dict(zip(cols, row)) for row in t.get("Rows", [])]
    return []


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _cd(cd: str, key: str) -> str:
    m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', cd or "")
    return m.group(1) if m else ""


async def recent_conversations(region: str, bot_id: str = "", hours: int = 24, limit: int = 25) -> dict:
    tenants = allowed_tenants()
    if not tenants:
        return {"ok": False, "error": "Set DOCTOR_ALLOWED_TENANTS to your test tenant ID(s) first."}
    tlist = ", ".join(f'"{_q(t)}"' for t in tenants)
    bot_filter = f'| where customDimensions has "{_q(bot_id)}"' if bot_id else ""
    csl = f"""
OperationEvents
| where env_time > ago({int(hours)}h)
| where activityName == "MessageProcessor.ExecuteAsync"
| where principalTenantId in ({tlist})
{bot_filter}
| extend schema = tostring(extract_json("$.BotSchemaName", customDimensions)),
         channel = tostring(extract_json("$.ChannelId", customDimensions)),
         cdsBotId = tostring(extract_json("$.CdsBotId", customDimensions))
| where schema startswith "{_q(settings.doctor_schema_prefix)}" or schema startswith "gptagent_" or "{_q(bot_id)}" != ""
| summarize firstSeen=min(env_time), lastSeen=max(env_time), turns=count(),
            failed=countif(resultType != "Success"), p95_ms=percentile(durationMs, 95)
          by conversationId=clientSessionId, schema, channel, cdsBotId
| order by lastSeen desc
| take {int(limit)}
"""
    rows = await query(region, csl)
    return {"ok": True, "region": region, "conversations": rows}


async def sessions_near(region: str, start_iso: str, minutes: int = 60, bot_id: str = "") -> dict:
    """Copilot Studio runtime sessions for allow-listed tenants within a time window (correlate DA turns)."""
    tenants = allowed_tenants()
    if not tenants:
        return {"ok": False, "error": "Set DOCTOR_ALLOWED_TENANTS to your test tenant ID(s) first."}
    if not re.match(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z?$", start_iso):
        return {"ok": False, "error": "Invalid start time."}
    tlist = ", ".join(f'"{_q(t)}"' for t in tenants)
    bot_filter = f'| where cdsBotId == "{_q(bot_id)}"' if bot_id else ""
    rows = await query(region, f"""
OperationEvents
| where env_time between (datetime({start_iso}) - 2m .. datetime({start_iso}) + {int(minutes)}m)
| where principalTenantId in ({tlist})
| where activityName == "MessageProcessor.ExecuteAsync"
| extend schema = tostring(extract_json("$.BotSchemaName", customDimensions)),
         channel = tostring(extract_json("$.ChannelId", customDimensions)),
         cdsBotId = tostring(extract_json("$.CdsBotId", customDimensions))
{bot_filter}
| summarize firstSeen=min(env_time), lastSeen=max(env_time), turns=count(), failed=countif(resultType != "Success")
          by conversationId=clientSessionId, schema, channel, cdsBotId
| order by firstSeen asc
| take 50""")
    return {"ok": True, "conversations": rows}


async def version_timeline(region: str, bot_id: str, days: int = 7) -> dict:
    """Saves, publishes, and which draft version each conversation ran on — all from runtime telemetry."""
    tenants = allowed_tenants()
    if not tenants:
        return {"ok": False, "error": "Set DOCTOR_ALLOWED_TENANTS to your test tenant ID(s) first."}
    if not _GUID.match(bot_id):
        return {"ok": False, "error": "Agent ID must be a GUID."}
    tlist = ", ".join(f'"{_q(t)}"' for t in tenants)
    events_q = f"""
OperationEvents
| where env_time > ago({int(days)}d)
| where principalTenantId in ({tlist})
| where activityName in ("PowerVA.API.MinimalBotContentPpapi.Update", "PowerVA.API.MinimalBotPpapi.Publish",
                         "PowerVA.API.BotContent.Update", "PowerVA.API.Bot.Publish")
   or (activityName has "Publish" and activityName startswith "PowerVA.API.")
| where customDimensions has "{_q(bot_id)}" or resourceId has "{_q(bot_id)}"
| project t = env_time, activity = activityName, who = principalObjectId, res = resultType
| order by t asc
| take 500"""
    conv_q = f"""
TraceEvents
| where env_time > ago({int(days)}d)
| where activityName == "PowerVA.RuntimeService.PipelineActivities.ExecuteSegment"
| where principalTenantId in ({tlist})
| where message has "BotDefinition Version" and message has "{_q(bot_id)}"
| extend version = toint(extract(@"BotDefinition Version: (\\d+)", 1, message)),
         vkind = iff(message has "draft", "draft", "published")
| where isnotnull(version)
| summarize firstSeen = min(env_time), lastSeen = max(env_time), conversations = dcount(clientSessionId),
            ids = make_set(clientSessionId, 20) by version, vkind
| order by version asc"""
    events, convs = await asyncio.gather(query(region, events_q), query(region, conv_q))
    saves = [{"t": e["t"], "user": e.get("who"), "result": e.get("res")} for e in events if "Update" in e["activity"]]
    publishes = [{"t": e["t"], "user": e.get("who"), "result": e.get("res")} for e in events if "Publish" in e["activity"]]
    versions = []
    for row in convs:
        first = str(row["firstSeen"])
        prior_saves = [s for s in saves if str(s["t"]) <= first]
        versions.append({
            "version": row["version"], "kind": row["vkind"], "first_seen": row["firstSeen"], "last_seen": row["lastSeen"],
            "conversations": row["conversations"], "conversation_ids": row["ids"],
            "saved_at": prior_saves[-1]["t"] if prior_saves else None,
            "saved_by": prior_saves[-1]["user"] if prior_saves else None,
        })
    return {"ok": True, "bot_id": bot_id, "saves": saves, "publishes": publishes, "versions": versions}


_TRACE_CACHE: dict[tuple, tuple[float, dict]] = {}


async def conversation_trace(conversation_id: str, region: str = "US", days: int = 7) -> dict:
    """Search the last day first (fast), then widen; cache results for 10 minutes."""
    import time as _time

    key = (conversation_id.strip(), region)
    hit = _TRACE_CACHE.get(key)
    if hit and _time.time() - hit[0] < 600:
        return hit[1]
    result = await _conversation_trace(conversation_id, region, 1)
    if not result.get("ok") and not result.get("blocked") and days > 1 and "No runtime telemetry" in result.get("error", ""):
        wide = await _conversation_trace(conversation_id, region, days)
        result = wide if wide.get("ok") or wide.get("blocked") else result
    if result.get("ok"):
        _TRACE_CACHE[key] = (_time.time(), result)
    return result


async def _conversation_trace(conversation_id: str, region: str = "US", days: int = 7) -> dict:
    cid = conversation_id.strip()
    if not _GUID.match(cid):
        return {"ok": False, "error": "Conversation ID must be a GUID (or hub_child GUID pair for connected agents)."}
    tenants = allowed_tenants()
    if not tenants:
        return {"ok": False, "error": "Set DOCTOR_ALLOWED_TENANTS to your test tenant ID(s) first."}

    window = f"ago({int(days)}d)"
    head = await query(region, f"""
OperationEvents
| where env_time > {window}
| where clientSessionId == "{cid}"
| where activityName == "MessageProcessor.ExecuteAsync"
| summarize t = min(env_time), tEnd = max(env_time), tenant = take_any(principalTenantId), cd = take_any(customDimensions)
| where isnotnull(t)""")
    if head and head[0].get("t") is None:
        head = []
    if not head:
        tlist = ", ".join(f'"{_q(t)}"' for t in tenants)
        fresh = await query(region, f"""
OperationEvents
| where env_time > ago(3h)
| where principalTenantId in ({tlist})
| where activityName == "MessageProcessor.ExecuteAsync"
| summarize latest = max(env_time)""")
        latest = (fresh[0].get("latest") if fresh else None) or ""
        lag = f" The newest runtime data for your tenant is from {str(latest)[11:16]} UTC; if your conversation is newer, " \
              "it hasn't been ingested yet — retry in a few minutes." if latest else ""
        return {"ok": False, "latest_ingested": latest,
                "error": f"No runtime telemetry yet for {cid} in {region} (searched the last {days} day"
                         f"{'s' if days != 1 else ''}).{lag}"}
    tenant = (head[0].get("tenant") or "").lower()
    if tenant not in tenants:
        return {"ok": False, "blocked": True,
                "error": "Conversation belongs to a tenant that isn't on the allowlist; details withheld."}
    cd = head[0].get("cd") or ""
    # Narrow every follow-up scan to this conversation's own time window.
    window = f"datetime({head[0]['t']}) - 10m"
    window_end = f"datetime({head[0]['tEnd']}) + 30m"

    ops_q = f"""
OperationEvents
| where env_time between ({window} .. {window_end})
| where clientSessionId == "{cid}"
| where activityName in ("MessageProcessor.ExecuteAsync", "PlannerOperationExecutor.ExecuteAsync",
    "UniversalSearchAsyncOperationExecutor.ExecuteAsync", "FlowOperationExecutor.ExecuteAsync",
    "ConnectorOperationExecutor.ExecuteAsync", "AnswerQuestionWithAIAsyncOperationExecutor.ExecuteAsync")
   or isnotempty(exceptionTypeName)
| project t = env_time, a = activityName, d = durationMs, r = resultType, ex = exceptionTypeName,
          detail = substring(resultDescription, 0, 400),
          msgId = tostring(extract_json("$.MessageId", customDimensions))
| order by t asc
| take 600"""
    trace_q = f"""
TraceEvents
| where env_time between ({window} .. {window_end})
| where clientSessionId == "{cid}"
| where message has_any ("Published version:", "Logged execute_tool", "Search completed", "OperationKind",
    "content filter results", "prompt filter results", "harm category", "ContentFilter", "modelSeries:",
    "LLM call completed", "Exception", "\\"Usage\\"", "Time to first token", "BotDefinition Version")
   or activityName startswith "TranscriptTelemetry."
| project t = env_time, a = activityName, m = substring(message, 0, 4000)
| order by t asc
| take 3000"""
    children_q = f"""
OperationEvents
| where env_time between ({window} .. {window_end})
| where clientSessionId startswith "{cid}_"
| where activityName == "MessageProcessor.ExecuteAsync"
| summarize turns = count(), failed = countif(resultType != "Success")
          by conversationId = clientSessionId, schema = tostring(extract_json("$.BotSchemaName", customDimensions))"""
    ops, traces, children = await asyncio.gather(
        query(region, ops_q), query(region, trace_q),
        query(region, children_q) if "_" not in cid else asyncio.sleep(0, result=[]),
    )
    result = _assemble(cid, region, tenant, cd, ops, traces)
    result["connected_agents"] = children
    if "_" in cid:
        result["parent_conversation_id"] = cid.split("_", 1)[0]
    result["ok"] = True
    return result


def _flatten_kv(obj, out: dict | None = None) -> dict:
    """Flatten TranscriptTelemetry's [{"Key":..,"Value":..}] structure into a flat dict (last write wins)."""
    out = {} if out is None else out
    if isinstance(obj, list):
        for x in obj:
            if isinstance(x, dict) and "Key" in x:
                v = x.get("Value")
                if isinstance(v, (list, dict)):
                    _flatten_kv(v, out)
                else:
                    out[str(x["Key"])] = v
            elif isinstance(x, dict) and "Name" in x and "Value" in x:
                out.setdefault("_metrics", {})[str(x["Name"])] = x["Value"]
            else:
                _flatten_kv(x, out)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (list, dict)):
                _flatten_kv(v, out)
            else:
                out[k] = v
    return out


def _assemble(cid: str, region: str, tenant: str, cd: str, ops: list[dict], traces: list[dict]) -> dict:
    versions: list[str] = []
    llm: list[dict] = []
    searches: list[dict] = []
    tools: list[dict] = []
    cf_signals: list[dict] = []
    model_series: set[str] = set()
    tool_calls: list[dict] = []
    prompts: list[dict] = []
    usage: list[dict] = []
    filter_counts: list[dict] = []
    ttft: list[int] = []
    ks_types: dict[str, str] = {}
    draft_versions: set[str] = set()
    for row in traces:
        m, t, a = row.get("m") or "", row.get("t"), row.get("a") or ""
        if a.startswith("TranscriptTelemetry."):
            try:
                kv = _flatten_kv(json.loads(m))
            except (json.JSONDecodeError, TypeError):
                continue
            if kv.get("providedKnowledgeSourceTypes"):
                ks_types["provided"] = kv["providedKnowledgeSourceTypes"]
            if kv.get("leveragedKnowledgeSourceTypes"):
                ks_types["leveraged"] = kv["leveragedKnowledgeSourceTypes"]
            if a.endswith("ToolCallStarted") or a.endswith("ToolExecution"):
                tid = kv.get("ToolCallId")
                existing = next((x for x in tool_calls if x["id"] == tid), None)
                if existing:
                    existing["status"] = kv.get("ToolCallStatus") or existing["status"]
                else:
                    tool_calls.append({"t": t, "id": tid, "name": kv.get("ToolName"), "status": kv.get("ToolCallStatus"),
                                       "turn": kv.get("TurnId")})
            elif a.endswith("MainlineLLMCall"):
                prompts.append({"t": t, "prompt": kv.get("PromptName"), "model": kv.get("ModelName"),
                                "family": kv.get("ModelFamily"), "metrics": kv.get("_metrics", {}), "turn": kv.get("TurnId")})
            continue
        if mv := re.search(r"Published version:\s*(\S+)", m):
            if mv.group(1) not in versions:
                versions.append(mv.group(1))
        elif md := re.search(r"Fetched latest (draft|published)? ?BotDefinition Version:\s*(\d+)", m):
            draft_versions.add(f"{md.group(1) or 'published'} v{md.group(2)}")
        elif m.startswith("{") and '"OperationKind"' in m:
            try:
                j = json.loads(m)
            except json.JSONDecodeError:
                continue
            llm.append({
                "t": t, "model": j.get("ModelName"), "kind": j.get("OperationKind"), "outcome": j.get("Outcome"),
                "drop_reason": j.get("DropReason"), "stop": j.get("FinalStopCode"),
                "elapsed_ms": j.get("ElapsedTime"), "rai_policy": j.get("RaiFilter"),
            })
        elif m.startswith('{"Usage"'):
            try:
                u = json.loads(m).get("Usage") or {}
                usage.append({"t": t, "prompt": u.get("promptTokens"), "completion": u.get("completionTokens"),
                              "total": u.get("totalTokens")})
            except json.JSONDecodeError:
                pass
        elif mf := re.search(r"Received (\d+) (content|prompt) filter results", m):
            filter_counts.append({"t": t, "kind": mf.group(2), "count": int(mf.group(1))})
        elif mt := re.search(r"Time to first token:\s*(\d+)", m):
            ttft.append(int(mt.group(1)))
        elif ms := re.search(r"Search completed for (\w+) with (\d+) results and (\d+) filtered results", m):
            searches.append({"t": t, "source": ms.group(1), "results": int(ms.group(2)), "kept": int(ms.group(3))})
        elif ms := re.search(r"Search completed with (\d+) results and (\d+) filtered results\.\s*(\d+) search errors", m):
            searches.append({"t": t, "source": "UniversalSearch (total)", "results": int(ms.group(1)),
                             "kept": int(ms.group(2)), "errors": int(ms.group(3))})
        elif mt := re.search(r"execute_tool for ToolType:\s*([^,]+).*?SpanId:\s*([^,]*),.*?HasError:\s*(\w+)", m):
            span = mt.group(2).strip()
            if not any(x.get("span") == span for x in tools):
                tools.append({"t": t, "type": mt.group(1).strip(), "span": span, "error": mt.group(3) == "True"})
        elif mm := re.search(r"modelSeries:\s*(\w+)", m):
            model_series.add(mm.group(1))
        elif "harm category" in m or "ContentFilter" in m:
            cf_signals.append({"t": t, "message": m[:300]})

    turns = [
        {"t": o["t"], "duration_ms": o.get("d"), "result": o.get("r"), "message_id": o.get("msgId")}
        for o in ops if o.get("a") == "MessageProcessor.ExecuteAsync"
    ]
    exceptions = [
        {"t": o["t"], "activity": o.get("a"), "type": o.get("ex"), "detail": o.get("detail")}
        for o in ops if o.get("ex")
    ]
    stages = [
        {"t": o["t"], "activity": o.get("a"), "duration_ms": o.get("d"), "result": o.get("r")}
        for o in ops if o.get("a") != "MessageProcessor.ExecuteAsync" and not o.get("ex")
    ]
    return {
        "conversation_id": cid,
        "region": region,
        "tenant_id": tenant,
        "bot": {
            "schema": _cd(cd, "BotSchemaName"), "cds_bot_id": _cd(cd, "CdsBotId"), "channel": _cd(cd, "ChannelId"),
            "environment_id": _cd(cd, "EnvironmentId"), "design_mode": _cd(cd, "IsDesignMode"),
        },
        "published_versions": versions,
        "planner_model_series": sorted(model_series),
        "turns": turns,
        "stages": stages,
        "llm_calls": llm,
        "searches": searches,
        "tools": tools,
        "content_filter_signals": cf_signals[:20],
        "exceptions": exceptions[:50],
        "tool_calls": tool_calls,
        "prompts": prompts,
        "usage": usage,
        "filter_counts": filter_counts,
        "ttft_ms": ttft,
        "knowledge_source_types": ks_types,
        "definition_versions": sorted(draft_versions),
        "verdict": _verdict(turns, llm, searches, tools, exceptions, _cd(cd, "ChannelId")),
    }


def _verdict(turns, llm, searches, tools, exceptions, channel: str = "") -> dict:
    ex_types = " ".join(str(e.get("type") or "") + " " + str(e.get("detail") or "") for e in exceptions)
    if "ContentFilter" in ex_types or any((c.get("drop_reason") or "").lower().startswith("content") for c in llm):
        return {"stage": "rai", "headline": "Content safety filter blocked the answer during generation",
                "detail": "Retrieval worked; the model's output tripped the content filter, so the user saw a "
                          "generic apology. Check content moderation level and how much sensitive policy text "
                          "the knowledge sources return for this question."}
    if any(t["error"] for t in tools) or any(e for e in exceptions if "Connector" in str(e.get("activity"))):
        return {"stage": "tool", "headline": "A tool or connector call failed",
                "detail": "The planner selected a tool but its execution returned an error. Check connector "
                          "auth, inputs mapped from instructions, and the flow run history."}
    totals = [s for s in searches if s["source"].startswith("UniversalSearch")] or searches
    if totals and all(s["results"] == 0 for s in totals):
        return {"stage": "search", "headline": "Knowledge search returned nothing",
                "detail": "The query reached search but matched no content. Compare the rewritten query with "
                          "the knowledge source; instruction rules that augment queries are a common cause."}
    if totals and all(s["results"] > 0 and s["kept"] == 0 for s in totals):
        return {"stage": "search", "headline": "Search found content but filtering removed all of it",
                "detail": "Results existed but none survived relevance/permission filtering."}
    if any(t.get("result") not in (None, "Success") for t in turns) or exceptions:
        return {"stage": "runtime", "headline": "Runtime reported a failure",
                "detail": "See exceptions for the failing activity."}
    if not searches and not tools and not llm and channel == "m365copilot":
        return {"stage": "orchestrator", "headline": "Answered by the M365 Copilot orchestrator",
                "detail": "This is a declarative agent: planning, knowledge search, and answer generation ran in the "
                          "M365 Copilot orchestrator. The Copilot Studio runtime only recorded the agent handoff, so "
                          "search and answer details aren't in this telemetry. Compare the answer with what the "
                          "instructions require for this question."}
    if not searches and not tools:
        return {"stage": "routing", "headline": "No knowledge search or tool ran",
                "detail": "The orchestrator answered without grounding (or only ran topics). If the answer was "
                          "wrong or generic, look at routing and grounding rules in the instructions."}
    return {"stage": "synthesis", "headline": "Runtime succeeded end to end",
            "detail": "Search and tools completed without errors, so a bad answer most likely comes from how the "
                      "instructions shape selection and synthesis. Cross-check the validator findings."}
