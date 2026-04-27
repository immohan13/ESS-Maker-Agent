"""SEVAL-style runner: drive scenarios through the live ESS agent, then score with the judge."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import yaml

from ess_maker.eval.directline import DirectLineClient
from ess_maker.eval.cs_client import CopilotStudioClient
from ess_maker.eval.judge import score as judge_score
from ess_maker.eval.schemas import (
    AgentTurn,
    EvalReport,
    RunResult,
    Scenario,
    Score,
    ScoredResult,
)
from ess_maker.settings import settings


def load_scenarios(path: str | Path) -> list[Scenario]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    items = raw.get("scenarios", raw) if isinstance(raw, dict) else raw
    return [Scenario.model_validate(x) for x in items]


async def _run_one(client, scenario: Scenario) -> RunResult:
    started = time.monotonic()
    try:
        conv = await client.start()
        # Drain the greeting so it doesn't pollute the reply window.
        await client.poll_reply(conv, max_wait_s=5.0, quiet_window_s=1.0)
        await client.send(conv, scenario.prompt)
        turns, _ = await client.poll_reply(conv, max_wait_s=45.0, quiet_window_s=2.0)
        reply_text = "\n\n".join(t.text for t in turns if t.text).strip()
        return RunResult(
            scenario_id=scenario.id,
            prompt=scenario.prompt,
            transcript=[AgentTurn(role="user", text=scenario.prompt), *turns],
            raw_reply=reply_text,
            latency_ms=int((time.monotonic() - started) * 1000),
            conversation_id=getattr(conv, "conversation_id", None),
        )
    except Exception as e:  # surface as a scored failure rather than crashing the batch
        return RunResult(
            scenario_id=scenario.id,
            prompt=scenario.prompt,
            transcript=[AgentTurn(role="user", text=scenario.prompt)],
            raw_reply="",
            error=f"{type(e).__name__}: {e}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _select_client():
    """Pick the right transport based on what's configured in .env."""
    if settings.mcs_env_id and settings.mcs_bot_schema:
        return CopilotStudioClient()
    return DirectLineClient()


def _heuristic_score(scenario: Scenario, run: RunResult) -> Score:
    """Cheap, no-LLM signal: detects empty/error/refusal replies and rough relevance."""
    text = (run.raw_reply or "").strip().lower()
    if run.error:
        return Score(scenario_id=scenario.id, failure_mode="auth_or_connection_error", evidence=run.error)
    if not text:
        return Score(scenario_id=scenario.id, failure_mode="no_topic_triggered", evidence="empty reply")
    refusal_markers = ("i can't help", "i cannot help", "i don't have", "i'm not able", "sorry", "out of scope")
    if any(m in text for m in refusal_markers) and len(text) < 400:
        return Score(scenario_id=scenario.id, relevance=2, groundedness=2, actionability=2,
                     failure_mode="knowledge_miss", evidence=run.raw_reply[:160])
    # Rough relevance: token overlap between prompt nouns and reply.
    prompt_words = {w for w in scenario.prompt.lower().split() if len(w) > 4}
    overlap = sum(1 for w in prompt_words if w in text)
    relevance = 5 if overlap >= 3 else (4 if overlap == 2 else 3 if overlap == 1 else 2)
    grounded = 4 if ("http" in text or len(text) > 200) else 3
    actionable = 4 if any(c in run.raw_reply for c in ("\n-", "\n*", "\n1.", ":")) else 3
    return Score(scenario_id=scenario.id, relevance=relevance, groundedness=grounded,
                 actionability=actionable, failure_mode="none", notes="heuristic")


async def run_eval(
    scenarios: list[Scenario],
    *,
    agent_id: str | None = None,
    concurrency: int = 3,
    use_judge: bool = True,
) -> EvalReport:
    client = _select_client()
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(s: Scenario) -> ScoredResult:
        async with sem:
            run = await _run_one(client, s)
            if use_judge and settings.azure_openai_endpoint and settings.azure_openai_api_key:
                sc = await judge_score(s, run)
            else:
                sc = _heuristic_score(s, run)
            return ScoredResult(scenario=s, result=run, score=sc)

    items = await asyncio.gather(*(_bounded(s) for s in scenarios))
    report = EvalReport(agent_id=agent_id or settings.pp_bot_id or "unknown", items=list(items))
    _persist(report)
    aclose = getattr(client, "aclose", None)
    if aclose:
        await aclose()
    return report


def _persist(report: EvalReport) -> Path:
    out = settings.ess_maker_workdir / "eval-runs"
    out.mkdir(parents=True, exist_ok=True)
    fname = f"run-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
    path = out / fname
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out / "latest.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_report(path: str | Path) -> EvalReport:
    return EvalReport.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
