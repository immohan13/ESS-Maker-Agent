"""Stub client for the Microsoft Copilot Studio MCP server.

Replace _call() with a real MCP client (e.g. the `mcp` python package, streaming-http transport)
once the MCS MCP endpoint is provisioned. For now, every method returns a typed stub so the
ESS Maker agent's tools are exercised end-to-end against the local file workspace.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ess_maker.settings import settings


class McsMcpClient:
    def __init__(self, url: str | None = None):
        self.url = url or settings.mcs_mcp_url
        self.workdir = settings.ess_maker_workdir / "agent-snapshot"
        self.workdir.mkdir(parents=True, exist_ok=True)

    # ---- discovery ----
    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        snap = self.workdir / f"{agent_id}.yaml"
        if snap.exists():
            return yaml.safe_load(snap.read_text(encoding="utf-8"))
        return {
            "id": agent_id,
            "displayName": "Employee Self-Service",
            "instruction": "You are the Employee Self-Service agent. Help with HR and IT tasks.",
            "topics": [],
            "knowledge": [],
            "_stub": True,
        }

    async def list_topics(self, agent_id: str) -> list[dict[str, Any]]:
        agent = await self.get_agent(agent_id)
        return agent.get("topics", [])

    async def get_topic(self, agent_id: str, topic_name: str) -> dict[str, Any]:
        for t in await self.list_topics(agent_id):
            if t.get("name") == topic_name:
                return t
        return {"name": topic_name, "description": "", "yaml": "", "_stub": True}

    async def list_knowledge(self, agent_id: str) -> list[dict[str, Any]]:
        agent = await self.get_agent(agent_id)
        return agent.get("knowledge", [])

    # ---- mutation (writes to local snapshot; real impl pushes via MCP) ----
    async def update_agent_instruction(self, agent_id: str, new_instruction: str) -> dict[str, Any]:
        agent = await self.get_agent(agent_id)
        agent["instruction"] = new_instruction
        self._save(agent_id, agent)
        return {"ok": True, "agent_id": agent_id, "stub": True}

    async def update_topic(self, agent_id: str, topic_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        agent = await self.get_agent(agent_id)
        topics = agent.setdefault("topics", [])
        for t in topics:
            if t.get("name") == topic_name:
                t.update(patch)
                break
        else:
            topics.append({"name": topic_name, **patch})
        self._save(agent_id, agent)
        return {"ok": True, "topic": topic_name, "stub": True}

    async def update_knowledge_description(
        self, agent_id: str, source_name: str, new_description: str
    ) -> dict[str, Any]:
        agent = await self.get_agent(agent_id)
        for k in agent.setdefault("knowledge", []):
            if k.get("name") == source_name:
                k["description"] = new_description
                break
        else:
            agent["knowledge"].append({"name": source_name, "description": new_description})
        self._save(agent_id, agent)
        return {"ok": True, "source": source_name, "stub": True}

    def _save(self, agent_id: str, agent: dict[str, Any]) -> None:
        path = self.workdir / f"{agent_id}.yaml"
        path.write_text(yaml.safe_dump(agent, sort_keys=False), encoding="utf-8")
