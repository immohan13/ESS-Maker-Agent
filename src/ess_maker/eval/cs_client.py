"""Wrapper around the official Microsoft Copilot Studio Python client.

Uses microsoft-agents-copilotstudio-client to talk to agents published to
Microsoft 365 / Teams (no DirectLine secret needed). Auth is AAD via
DefaultAzureCredential (e.g. `az login` as the test user).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from pathlib import Path

import msal
from azure.identity.aio import DefaultAzureCredential
from microsoft_agents.copilotstudio.client import (
    ConnectionSettings,
    CopilotClient,
)

from ess_maker.eval.schemas import AgentTurn
from ess_maker.settings import settings

_TOKEN_CACHE = Path(settings.workdir) / "msal_token_cache.bin"


def _msal_token(scope: str, app_client_id: str, tenant_id: str) -> str:
    """Public-client interactive/device-code MSAL flow with on-disk token cache."""
    cache = msal.SerializableTokenCache()
    if _TOKEN_CACHE.exists():
        cache.deserialize(_TOKEN_CACHE.read_text())
    app = msal.PublicClientApplication(
        app_client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent([scope], account=accounts[0])
    if not result:
        flow = app.initiate_device_flow(scopes=[scope])
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start device flow: {flow}")
        print("\n" + flow["message"] + "\n", flush=True)
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE.write_text(cache.serialize())
    if "access_token" not in result:
        raise RuntimeError(f"MSAL failed: {result.get('error')}: {result.get('error_description')}")
    return result["access_token"]


@dataclass
class CsConversation:
    conversation_id: str | None = None


class CopilotStudioClient:
    """Drives a Copilot Studio agent published to M365/Teams."""

    def __init__(
        self,
        env_id: str | None = None,
        schema_name: str | None = None,
    ):
        self.env_id = env_id or settings.mcs_env_id
        self.schema_name = schema_name or settings.mcs_bot_schema
        if not (self.env_id and self.schema_name):
            raise RuntimeError(
                "Set MCS_ENV_ID and MCS_BOT_SCHEMA in .env to use the M365/Teams adapter."
            )
        self._settings = ConnectionSettings(
            environment_id=self.env_id,
            agent_identifier=self.schema_name,
        )
        self._scope = CopilotClient.scope_from_settings(self._settings)
        self._cred = DefaultAzureCredential()
        self._client: CopilotClient | None = None
        self._pending: tuple[CsConversation, str] | None = None

    async def _ensure_client(self) -> CopilotClient:
        if self._client is None:
            if settings.app_client_id and settings.tenant_id:
                token = _msal_token(self._scope, settings.app_client_id, settings.tenant_id)
            else:
                tok = await self._cred.get_token(self._scope)
                token = tok.token
            self._client = CopilotClient(self._settings, token)
        return self._client

    async def start(self) -> CsConversation:
        client = await self._ensure_client()
        conv_id: str | None = None
        async for activity in client.start_conversation(emit_start_conversation_event=True):
            cid = getattr(activity, "conversation", None)
            if cid is not None:
                conv_id = getattr(cid, "id", None) or conv_id
        return CsConversation(conversation_id=conv_id)

    async def send(self, conv: CsConversation, text: str) -> None:
        # ask_question both sends and reads; queue the prompt for poll_reply
        self._pending = (conv, text)

    async def poll_reply(
        self,
        conv: CsConversation,
        *,
        max_wait_s: float = 45.0,
        quiet_window_s: float = 2.0,
    ) -> tuple[list[AgentTurn], str]:
        client = await self._ensure_client()
        if self._pending is None:
            return [], ""
        _, text = self._pending
        self._pending = None
        collected: list[AgentTurn] = []
        async for activity in client.ask_question(text, conversation_id=conv.conversation_id):
            atype = getattr(activity, "type", None)
            txt = getattr(activity, "text", None) or ""
            cid = getattr(activity, "conversation", None)
            if cid and not conv.conversation_id:
                conv.conversation_id = getattr(cid, "id", None)
            if atype == "message" and txt:
                collected.append(AgentTurn(role="agent", text=txt, activity_type=str(atype)))
        return collected, ""

    async def aclose(self) -> None:
        await self._cred.close()


async def ping() -> dict:
    """Quick connection check: start a conversation and ask one question."""
    started = time.monotonic()
    client = CopilotStudioClient()
    try:
        conv = await client.start()
        await client.send(conv, "Hello")
        turns, _ = await client.poll_reply(conv, max_wait_s=20)
        return {
            "ok": True,
            "conversation_id": conv.conversation_id,
            "env_id": client.env_id,
            "schema": client.schema_name,
            "first_reply_chars": len(turns[0].text) if turns else 0,
            "reply_count": len(turns),
            "first_reply_preview": turns[0].text[:200] if turns else "",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        await client.aclose()
