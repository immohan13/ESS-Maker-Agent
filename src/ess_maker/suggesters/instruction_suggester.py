"""Suggest a new agent (system) instruction grounded in the analyzer findings."""
from __future__ import annotations

import json

from ess_maker.llm.azure_openai import chat_json
from ess_maker.llm.prompts import load


async def suggest_agent_instruction(
    *,
    current_instruction: str,
    topics: list[dict],
    knowledge_sources: list[dict],
    failure_clusters: list[dict],
) -> dict:
    user = json.dumps(
        {
            "current_instruction": current_instruction,
            "topics": topics,
            "knowledge_sources": knowledge_sources,
            "failure_clusters": failure_clusters,
        },
        ensure_ascii=False,
    )
    return await chat_json(load("suggest_agent_instructions"), user, temperature=0.2)
