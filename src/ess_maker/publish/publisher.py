"""Selective publish flow.

The maker (or the ESS Maker agent) hands us a list of approved changes; we apply them via the
MCS MCP client and then call the Power Platform MCP client to publish to chosen channels.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from ess_maker.mcs.mcs_mcp_client import McsMcpClient
from ess_maker.mcs.pp_mcp_client import PowerPlatformMcpClient


ChangeKind = Literal["agent_instruction", "topic", "knowledge"]


class ApprovedChange(BaseModel):
    kind: ChangeKind
    name: str | None = None
    payload: dict[str, Any]


class PublishPlan(BaseModel):
    agent_id: str
    environment_id: str
    channels: list[str] = ["Teams"]
    changes: list[ApprovedChange] = []
    dry_run: bool = True


async def execute(plan: PublishPlan) -> dict[str, Any]:
    mcs = McsMcpClient()
    pp = PowerPlatformMcpClient()
    applied: list[dict[str, Any]] = []
    for ch in plan.changes:
        if plan.dry_run:
            applied.append({"kind": ch.kind, "name": ch.name, "dry_run": True})
            continue
        if ch.kind == "agent_instruction":
            r = await mcs.update_agent_instruction(plan.agent_id, ch.payload["new_instruction"])
        elif ch.kind == "topic":
            r = await mcs.update_topic(plan.agent_id, ch.name or "", ch.payload)
        elif ch.kind == "knowledge":
            r = await mcs.update_knowledge_description(
                plan.agent_id, ch.name or "", ch.payload["new_description"]
            )
        else:
            r = {"ok": False, "error": f"unknown change kind {ch.kind}"}
        applied.append({"kind": ch.kind, "name": ch.name, "result": r})
    publish = (
        {"dry_run": True}
        if plan.dry_run
        else await pp.publish_agent(plan.environment_id, plan.agent_id, plan.channels)
    )
    return {"applied": applied, "publish": publish}
