"""Minimal Bot Framework DirectLine v3 client for talking to a Copilot Studio agent."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

import httpx

from ess_maker.settings import settings
from ess_maker.eval.schemas import AgentTurn


@dataclass
class Conversation:
    conversation_id: str
    token: str
    stream_url: str | None = None


class DirectLineClient:
    """Async DirectLine client. Uses long-poll over /activities for simplicity.

    Auth modes (in priority order):
      1. mcs_token_endpoint (Copilot Studio "Token endpoint" from Custom website / Mobile app
         channel). We GET it to obtain a fresh token + conversationId on each session.
      2. directline_secret (classic Bot Framework DirectLine channel).
    """

    def __init__(
        self,
        secret: str | None = None,
        endpoint: str | None = None,
        token_endpoint: str | None = None,
    ):
        self.secret = secret or settings.directline_secret
        self.token_endpoint = token_endpoint or settings.mcs_token_endpoint
        self.base = (endpoint or settings.directline_endpoint).rstrip("/")
        self._user_id = f"ess-maker-{uuid.uuid4().hex[:8]}"

    async def _get_mcs_token(self) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(self.token_endpoint)
            r.raise_for_status()
            return r.json()  # { token, conversationId, expires_in, tokenEndpointURL? }

    async def start(self) -> Conversation:
        if self.token_endpoint:
            data = await self._get_mcs_token()
            token = data["token"]
            conv_id = data.get("conversationId")
            if not conv_id:
                # Some MCS responses only return token; start a conversation with it.
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.post(
                        f"{self.base}/conversations",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    r.raise_for_status()
                    j = r.json()
                    conv_id = j["conversationId"]
                    token = j.get("token", token)
            return Conversation(conversation_id=conv_id, token=token)

        if not self.secret:
            raise RuntimeError(
                "Configure either MCS_TOKEN_ENDPOINT (Copilot Studio) or DIRECTLINE_SECRET (Bot Framework)."
            )
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{self.base}/conversations",
                headers={"Authorization": f"Bearer {self.secret}"},
            )
            r.raise_for_status()
            data = r.json()
            return Conversation(
                conversation_id=data["conversationId"],
                token=data["token"],
                stream_url=data.get("streamUrl"),
            )

    async def send(self, conv: Conversation, text: str) -> None:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{self.base}/conversations/{conv.conversation_id}/activities",
                headers={"Authorization": f"Bearer {conv.token}"},
                json={
                    "type": "message",
                    "from": {"id": self._user_id, "name": "ESS Maker"},
                    "text": text,
                },
            )
            r.raise_for_status()

    async def poll_reply(
        self, conv: Conversation, *, since_watermark: str | None = None,
        max_wait_s: float = 30.0, quiet_window_s: float = 1.5,
    ) -> tuple[list[AgentTurn], str]:
        """Poll /activities until the bot stops sending for `quiet_window_s` or timeout."""
        deadline = time.monotonic() + max_wait_s
        watermark = since_watermark
        collected: list[AgentTurn] = []
        last_recv = time.monotonic()
        async with httpx.AsyncClient(timeout=30) as c:
            while time.monotonic() < deadline:
                params = {"watermark": watermark} if watermark else {}
                r = await c.get(
                    f"{self.base}/conversations/{conv.conversation_id}/activities",
                    headers={"Authorization": f"Bearer {conv.token}"},
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
                watermark = data.get("watermark") or watermark
                got_new = False
                for act in data.get("activities", []):
                    if act.get("from", {}).get("id") == self._user_id:
                        continue
                    if act.get("type") == "message":
                        collected.append(
                            AgentTurn(
                                role="agent",
                                text=act.get("text") or "",
                                activity_type=act.get("type"),
                            )
                        )
                        got_new = True
                if got_new:
                    last_recv = time.monotonic()
                if collected and (time.monotonic() - last_recv) >= quiet_window_s:
                    break
                await asyncio.sleep(0.7)
        return collected, watermark or ""
