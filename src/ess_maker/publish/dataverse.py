"""Dataverse Web API integration for ESS Maker.

Writes knowledge sources directly to a Copilot Studio agent via the
`botcomponents` table, bypassing solution pack/import entirely. The KB shows
up in Copilot Studio's Knowledge tab immediately and is ready to chat after
the maker clicks **Publish** on the agent itself.

Auth strategy (in order):
  1. Azure CLI: `az account get-access-token --resource <org_url>` (preferred,
     no app registration needed; reuses whatever the maker is signed in as).
  2. (future) MSAL device-code if APP_CLIENT_ID + TENANT_ID are set.

Org URL resolution (in order):
  1. Settings.dataverse_org_url (env var DATAVERSE_ORG_URL)
  2. Active `pac auth list` profile's "Environment Url"
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any

import httpx

from ..settings import settings
from . import pac as pac_mod


# Component type 16 == SearchAndSummarizeContent (a.k.a. Generative Knowledge
# source). Confirmed by inspecting an existing live SharePoint KB on the HR
# bot in the Mohan Maker Env environment on 2026-04-26.
COMPONENT_TYPE_KNOWLEDGE = 16


@dataclass
class DvResult:
    ok: bool
    status: int
    body: Any
    error: str | None = None
    hint: str | None = None


# ---------- Org URL resolution --------------------------------------------

async def resolve_org_url() -> tuple[str | None, str | None]:
    """Return (org_url, source). source describes where it came from."""
    cfg = (settings.__dict__.get("dataverse_org_url") or "").strip()
    if cfg:
        return cfg.rstrip("/"), "settings.dataverse_org_url"

    status = await pac_mod.pac_status()
    auth = status.get("auth") or {}
    raw = auth.get("raw") or ""
    # The "Environment Url" is the last whitespace-separated token if present.
    # Format observed: "[2] * UNIVERSAL <user> Public User <env_name> https://orgxxxx.crm.dynamics.com/"
    m = re.search(r"(https?://[\w.-]+)/?\s*$", raw)
    if m:
        return m.group(1).rstrip("/"), "pac auth list (active profile)"
    return None, None


# ---------- Token acquisition ---------------------------------------------

def _find_az() -> str | None:
    """Locate az CLI. Prefer PATH, fall back to common Windows install dirs."""
    import shutil
    found = shutil.which("az") or shutil.which("az.cmd") or shutil.which("az.exe")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
        r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


async def get_access_token(org_url: str) -> tuple[str | None, str | None]:
    """Try DATAVERSE_TOKEN env var, then `az account get-access-token`."""
    tok = (os.environ.get("DATAVERSE_TOKEN") or "").strip()
    if tok:
        return tok, None
    az = _find_az()
    if not az:
        return None, "Azure CLI (`az`) not found on PATH. Install it or set DATAVERSE_TOKEN env var."
    try:
        # Use shell=False with the resolved path; .cmd needs shell on Windows
        # asyncio. Use subprocess_shell to handle .cmd reliably.
        tenant = (settings.__dict__.get("dataverse_tenant_id") or "").strip()
        tenant_arg = f' --tenant "{tenant}"' if tenant else ""
        cmd = f'"{az}" account get-access-token --resource "{org_url}"{tenant_arg} --output json'
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except FileNotFoundError:
        return None, "Azure CLI (`az`) not found on PATH. Install it or set DATAVERSE_TOKEN env var."
    except asyncio.TimeoutError:
        return None, "az account get-access-token timed out"
    if proc.returncode != 0:
        msg = (err or b"").decode(errors="replace").strip().splitlines()[-1] if err else "unknown error"
        return None, f"az failed: {msg}"
    try:
        data = json.loads((out or b"").decode(errors="replace"))
        return data.get("accessToken"), None
    except json.JSONDecodeError as e:
        return None, f"could not parse az output: {e}"


# ---------- Bot lookup ----------------------------------------------------

async def get_bot_id(client: httpx.AsyncClient, schema: str) -> tuple[str | None, str | None]:
    """Return (botid, error)."""
    if not schema:
        return None, "MCS_BOT_SCHEMA is not set in your .env (e.g. msdyn_copilotforemployeeselfservicehr)"
    r = await client.get(
        "/api/data/v9.2/bots",
        params={"$filter": f"schemaname eq '{schema}'", "$select": "botid,name,schemaname"},
    )
    if r.status_code != 200:
        return None, f"bot lookup failed: HTTP {r.status_code} {r.text[:300]}"
    items = (r.json() or {}).get("value") or []
    if not items:
        return None, f"no bot found with schemaname `{schema}` in this environment"
    return items[0]["botid"], None


# ---------- Schema name + body builders -----------------------------------

_SCHEMA_SAFE_RE = re.compile(r"[^A-Za-z0-9]+")


def _safe_schema_token(name: str) -> str:
    """Convert a free-form name into a safe Pascal-ish token (alphanumeric)."""
    parts = [p for p in _SCHEMA_SAFE_RE.split(name) if p]
    if not parts:
        return "Knowledge"
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Knowledge"


def _suffix_22() -> str:
    """22 url-ish-safe chars derived from random bytes (matches MCS pattern)."""
    raw = secrets.token_bytes(16)
    s = base64.b64encode(raw).decode("ascii")
    return re.sub(r"[/+=]", "x", s)[:22]


def build_kb_body(
    *,
    bot_schema: str,
    bot_id: str,
    name: str,
    description: str,
    sharepoint_url: str,
) -> dict[str, Any]:
    """Build the JSON payload that gets POSTed to /botcomponents."""
    schema_token = _safe_schema_token(name)
    schema = f"{bot_schema}.topic.{schema_token}__{_suffix_22()}"
    data = (
        "kind: KnowledgeSourceConfiguration\r\n"
        "source:\r\n"
        "  kind: SharePointSearchSource\r\n"
        f"  site: {sharepoint_url.strip()}"
    )
    return {
        "name": name.strip(),
        "description": (description or "").strip()
            or f"This knowledge source provides information found in {name.strip()} SharePoint.",
        "componenttype": COMPONENT_TYPE_KNOWLEDGE,
        "schemaname": schema,
        "data": data,
        "parentbotid@odata.bind": f"/bots({bot_id})",
    }


# ---------- Public API ----------------------------------------------------

async def status() -> dict[str, Any]:
    """Diagnostics: can we reach Dataverse and find the bot?"""
    org_url, source = await resolve_org_url()
    if not org_url:
        return {
            "ok": False,
            "step": "resolve org url",
            "error": "could not determine the Dataverse org URL",
            "hint": (
                "Either set `DATAVERSE_ORG_URL=https://orgxxxx.crm.dynamics.com` in `.env`, "
                "or run `pac auth create --environment <ENV_GUID>` to register an active profile."
            ),
        }
    token, err = await get_access_token(org_url)
    if not token:
        return {
            "ok": False,
            "step": "get token",
            "org_url": org_url,
            "org_url_source": source,
            "error": err,
            "hint": "Run `az login --tenant <your-tenant>` then try again.",
        }
    schema = settings.mcs_bot_schema or ""
    async with httpx.AsyncClient(base_url=org_url, headers=_headers(token), timeout=30.0) as client:
        bot_id, berr = await get_bot_id(client, schema)
    if not bot_id:
        return {
            "ok": False,
            "step": "lookup bot",
            "org_url": org_url,
            "org_url_source": source,
            "bot_schema": schema,
            "error": berr,
            "hint": "Set `MCS_BOT_SCHEMA` in your `.env` to the bot's schema name (e.g. msdyn_copilotforemployeeselfservicehr).",
        }
    return {
        "ok": True,
        "org_url": org_url,
        "org_url_source": source,
        "bot_schema": schema,
        "bot_id": bot_id,
    }


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def create_sharepoint_kb(
    *,
    name: str,
    description: str,
    sharepoint_url: str,
    bot_schema: str | None = None,
) -> dict[str, Any]:
    """One-shot: resolve env, get token, find bot, POST KB component.

    Returns a dict with {ok, botcomponentid?, location?, name, schema, error?, hint?, step}.
    """
    if not name.strip() or not sharepoint_url.strip():
        return {"ok": False, "step": "validate", "error": "name and sharepoint_url are required"}
    org_url, source = await resolve_org_url()
    if not org_url:
        return {
            "ok": False, "step": "resolve org url",
            "error": "could not determine the Dataverse org URL",
            "hint": "Either set DATAVERSE_ORG_URL in .env or run `pac auth create --environment <ENV_GUID>`.",
        }
    token, err = await get_access_token(org_url)
    if not token:
        return {
            "ok": False, "step": "get token", "org_url": org_url,
            "error": err, "hint": "Run `az login --tenant <your-tenant>` then try again.",
        }
    schema = (bot_schema or settings.mcs_bot_schema or "").strip()
    async with httpx.AsyncClient(base_url=org_url, headers=_headers(token), timeout=60.0) as client:
        bot_id, berr = await get_bot_id(client, schema)
        if not bot_id:
            return {
                "ok": False, "step": "lookup bot", "org_url": org_url, "bot_schema": schema,
                "error": berr,
                "hint": "Set MCS_BOT_SCHEMA in your .env to the agent's schema name.",
            }
        body = build_kb_body(
            bot_schema=schema, bot_id=bot_id,
            name=name, description=description, sharepoint_url=sharepoint_url,
        )
        r = await client.post("/api/data/v9.2/botcomponents", json=body)
        if r.status_code not in (200, 201, 204):
            return {
                "ok": False, "step": "create botcomponent",
                "status": r.status_code, "error": _short_error(r.text),
                "request_body": body,
                "hint": _hint_for_status(r.status_code),
            }
        location = r.headers.get("OData-EntityId") or r.headers.get("Location") or ""
        # Pull the GUID out of the entity URL: .../botcomponents(<guid>)
        m = re.search(r"botcomponents\(([0-9a-fA-F-]+)\)", location)
        botcomponentid = m.group(1) if m else None
    return {
        "ok": True,
        "step": "create botcomponent",
        "botcomponentid": botcomponentid,
        "location": location,
        "org_url": org_url,
        "org_url_source": source,
        "bot_schema": schema,
        "bot_id": bot_id,
        "name": body["name"],
        "schema": body["schemaname"],
        "next_steps": [
            "Refresh the Knowledge tab in Copilot Studio for the agent.",
            "Click Publish (top-right) on the agent to push the change to live chat.",
        ],
    }


def _short_error(body: str) -> str:
    try:
        d = json.loads(body)
        msg = (d.get("error") or {}).get("message") or body[:300]
        return str(msg)[:600]
    except Exception:
        return body[:600]


def _hint_for_status(code: int) -> str | None:
    if code == 401:
        return "Token rejected. Re-run `az login --tenant <your-tenant>` and retry."
    if code == 403:
        return "You don't have write permission on this bot. The agent owner needs to share it with you (Edit role) in Power Apps."
    if code == 400:
        return "Dataverse rejected the payload. The bot's schemaname may not match this environment, or schemaname collision."
    return None


# ---------- Topics (componenttype 9 = AdaptiveDialog) ---------------------

# Component type 9 == Topic / AdaptiveDialog. Verified against the HR bot in
# the Mohan Maker Env on 2026-04-26 (37 of 61 botcomponents on that bot are
# componenttype=9, all with `data` containing `kind: AdaptiveDialog`).
COMPONENT_TYPE_TOPIC = 9


def build_topic_body(
    *,
    bot_schema: str,
    bot_id: str,
    name: str,
    description: str,
    topic_yaml: str,
) -> dict[str, Any]:
    """Build the JSON payload that gets POSTed to /botcomponents for a topic."""
    schema_token = _safe_schema_token(name)
    schema = f"{bot_schema}.topic.{schema_token}__{_suffix_22()}"
    return {
        "name": name.strip(),
        "description": (description or "").strip()
            or f"Topic: {name.strip()}",
        "componenttype": COMPONENT_TYPE_TOPIC,
        "schemaname": schema,
        "data": topic_yaml,
        "parentbotid@odata.bind": f"/bots({bot_id})",
    }


async def create_topic(
    *,
    name: str,
    description: str,
    topic_yaml: str,
    bot_schema: str | None = None,
) -> dict[str, Any]:
    """Write a topic (AdaptiveDialog YAML) directly into the live agent."""
    if not name.strip() or not topic_yaml.strip():
        return {"ok": False, "step": "validate", "error": "name and topic_yaml are required"}
    org_url, source = await resolve_org_url()
    if not org_url:
        return {
            "ok": False, "step": "resolve org url",
            "error": "could not determine the Dataverse org URL",
            "hint": "Either set DATAVERSE_ORG_URL in .env or run `pac auth create --environment <ENV_GUID>`.",
        }
    token, err = await get_access_token(org_url)
    if not token:
        return {
            "ok": False, "step": "get token", "org_url": org_url,
            "error": err, "hint": "Run `az login --tenant <your-tenant>` then try again.",
        }
    schema = (bot_schema or settings.mcs_bot_schema or "").strip()
    async with httpx.AsyncClient(base_url=org_url, headers=_headers(token), timeout=60.0) as client:
        bot_id, berr = await get_bot_id(client, schema)
        if not bot_id:
            return {
                "ok": False, "step": "lookup bot", "org_url": org_url, "bot_schema": schema,
                "error": berr,
                "hint": "Set MCS_BOT_SCHEMA in your .env to the agent's schema name.",
            }
        body = build_topic_body(
            bot_schema=schema, bot_id=bot_id,
            name=name, description=description, topic_yaml=topic_yaml,
        )
        r = await client.post("/api/data/v9.2/botcomponents", json=body)
        if r.status_code not in (200, 201, 204):
            return {
                "ok": False, "step": "create botcomponent",
                "status": r.status_code, "error": _short_error(r.text),
                "hint": _hint_for_status(r.status_code),
                "schema": body["schemaname"],
            }
        location = r.headers.get("OData-EntityId") or r.headers.get("Location") or ""
        m = re.search(r"botcomponents\(([0-9a-fA-F-]+)\)", location)
        botcomponentid = m.group(1) if m else None
    return {
        "ok": True,
        "step": "create botcomponent",
        "botcomponentid": botcomponentid,
        "location": location,
        "org_url": org_url,
        "org_url_source": source,
        "bot_schema": schema,
        "bot_id": bot_id,
        "name": body["name"],
        "schema": body["schemaname"],
        "next_steps": [
            "Refresh the Topics tab in Copilot Studio for the agent.",
            "Click Publish (top-right) on the agent to push the change to live chat.",
        ],
    }
