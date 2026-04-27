"""Stub for the Power Platform MCP server (solution import/export, publish, env mgmt)."""
from __future__ import annotations

from typing import Any

from ess_maker.settings import settings


class PowerPlatformMcpClient:
    def __init__(self, url: str | None = None):
        self.url = url or settings.pp_mcp_url

    async def export_solution(self, environment_id: str, solution_name: str) -> dict[str, Any]:
        return {"ok": True, "stub": True, "path": f"./.workdir/{solution_name}.zip"}

    async def import_solution(self, environment_id: str, zip_path: str) -> dict[str, Any]:
        return {"ok": True, "stub": True, "imported": zip_path}

    async def publish_agent(self, environment_id: str, bot_id: str, channels: list[str]) -> dict[str, Any]:
        return {
            "ok": True,
            "stub": True,
            "environment_id": environment_id,
            "bot_id": bot_id,
            "channels": channels,
        }

    async def get_logs(self, environment_id: str, bot_id: str, *, top: int = 50) -> list[dict[str, Any]]:
        return [{"stub": True, "message": "wire pp-mcp to fetch real telemetry/error logs"}]
