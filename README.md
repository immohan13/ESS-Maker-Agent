# ESS Maker

> Auto-evaluates, analyzes, and tunes a Copilot Studio **Employee Self-Service** agent.
> Built to be operated by a Copilot Studio "ESS Maker" agent (no UI), with a CLI for power users.

## What it is

A Python package + MCP server + Copilot Studio agent definition. The flow:

```
maker scenarios.yaml
        │
        ▼
  ┌─────────────┐    DirectLine      ┌──────────────────────┐
  │ ESS Maker   │ ─────────────────▶ │  Target ESS agent    │
  │  (Copilot   │ ◀───────────────── │  (Copilot Studio)    │
  │   Studio    │                    └──────────────────────┘
  │   agent)    │
  └─────┬───────┘
        │ MCP tools (this repo)
        ▼
  run_scenarios → analyze_latest → suggest_{instruction|topic|knowledge}
              → patch_topic_file / patch_dataverse_text → publish
```

The ESS Maker **agent** sits in Copilot Studio and calls the **MCP server** in this repo for
every action. The maker chats with it in plain English; the agent runs the loop.

## Repo layout

| Path | What |
| --- | --- |
| [src/ess_maker/eval](src/ess_maker/eval) | DirectLine client, scenario runner, LLM-as-judge |
| [src/ess_maker/analyzers](src/ess_maker/analyzers) | Failure clustering + recommended actions |
| [src/ess_maker/suggesters](src/ess_maker/suggesters) | Topic / knowledge / instruction suggesters |
| [src/ess_maker/mcs](src/ess_maker/mcs) | MCS + Power Platform MCP client stubs, topic + Dataverse XML I/O |
| [src/ess_maker/publish](src/ess_maker/publish) | Selective publish flow |
| [src/ess_maker/mcp_server.py](src/ess_maker/mcp_server.py) | FastMCP server exposing all tools |
| [src/ess_maker/cli.py](src/ess_maker/cli.py) | `ess-maker` CLI |
| [config/agent.yaml](config/agent.yaml) | ESS Maker Copilot Studio agent spec |
| [config/mcp.json](config/mcp.json) | MCP server registration (for IDEs / agents) |
| [prompts/scenarios.example.yaml](prompts/scenarios.example.yaml) | Example employee scenarios |
| [samples/ess-agent](samples/ess-agent) | Vendored Microsoft sample (placeholder, see README) |

## Quick start

```pwsh
cd ess-maker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env   # then fill in AOAI + DirectLine secrets

# Smoke test (no network)
pytest -q

# Run scenarios against the live ESS agent
ess-maker run-eval prompts/scenarios.example.yaml --agent-id 88d09d7e-9372-45df-81ce-e60f21f5007e

# Cluster failures + recommended actions
ess-maker analyze

# One-shot iteration loop
ess-maker iterate prompts/scenarios.example.yaml --agent-id 88d09d7e-9372-45df-81ce-e60f21f5007e
```

## Wire the ESS Maker agent (Copilot Studio)

1. Run the MCP server locally or host it: `ess-maker serve-mcp`
2. In Copilot Studio, create a new agent from [config/agent.yaml](config/agent.yaml).
3. Add a **Custom MCP connector** pointing at your `ess-maker-mcp` endpoint.
4. Map each tool listed in `agent.yaml -> tools` to an action.
5. Publish to Teams / web. Talk to the agent: *"Run my scenarios and tell me what's broken."*

## Stubs that need real wiring later

| File | What to swap |
| --- | --- |
| [src/ess_maker/mcs/mcs_mcp_client.py](src/ess_maker/mcs/mcs_mcp_client.py) | Replace local YAML snapshot with real MCS MCP calls |
| [src/ess_maker/mcs/pp_mcp_client.py](src/ess_maker/mcs/pp_mcp_client.py) | Replace stub responses with Power Platform MCP calls |
| [samples/ess-agent](samples/ess-agent) | Clone the EmployeeSelfServiceAgent sample (instructions in folder README) |

## Failure modes the analyzer recognizes

`no_topic_triggered`, `wrong_topic_triggered`, `knowledge_miss`, `ambiguous_intent`,
`auth_or_connection_error`, `hallucination`, `missing_slot`, `other`.
Each is mapped to a primary fix target in
[src/ess_maker/analyzers/failure_classifier.py](src/ess_maker/analyzers/failure_classifier.py).

## License

Internal Microsoft project. See top-level repo for license terms.
