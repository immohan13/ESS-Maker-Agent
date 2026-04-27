"""Typer CLI: run eval / analyze / iterate from the terminal."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table

from ess_maker.analyzers.result_analyzer import analyze
from ess_maker.eval.cs_client import ping as cs_ping
from ess_maker.eval.runner import load_report, load_scenarios, run_eval
from ess_maker.eval.seval_import import from_json_file, from_seval_api, write_scenarios_yaml
from ess_maker.eval.seval_scrape_import import attach_leo_metrics, from_scrape_dir
from ess_maker.mcp_server import iterate_once
from ess_maker.settings import settings

app = typer.Typer(add_completion=False, help="ESS Maker CLI")


@app.command("run-eval")
def run_eval_cmd(
    scenarios: Path = typer.Argument(..., exists=True, readable=True),
    agent_id: str = typer.Option(None, "--agent-id"),
    concurrency: int = typer.Option(3, "--concurrency"),
    no_judge: bool = typer.Option(False, "--no-judge", help="Skip LLM judge; use heuristic scoring."),
):
    """Run scenarios through the live ESS agent and score them."""
    report = asyncio.run(run_eval(
        load_scenarios(scenarios),
        agent_id=agent_id,
        concurrency=concurrency,
        use_judge=not no_judge,
    ))
    table = Table(title=f"Eval pass-rate: {report.pass_rate:.0%}  ({len(report.items)} scenarios)")
    table.add_column("ID")
    table.add_column("Pass")
    table.add_column("Failure")
    table.add_column("Notes", overflow="fold")
    for i in report.items:
        table.add_row(
            i.scenario.id,
            "PASS" if i.score.passed else "FAIL",
            i.score.failure_mode,
            i.score.notes[:120],
        )
    rprint(table)
    rprint(f"[dim]Saved -> {settings.ess_maker_workdir / 'eval-runs' / 'latest.json'}[/dim]")


@app.command("analyze")
def analyze_cmd(report_path: Path = typer.Argument(None)):
    """Analyze a saved report (defaults to latest)."""
    path = report_path or settings.ess_maker_workdir / "eval-runs" / "latest.json"
    out = asyncio.run(analyze(load_report(path)))
    rprint(json.dumps(out, indent=2))


@app.command("iterate")
def iterate_cmd(
    scenarios: Path = typer.Argument(..., exists=True, readable=True),
    agent_id: str = typer.Option(..., "--agent-id"),
):
    """Run scenarios + analyze + propose a new agent instruction in one shot."""
    out = asyncio.run(iterate_once(str(scenarios), agent_id))
    rprint(json.dumps(out, indent=2))


@app.command("serve-mcp")
def serve_mcp():
    """Start the ESS Maker MCP server (stdio)."""
    from ess_maker.mcp_server import run

    run()


@app.command("test-connection")
def test_connection_cmd():
    """Verify AAD auth + Copilot Studio endpoint by starting one conversation."""
    out = asyncio.run(cs_ping())
    rprint(json.dumps(out, indent=2))


@app.command("serve-ux")
def serve_ux(host: str = "127.0.0.1", port: int = 8765):
    """Run the ESS Maker web UX (FastAPI + simple HTML)."""
    import uvicorn
    uvicorn.run("ess_maker.webapp:app", host=host, port=port, reload=False)


@app.command("import-seval")
def import_seval_cmd(
    out: Path = typer.Option(Path("prompts/scenarios.seval.yaml"), "--out", "-o"),
    json_file: Path = typer.Option(None, "--json", help="Path to a SEVAL JSON export."),
    job_id: str = typer.Option(None, "--job-id", help="SEVAL job ID (e.g. 464263)."),
    cookie: str = typer.Option(None, "--cookie", help="Cookie header from a logged-in SEVAL session."),
):
    """Convert a SEVAL job's utterances + assertions into our scenarios YAML."""
    if json_file:
        scenarios = from_json_file(json_file)
    elif job_id and cookie:
        scenarios = from_seval_api(job_id, cookie)
    else:
        raise typer.BadParameter("Provide either --json <file> or both --job-id and --cookie.")
    path = write_scenarios_yaml(scenarios, out)
    rprint(f"[green]Imported {len(scenarios)} scenarios -> {path}[/green]")


@app.command("import-seval-scrape")
def import_seval_scrape_cmd(
    scrape_dir: Path = typer.Argument(..., exists=True, file_okay=False, readable=True),
    out: Path = typer.Option(Path("prompts/scenarios.seval.yaml"), "--out", "-o"),
    only_exp: str = typer.Option("control", "--only-exp", help="Filter exp_name (control|experiment|all)"),
    leo_csv: Path = typer.Option(None, "--leo-csv", help="Optional sbsleov3_details.csv to attach LEO scores."),
):
    """Convert a SEVAL 'Scrape Data & Conversation' export folder into a scenarios YAML."""
    scenarios = from_scrape_dir(
        scrape_dir,
        only_exp_name=None if only_exp.lower() == "all" else only_exp,
    )
    if leo_csv and leo_csv.exists():
        attach_leo_metrics(scenarios, leo_csv)
    path = write_scenarios_yaml(scenarios, out)
    rprint(f"[green]Imported {len(scenarios)} unique scenarios -> {path}[/green]")


if __name__ == "__main__":
    app()
