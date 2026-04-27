"""ESS Maker MCP server.

Exposes the full maker workflow as MCP tools so the Copilot Studio "ESS Maker" agent (or any
MCP-capable client) can drive it: run evals -> analyze -> suggest -> patch -> publish.

Run:
    uvx --from . ess-maker-mcp
or:
    python -m ess_maker.mcp_server
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from ess_maker.analyzers.failure_classifier import primary_fix_for
from ess_maker.analyzers.result_analyzer import analyze
from ess_maker.eval.runner import load_report, load_scenarios, run_eval
from ess_maker.eval.schemas import EvalReport
from ess_maker.eval.seval_import import from_json_file as seval_from_json
from ess_maker.eval.seval_import import from_seval_api as seval_from_api
from ess_maker.eval.seval_import import write_scenarios_yaml as seval_write
from ess_maker.mcs.dataverse_xml import get_descriptions, parse, set_text_at, write
from ess_maker.mcs.mcs_mcp_client import McsMcpClient
from ess_maker.mcs.pp_mcp_client import PowerPlatformMcpClient
from ess_maker.mcs.topics_io import apply_patch, read_topic, write_topic
from ess_maker.publish.publisher import ApprovedChange, PublishPlan, execute as publish_execute
from ess_maker.settings import settings
from ess_maker.suggesters.instruction_suggester import suggest_agent_instruction
from ess_maker.suggesters.knowledge_suggester import suggest_knowledge_description
from ess_maker.suggesters.topic_suggester import suggest_topic_patch

mcp = FastMCP("ess-maker")


# ---------------- Eval ----------------
@mcp.tool()
async def run_scenarios(scenarios_path: str, agent_id: str | None = None, concurrency: int = 3) -> dict:
    """Run a scenarios YAML file against the configured ESS agent (DirectLine) and score with LLM-as-judge."""
    scenarios = load_scenarios(scenarios_path)
    report = await run_eval(scenarios, agent_id=agent_id, concurrency=concurrency)
    return {
        "agent_id": report.agent_id,
        "pass_rate": report.pass_rate,
        "count": len(report.items),
        "report_path": str(settings.ess_maker_workdir / "eval-runs" / "latest.json"),
    }


@mcp.tool()
def import_seval_job(
    out_path: str = "prompts/scenarios.seval.yaml",
    json_file: str | None = None,
    job_id: str | None = None,
    cookie: str | None = None,
) -> dict:
    """Import scenarios from a SEVAL job. Provide either a downloaded `json_file` or `job_id`+`cookie`."""
    if json_file:
        scenarios = seval_from_json(json_file)
    elif job_id and cookie:
        scenarios = seval_from_api(job_id, cookie)
    else:
        return {"ok": False, "error": "Provide json_file OR (job_id and cookie)."}
    path = seval_write(scenarios, out_path)
    return {"ok": True, "count": len(scenarios), "path": str(path)}


@mcp.tool()
async def analyze_latest(report_path: str | None = None) -> dict:
    """Analyze a saved eval report and return failure clusters + recommended actions."""
    path = Path(report_path) if report_path else settings.ess_maker_workdir / "eval-runs" / "latest.json"
    report: EvalReport = load_report(path)
    return await analyze(report)


@mcp.tool()
def classify_failure(failure_mode: str) -> dict:
    """Map a failure_mode to its primary fix target."""
    return {"failure_mode": failure_mode, "primary_fix_target": primary_fix_for(failure_mode)}


# ---------------- Suggesters ----------------
@mcp.tool()
async def suggest_topic(topic_path: str, scenarios: list[dict], failure_evidence: list[dict]) -> dict:
    """Read a topic YAML from disk and propose description/trigger/node patches as JSON."""
    topic = read_topic(topic_path)
    name = topic.get("name") or Path(topic_path).stem
    return await suggest_topic_patch(
        topic_yaml=Path(topic_path).read_text(encoding="utf-8"),
        topic_name=name,
        scenarios=scenarios,
        failure_evidence=failure_evidence,
    )


@mcp.tool()
async def suggest_knowledge(
    source_name: str,
    source_type: str,
    current_description: str,
    sample_snippets: list[str],
    missed_prompts: list[str],
    wrongly_picked_prompts: list[str],
    sibling_sources: list[dict] | None = None,
) -> dict:
    """Propose a better description + example questions for a knowledge source."""
    return await suggest_knowledge_description(
        source_name=source_name,
        source_type=source_type,
        current_description=current_description,
        sample_snippets=sample_snippets,
        missed_prompts=missed_prompts,
        wrongly_picked_prompts=wrongly_picked_prompts,
        sibling_sources=sibling_sources,
    )


@mcp.tool()
async def suggest_instruction(
    agent_id: str, failure_clusters: list[dict]
) -> dict:
    """Propose a new agent instruction based on the current snapshot + failure clusters."""
    mcs = McsMcpClient()
    agent = await mcs.get_agent(agent_id)
    return await suggest_agent_instruction(
        current_instruction=agent.get("instruction", ""),
        topics=agent.get("topics", []),
        knowledge_sources=agent.get("knowledge", []),
        failure_clusters=failure_clusters,
    )


# ---------------- Topic / Dataverse I/O ----------------
@mcp.tool()
def patch_topic_file(topic_path: str, patch: dict) -> dict:
    """Apply a suggester JSON patch to a topic YAML on disk and return the new file path."""
    topic = read_topic(topic_path)
    apply_patch(topic, patch)
    out = write_topic(topic_path, topic)
    return {"ok": True, "path": str(out)}


@mcp.tool()
def read_dataverse_descriptions(xml_path: str) -> list[dict]:
    """List description-like elements from a Dataverse XML for review."""
    return get_descriptions(parse(xml_path))


@mcp.tool()
def patch_dataverse_text(xml_path: str, xpath: str, new_text: str) -> dict:
    """Set text at an xpath inside a Dataverse XML and save."""
    tree = parse(xml_path)
    ok = set_text_at(tree, xpath, new_text)
    if ok:
        write(tree, xml_path)
    return {"ok": ok, "xpath": xpath}


# ---------------- Agent snapshot via MCS MCP stub ----------------
@mcp.tool()
async def get_agent_snapshot(agent_id: str) -> dict:
    """Return the current agent definition (instruction, topics, knowledge) from the MCS MCP stub."""
    return await McsMcpClient().get_agent(agent_id)


@mcp.tool()
async def get_agent_logs(agent_id: str, top: int = 50) -> list[dict[str, Any]]:
    """Pull recent agent error/telemetry logs (Power Platform MCP stub)."""
    return await PowerPlatformMcpClient().get_logs(settings.pp_environment_id, agent_id, top=top)


# ---------------- Publish ----------------
@mcp.tool()
async def publish(
    agent_id: str,
    changes: list[dict],
    environment_id: str | None = None,
    channels: list[str] | None = None,
    dry_run: bool = True,
) -> dict:
    """Selectively apply approved changes and (optionally) publish the agent."""
    plan = PublishPlan(
        agent_id=agent_id,
        environment_id=environment_id or settings.pp_environment_id,
        channels=channels or ["Teams"],
        changes=[ApprovedChange.model_validate(c) for c in changes],
        dry_run=dry_run,
    )
    return await publish_execute(plan)


# ---------------- Convenience: end-to-end iteration loop ----------------
@mcp.tool()
async def iterate_once(scenarios_path: str, agent_id: str) -> dict:
    """One full loop: run scenarios -> analyze -> propose instruction update (no publish)."""
    scenarios = load_scenarios(scenarios_path)
    report = await run_eval(scenarios, agent_id=agent_id)
    analysis = await analyze(report)
    mcs = McsMcpClient()
    agent = await mcs.get_agent(agent_id)
    instruction_suggestion = await suggest_agent_instruction(
        current_instruction=agent.get("instruction", ""),
        topics=agent.get("topics", []),
        knowledge_sources=agent.get("knowledge", []),
        failure_clusters=analysis.get("failure_clusters", []),
    )
    return {
        "metrics": analysis.get("metrics"),
        "failure_clusters": analysis.get("failure_clusters", []),
        "recommended_actions": analysis.get("recommended_actions", []),
        "agent_instruction_suggestion": instruction_suggestion,
        "report_path": str(settings.ess_maker_workdir / "eval-runs" / "latest.json"),
    }


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
