"""Aggregate scored eval results into clusters + recommended actions via the analyzer prompt."""
from __future__ import annotations

import json
from collections import Counter

from ess_maker.eval.schemas import EvalReport
from ess_maker.llm.azure_openai import chat_json
from ess_maker.llm.prompts import load


def _metrics(report: EvalReport) -> dict:
    if not report.items:
        return {"pass_rate": 0.0, "groundedness_avg": 0.0, "relevance_avg": 0.0, "actionability_avg": 0.0}
    n = len(report.items)
    return {
        "pass_rate": round(report.pass_rate, 3),
        "groundedness_avg": round(sum(i.score.groundedness for i in report.items) / n, 2),
        "relevance_avg": round(sum(i.score.relevance for i in report.items) / n, 2),
        "actionability_avg": round(sum(i.score.actionability for i in report.items) / n, 2),
    }


def _failure_distribution(report: EvalReport) -> dict[str, int]:
    return dict(Counter(i.score.failure_mode for i in report.items if not i.score.passed))


async def analyze(report: EvalReport) -> dict:
    """Return analyzer JSON with clusters + recommended_actions."""
    user = json.dumps(
        {
            "metrics": _metrics(report),
            "failure_distribution": _failure_distribution(report),
            "items": [
                {
                    "scenario_id": i.scenario.id,
                    "category": i.scenario.category,
                    "prompt": i.scenario.prompt,
                    "expected_topic": i.scenario.expected_topic,
                    "expected_knowledge_source": i.scenario.expected_knowledge_source,
                    "agent_reply": i.result.raw_reply,
                    "triggered_topic": i.result.triggered_topic,
                    "score": i.score.model_dump(),
                }
                for i in report.items
            ],
        },
        ensure_ascii=False,
    )
    out = await chat_json(load("analyze_results"), user, temperature=0.1)
    out.setdefault("metrics", _metrics(report))
    return out
