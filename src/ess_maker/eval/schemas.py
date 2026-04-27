"""Pydantic schemas for SEVAL-style runs."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

FailureMode = Literal[
    "none",
    "no_topic_triggered",
    "wrong_topic_triggered",
    "knowledge_miss",
    "ambiguous_intent",
    "auth_or_connection_error",
    "hallucination",
    "missing_slot",
    "other",
]


class Scenario(BaseModel):
    id: str
    category: Literal["HR", "IT", "Mixed"] = "HR"
    prompt: str
    expected_topic: str | None = None
    expected_knowledge_source: str | None = None
    expected_assertions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class AgentTurn(BaseModel):
    role: Literal["user", "agent"]
    text: str
    activity_type: str | None = None
    citations: list[str] = Field(default_factory=list)
    triggered_topic: str | None = None


class RunResult(BaseModel):
    scenario_id: str
    prompt: str
    transcript: list[AgentTurn]
    raw_reply: str
    triggered_topic: str | None = None
    citations: list[str] = Field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0
    started_at: datetime = Field(default_factory=datetime.utcnow)
    conversation_id: str | None = None


class Score(BaseModel):
    scenario_id: str
    groundedness: int = 0
    relevance: int = 0
    actionability: int = 0
    failure_mode: FailureMode = "none"
    evidence: str = ""
    notes: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.failure_mode == "none"
            and self.relevance >= 3
            and self.groundedness >= 3
        )


class ScoredResult(BaseModel):
    scenario: Scenario
    result: RunResult
    score: Score


class EvalReport(BaseModel):
    agent_id: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    items: list[ScoredResult]

    @property
    def pass_rate(self) -> float:
        if not self.items:
            return 0.0
        return sum(1 for i in self.items if i.score.passed) / len(self.items)
