"""Heuristic mapping from failure_mode -> primary fix target."""
from __future__ import annotations

FIX_TARGET_BY_MODE = {
    "no_topic_triggered": "agent_instruction_or_topic_description",
    "wrong_topic_triggered": "topic_description",
    "knowledge_miss": "knowledge_description",
    "ambiguous_intent": "agent_instruction",
    "auth_or_connection_error": "tool_connection",
    "hallucination": "knowledge_description_or_grounding",
    "missing_slot": "topic_workflow",
    "other": "review_manually",
    "none": "none",
}


def primary_fix_for(failure_mode: str) -> str:
    return FIX_TARGET_BY_MODE.get(failure_mode, "review_manually")
