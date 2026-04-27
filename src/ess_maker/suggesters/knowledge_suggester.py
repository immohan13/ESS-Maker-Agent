"""Suggest improvements to a knowledge source description + content gaps."""
from __future__ import annotations

import json

from ess_maker.llm.azure_openai import chat_json
from ess_maker.llm.prompts import load


async def suggest_knowledge_description(
    *,
    source_name: str,
    source_type: str,
    current_description: str,
    sample_snippets: list[str],
    missed_prompts: list[str],
    wrongly_picked_prompts: list[str],
    sibling_sources: list[dict] | None = None,
) -> dict:
    user = json.dumps(
        {
            "source_name": source_name,
            "source_type": source_type,
            "current_description": current_description,
            "sample_snippets": sample_snippets,
            "missed_prompts": missed_prompts,
            "wrongly_picked_prompts": wrongly_picked_prompts,
            "sibling_sources": sibling_sources or [],
        },
        ensure_ascii=False,
    )
    return await chat_json(load("suggest_knowledge_desc"), user, temperature=0.2)
