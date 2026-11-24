"""Live agent access via Dataverse: load instructions, keep version snapshots, swap + publish for evals."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ess_maker.doctor.agent_file import AgentDoc, Line
from ess_maker.publish.dataverse import _headers, get_access_token, get_bot_id, resolve_org_url
from ess_maker.settings import settings

COMPONENT_TYPE_CUSTOM_GPT = 15
COMPONENT_TYPE_TOPIC_V2 = 9
SNAP_ROOT = Path(settings.workdir) / "doctor" / "snapshots"


class LiveError(RuntimeError):
    pass


PVA_RESOURCE = "96ff4394-9197-43aa-b393-6a41652e21f8"  # Power Virtual Agents service (Copilot Studio gateway)
_CAPABILITY_LABELS = {
    "webBrowsing": "Search the whole web",
    "searchPeople": "Use org chart and profile data to answer people questions",
    "codeInterpreter": "Create code, graphs, and documents",
    "generateImages": "Create images",
}


async def fetch_agent_definition(tenant_id: str, environment_id: str, agent_id: str) -> dict:
    """Read the saved agent straight from Copilot Studio (the same call its Configure page makes).

    Uses the maker's `az` sign-in to the agent's tenant; new-UX agents aren't in Dataverse, so this is the only
    direct read path. Raises LiveError with a maker-facing message on sign-in or permission problems.
    """
    if not settings.doctor_cs_gateway:
        raise LiveError("Set DOCTOR_CS_GATEWAY in .env to the Copilot Studio gateway for your environment's region.")
    proc = await asyncio.create_subprocess_shell(
        f'az account get-access-token --tenant {tenant_id} --resource {PVA_RESOURCE} --query accessToken -o tsv',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    token = out.decode().strip()
    if proc.returncode or not token:
        raise LiveError("Sign in to the agent's tenant first: az login --tenant "
                        f"{tenant_id} --use-device-code --allow-no-subscriptions "
                        f"--scope {PVA_RESOURCE}/.default")
    url = (f"{settings.doctor_cs_gateway.rstrip('/')}/chatbotmanagement/tenants/{tenant_id}/environments/"
           f"{environment_id}/minimalBots/api/{agent_id}/components")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "x-cci-cdsbotid": agent_id, "x-cci-tenantid": tenant_id, "x-cci-applicationsource": "Web"}
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(url, headers=headers, content=b"{}")
    if r.status_code in (401, 403):
        raise LiveError(f"Copilot Studio refused the request ({r.status_code}). Sign in again with az login for tenant "
                        f"{tenant_id}, or check you're a maker on this agent.")
    if r.status_code != 200:
        raise LiveError(f"Copilot Studio returned {r.status_code}: {r.text[:200]}")
    d = r.json()
    comps = [ch.get("component") or {} for ch in d.get("botComponentChanges") or []]
    gpt = next((c for c in comps if c.get("$kind") == "GptComponent"), None)
    if not gpt:
        raise LiveError("This agent has no instructions component (GptComponent).")
    md = gpt.get("metadata") or {}
    segs = (md.get("instructions") or {}).get("segments") or []
    text = "".join(s.get("value") or (f"{{{s.get('variable') or s.get('name')}}}" if s.get("$kind") != "TextSegment" else "")
                   for s in segs)
    bot = d.get("bot") or {}
    ai = ((bot.get("configuration") or {}).get("aISettings")) or {}
    caps = md.get("gptCapabilities") or {}
    dialogs = [c for c in comps if c.get("$kind") == "DialogComponent"]
    knowledge = [c for c in comps if c.get("$kind") == "KnowledgeSourceComponent"]
    return {
        "text": text,
        "name": bot.get("displayName") or gpt.get("displayName") or "Copilot Studio agent",
        "schema": (gpt.get("schemaName") or "").split(".gpt.")[0],
        "modified": ((gpt.get("auditInfo") or {}).get("modifiedTimeUtc") or "")[:19] + "Z",
        "published": (bot.get("publishedOn") or "")[:19] + "Z" if bot.get("publishedOn") else "",
        "content_moderation": ai.get("contentModeration", ""),
        "use_model_knowledge": (md.get("aISettings") or {}).get("useModelKnowledge"),
        "model_hint": ((md.get("aISettings") or {}).get("model") or {}).get("modelNameHint", ""),
        "capabilities": {_CAPABILITY_LABELS.get(k, k): bool(v) for k, v in caps.items() if k != "$kind"},
        "knowledge": [{"name": k.get("displayName") or k.get("schemaName", ""), "url": ""} for k in knowledge],
        "topics_total": len(dialogs),
        "topics_active": sum(1 for c in dialogs if (c.get("state") or "Active") == "Active"),
        "topic_list": [{"name": c.get("displayName") or "", "schema": c.get("schemaName") or "",
                        "state": c.get("state") or "Active", "description": c.get("description") or ""} for c in dialogs],
    }


async def _client() -> tuple[httpx.AsyncClient, str]:
    org_url, _ = await resolve_org_url()
    if not org_url:
        raise LiveError("No Dataverse org URL. Set DATAVERSE_ORG_URL or run `pac auth create`.")
    token, err = await get_access_token(org_url)
    if not token:
        raise LiveError(err or "Could not get a Dataverse token.")
    return httpx.AsyncClient(base_url=org_url, headers=_headers(token), timeout=60.0), org_url


def _block(lines: list[str], key: str) -> tuple[int, int, int] | None:
    """Return (key_line_idx, body_start, body_end) for a YAML literal block `key: |`."""
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith(f"{key}:") and s.split(":", 1)[1].strip().startswith("|"):
            ind = len(raw) - len(raw.lstrip(" "))
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip(" ")) > ind):
                j += 1
            end = j
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return i, i + 1, end
    return None


def instructions_from_data(data: str) -> list[Line]:
    lines = data.splitlines()
    b = _block(lines, "instructions")
    if not b:
        return []
    _, s, e = b
    body = [ln for ln in lines[s:e] if ln.strip()]
    ind = min((len(ln) - len(ln.lstrip(" ")) for ln in body), default=0)
    return [Line(k + 1, ln[ind:] if ln.strip() else "") for k, ln in enumerate(lines[s:e], start=s)]


def replace_instructions(data: str, new_text: str) -> str:
    lines = data.splitlines()
    b = _block(lines, "instructions")
    if not b:
        raise LiveError("No `instructions: |` block found in the component data.")
    k, s, e = b
    key_ind = len(lines[k]) - len(lines[k].lstrip(" "))
    pad = " " * (key_ind + 2)
    body = [pad + ln if ln.strip() else "" for ln in new_text.splitlines()]
    return "\n".join(lines[:s] + body + lines[e:]) + ("\n" if data.endswith("\n") else "")


async def load_live(schema: str | None = None) -> dict:
    schema = schema or settings.mcs_bot_schema
    if not schema:
        raise LiveError("Set MCS_BOT_SCHEMA (Copilot Studio > Settings > Advanced > Schema name).")
    client, org_url = await _client()
    async with client:
        bot_id, err = await get_bot_id(client, schema)
        if not bot_id:
            raise LiveError(err or f"Bot {schema} not found in {org_url}.")
        r = await client.get(
            "/api/data/v9.2/botcomponents",
            params={
                "$filter": f"_parentbotid_value eq {bot_id} and componenttype eq {COMPONENT_TYPE_CUSTOM_GPT}",
                "$select": "botcomponentid,name,schemaname,data,modifiedon,_modifiedby_value",
            },
        )
        r.raise_for_status()
        comps = r.json().get("value", [])
        if not comps:
            raise LiveError("No Custom GPT component (instructions) found on this agent.")
        gpt = comps[0]
        t = await client.get(
            "/api/data/v9.2/botcomponents",
            params={
                "$filter": f"_parentbotid_value eq {bot_id} and componenttype eq {COMPONENT_TYPE_TOPIC_V2}",
                "$select": "schemaname,name,statecode",
            },
        )
        topics = t.json().get("value", []) if t.status_code == 200 else []
        b = await client.get(f"/api/data/v9.2/bots({bot_id})", params={"$select": "name,publishedon,configuration"})
        bot = b.json() if b.status_code == 200 else {}

    doc = AgentDoc(name=bot.get("name") or schema, source=f"dataverse:{org_url}")
    doc.bot_schema = schema
    doc.instructions = instructions_from_data(gpt.get("data") or "")
    doc.instructions_line = doc.instructions[0].no - 1 if doc.instructions else 0
    doc.modified_utc = gpt.get("modifiedon", "")
    doc.is_full_export = bool(topics)
    from ess_maker.doctor.agent_file import TopicInfo

    for tp in topics:
        doc.topics[tp["schemaname"]] = TopicInfo(tp["schemaname"], tp.get("name", ""),
                                                 "Active" if tp.get("statecode") == 0 else "Inactive")
    cfg = bot.get("configuration") or ""
    if m := re.search(r'"contentModeration"\s*:\s*"(\w+)"', cfg):
        doc.content_moderation = m.group(1)
    snap = save_snapshot(bot_id, gpt.get("data") or "", gpt.get("modifiedon", ""))
    return {
        "doc": doc, "bot_id": bot_id, "component_id": gpt["botcomponentid"], "data": gpt.get("data") or "",
        "published_on": bot.get("publishedon"), "snapshot": snap,
    }


def save_snapshot(bot_id: str, data: str, modified: str, kind: str = "yaml", config: dict | None = None) -> dict:
    d = SNAP_ROOT / bot_id
    d.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256((data + json.dumps(config or {}, sort_keys=True)).encode("utf-8")).hexdigest()[:12]
    existing = sorted(d.glob("*.json"))
    if existing and json.loads(existing[-1].read_text(encoding="utf-8"))["hash"] == h:
        return {"new": False, "hash": h, "count": len(existing)}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (d / f"{stamp}_{h}.json").write_text(
        json.dumps({"hash": h, "captured": stamp, "modifiedon": modified, "kind": kind, "data": data,
                    "config": config or {}}) + "\n",
        encoding="utf-8",
    )
    return {"new": True, "hash": h, "count": len(existing) + 1}


def list_snapshots(bot_id: str) -> list[dict]:
    d = SNAP_ROOT / bot_id
    out = []
    for p in sorted(d.glob("*.json")):
        j = json.loads(p.read_text(encoding="utf-8"))
        out.append({"id": p.stem, "hash": j["hash"], "captured": j["captured"], "modifiedon": j.get("modifiedon")})
    return out


def snapshot_doc(bot_id: str, snap_id: str, name: str) -> AgentDoc:
    p = SNAP_ROOT / bot_id / f"{snap_id}.json"
    if not p.exists():
        raise LiveError(f"Snapshot {snap_id} not found.")
    j = json.loads(p.read_text(encoding="utf-8"))
    doc = AgentDoc(name=f"{name} @ {j['captured']}", source=f"snapshot:{snap_id}")
    if j.get("kind") == "text":
        doc.instructions = [Line(k + 1, t) for k, t in enumerate(j["data"].splitlines())]
    else:
        doc.instructions = instructions_from_data(j["data"])
    return doc


async def change_history(component_id: str) -> dict:
    """Dataverse audit history for the instructions component (requires auditing on botcomponent)."""
    client, _ = await _client()
    async with client:
        r = await client.get(
            "/api/data/v9.2/RetrieveRecordChangeHistory(Target=@t)",
            params={"@t": json.dumps({"@odata.id": f"botcomponents({component_id})"})},
        )
    if r.status_code != 200:
        return {"ok": False, "error": f"Audit history unavailable ({r.status_code}). Enable auditing on the "
                                       "Agent component table, or rely on local snapshots."}
    details = r.json().get("AuditDetailCollection", {}).get("AuditDetails", [])
    out = []
    for d in details:
        rec = d.get("AuditRecord", {})
        new_v = (d.get("NewValue") or {}).get("data")
        old_v = (d.get("OldValue") or {}).get("data")
        out.append({
            "when": rec.get("createdon"), "user": rec.get("_userid_value@OData.Community.Display.V1.FormattedValue")
            or rec.get("_userid_value"), "operation": rec.get("operation"),
            "has_instruction_change": bool(new_v and old_v and new_v != old_v),
            "old_data": old_v, "new_data": new_v,
        })
    return {"ok": True, "entries": out}


async def swap_and_publish(bot_id: str, component_id: str, data: str, wait_s: int = 45) -> dict:
    client, _ = await _client()
    async with client:
        r = await client.patch(f"/api/data/v9.2/botcomponents({component_id})", json={"data": data})
        if r.status_code not in (200, 204):
            raise LiveError(f"Update failed {r.status_code}: {r.text[:300]}")
        p = await client.post(f"/api/data/v9.2/bots({bot_id})/Microsoft.Dynamics.CRM.PvaPublish", json={})
        if p.status_code not in (200, 204):
            raise LiveError(f"Publish failed {p.status_code}: {p.text[:300]}")
    await asyncio.sleep(wait_s)
    return {"ok": True}
