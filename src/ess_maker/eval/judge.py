"""LLM-as-judge scoring."""
from __future__ import annotations

import json

from ess_maker.eval.schemas import RunResult, Scenario, Score
from ess_maker.llm.azure_openai import chat_json
from ess_maker.llm.prompts import load
from ess_maker.settings import settings


async def score(scenario: Scenario, result: RunResult) -> Score:
    if result.error:
        return Score(
            scenario_id=scenario.id,
            failure_mode="auth_or_connection_error",
            notes=f"Run error: {result.error}",
        )
    user = json.dumps(
        {
            "scenario": scenario.model_dump(),
            "agent_reply": result.raw_reply,
            "triggered_topic": result.triggered_topic,
            "citations": result.citations,
        },
        ensure_ascii=False,
    )
    data = await chat_json(load("judge"), user, deployment=settings.azure_openai_judge_deployment)
    return Score(
        scenario_id=scenario.id,
        groundedness=int(data.get("groundedness", 0)),
        relevance=int(data.get("relevance", 0)),
        actionability=int(data.get("actionability", 0)),
        failure_mode=data.get("failure_mode", "other"),
        evidence=str(data.get("evidence", "")),
        notes=str(data.get("notes", "")),
    )
