"""Suggest improvements to a Copilot Studio topic (description, triggers, nodes)."""
from __future__ import annotations

import json

from ess_maker.llm.azure_openai import chat_json
from ess_maker.llm.prompts import load


async def suggest_topic_patch(
    *,
    topic_yaml: str,
    topic_name: str,
    scenarios: list[dict],
    failure_evidence: list[dict],
) -> dict:
    user = json.dumps(
        {
            "topic_name": topic_name,
            "topic_yaml": topic_yaml,
            "scenarios": scenarios,
            "failure_evidence": failure_evidence,
        },
        ensure_ascii=False,
    )
    return await chat_json(load("suggest_topic_patch"), user, temperature=0.2)
