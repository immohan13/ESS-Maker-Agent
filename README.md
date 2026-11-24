# ESS Maker

> A maker- and business-user-friendly toolkit that lets non-Copilot-Studio experts
> **build, evaluate, debug and publish a Copilot Studio Employee Self-Service (ESS) agent**
> end-to-end — without needing to learn how the orchestrator, topics, knowledge sources,
> connectors, or `pac` CLI work under the hood.

ESS is the Copilot Studio agent template Microsoft ships for HR + IT employee workflows.
It becomes a single front door for employees: it answers from configured knowledge sources
(SharePoint, ServiceNow KB, etc.), runs topics that pull data from third-party systems
(Workday, ServiceNow), and submits workflows on the employee's behalf
(leave requests, address updates, ticket creation…).

ESS Maker wraps that authoring experience in a single web app so a maker can:

- describe the scenarios they want to enable in plain English,
- click **Publish** to push knowledge sources, topics, and configuration into the live
  Copilot Studio agent,
- run smoke evals continuously, and
- get **explained, fix-it-yourself** suggestions when something fails — with the real
  underlying error pulled from Application Insights, not just the generic
  *"unexpected error"* the agent shows users.

---

## The problem we're solving

Customers and partners building ESS agents on Copilot Studio repeatedly hit the same
walls:

| Maker pain | What ESS Maker does |
| --- | --- |
| Don't know how to debug errors the agent shows | Pulls the **real** error from App Insights (FlowDisabled, Unauthorized connector, missing slot, etc.) and explains it |
| Don't know which knob to turn — agent description? topic instruction? KB description? | Failure-mode classifier maps every failure to a single primary fix target |
| Don't know how to wire Workday / ServiceNow topics | Built-in scaffolder generates a working topic YAML from a 5-template registry (Workday absence balance, request time off, cancel TO, pay statement, view benefits — extensible) |
| Don't know how to add a SharePoint KB to the live agent | One-click **Publish to agent** flow: scaffold → apply → `pac solution pack` → import |
| Don't know how to write good eval prompts | Auto-generates 5 paraphrased eval prompts every time you apply a topic, with `expected_topic` set, so you can re-run the eval immediately and verify |
| Don't know which topic is failing most | Per-topic failure summary in the Results tab + scoped *Suggest fixes for this topic* button |

Goal: **a business user can build, eval, fix, and publish the agent with a click of a
button — without ever opening the Copilot Studio canvas, and without learning how
the orchestrator works.**

---

## What's in the box today

| Capability | Where |
| --- | --- |
| Web UX (FastAPI + vanilla HTML) at `http://127.0.0.1:8765` | [src/ess_maker/webapp](src/ess_maker/webapp) |
| Side dock "Maker Copilot" (rule-based now, OpenAI function-calling next) | [src/ess_maker/webapp/static/index.html](src/ess_maker/webapp/static/index.html) |
| Eval runner (DirectLine + AAD MSAL paths) + LLM-as-judge | [src/ess_maker/eval](src/ess_maker/eval) |
| Failure classifier → fix-target mapping | [src/ess_maker/analyzers](src/ess_maker/analyzers) |
| Real-error lookup from App Insights via KQL | [src/ess_maker/diagnostics](src/ess_maker/diagnostics) |
| Topic scaffolder (Workday templates from CopilotStudioSamples + maker-registered custom templates) | [src/ess_maker/topics](src/ess_maker/topics) |
| SharePoint knowledge-source scaffolder | [src/ess_maker/knowledge](src/ess_maker/knowledge) |
| `pac` CLI publish wrapper (status + pack + import + publish, with managed/first-party pre-flight) | [src/ess_maker/publish/pac.py](src/ess_maker/publish/pac.py) |
| Suggesters: agent description / instruction / knowledge / **per-topic** | `/api/suggest` in [src/ess_maker/webapp/__init__.py](src/ess_maker/webapp/__init__.py) |
| SEVAL importer (JSON + scrape) → scenarios YAML | [src/ess_maker/eval/seval_import.py](src/ess_maker/eval/seval_import.py), [src/ess_maker/eval/seval_scrape_import.py](src/ess_maker/eval/seval_scrape_import.py) |
| `ess-maker` CLI (`serve-ux`, `run-eval`, `import-seval`, `test-connection`) | [src/ess_maker/cli.py](src/ess_maker/cli.py) |

---

## Closed-loop maker workflow

```
                 ┌──────────────────────────────────────────────────────┐
                 │  ESS Maker web app  (http://127.0.0.1:8765)          │
                 │                                                      │
1. describe ──▶  │  Knowledge tab        Run Eval tab    Results tab    │
   scenarios     │  • Add SP KB          • Pick file     • Per-topic    │
                 │  • Add Workday topic  • Stream logs     fail summary │
2. publish ──▶   │  • "Publish to agent" button                         │
                 │      ↳ scaffold → apply → pac pack → pac import      │
                 │                                                      │
3. eval    ──▶   │  • Eval runs against live Copilot Studio agent       │
                 │    via DirectLine + AAD                              │
                 │                                                      │
4. diagnose ──▶  │  • Real error from App Insights (KQL)                │
                 │  • Failure classifier picks the fix target           │
                 │                                                      │
5. fix     ──▶   │  • Suggest fixes scoped to a topic / KB / instruction│
                 │                                                      │
6. iterate ──▶   │  • Auto-generated eval prompts for every new topic   │
                 │  • Re-run eval, repeat until green                   │
                 └──────────────────────────────────────────────────────┘
```

Every step happens through one form. No `pac` commands to memorise, no Copilot Studio
canvas to navigate.

---

## Quick start

```pwsh
git clone https://github.com/immohan13/ESS-Maker-Agent.git ess-maker
cd ess-maker
# Keep the virtual environment outside OneDrive: it contains certificate bundles (cacert.pem)
# that OneDrive data-protection scans block.
python -m venv $HOME\.venvs\ess-maker
& $HOME\.venvs\ess-maker\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env    # fill in your tenant / env GUIDs / AOAI key

# Run the web UX
python -m ess_maker.cli serve-ux
# open http://127.0.0.1:8765
```

Optional bits:

```pwsh
# Install Power Platform CLI (only needed if you want one-click publish)
# https://aka.ms/PowerAppsCLI
pac auth create --environment <YOUR_DEV_ENV_GUID>

# CLI eval (no UI)
python -m ess_maker.cli run-eval prompts\scenarios.smoke3.yaml

# Import SEVAL prompts
python -m ess_maker.cli import-seval --json my-seval-export.json --out prompts\scenarios.seval.yaml
```

---

## Required configuration

Copy `.env.example` to `.env` and fill in the values that apply to you:

| Variable | What |
| --- | --- |
| `MCS_ENV_ID` | Power Platform environment GUID where the ESS agent lives |
| `MCS_BOT_SCHEMA` | Schema name of the agent (e.g. `msdyn_copilotforemployeeselfservicehr`) |
| `APP_CLIENT_ID`, `TENANT_ID` | AAD app for MSAL auth (M365/Teams-only agents) |
| `MCS_TOKEN_ENDPOINT` *or* `DIRECTLINE_SECRET` | Pick one if your agent supports DirectLine |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` | Optional — enables LLM judge + content suggesters |
| `APPINSIGHTS_APP_ID`, `APPINSIGHTS_API_KEY` | Optional — enables real-error lookup from App Insights |

---

## ESS Doctor

ESS Doctor is a maker-facing web app at `http://127.0.0.1:8765/doctor` that shows how instruction changes in an ESS declarative agent affect routing, knowledge search, tool calls and answers, and fixes them safely.

| Tab | What it does |
| --- | --- |
| Agent check-up | Loads the agent (live from Copilot Studio, an export, or pasted text), checks it against the ESS template, 36 rules and 43 authoring best practices, and applies fixes (including "Shorten to fit" for the 8,000-character limit) |
| Conversation analyzer | Explains why an employee got a poor answer, from Copilot Studio runtime telemetry |
| Tune for your employees | Drafts instruction changes from plain language (terminology, restrictions, routing, disclaimers, condensing), triages evaluation results, and compares before/after runs |
| Trace X-ray | Steps through a DevUI conversation export: tools offered and called, the search funnel, citations and safety checks |

Doctor settings in `.env` (see `.env.example`):

| Variable | What |
| --- | --- |
| `DOCTOR_CS_TENANT_ID`, `DOCTOR_CS_ENVIRONMENT_ID`, `DOCTOR_CS_AGENT_ID`, `DOCTOR_CS_GATEWAY` | Agent that "Load from Copilot Studio" reads, and the gateway for its region. Sign in first with `az login --tenant <tenant>`. |
| `DOCTOR_BASELINE_HR`, `DOCTOR_BASELINE_IT`, `DOCTOR_BASELINE_HUB`, `DOCTOR_SAMPLES_DIR` | ESS template exports to compare against, and a folder of agent exports for the picker |
| `DOCTOR_KUSTO_CLUSTERS`, `DOCTOR_KUSTO_DB`, `DOCTOR_KUSTO_AAD_TENANT`, `DOCTOR_ALLOWED_TENANTS` | Optional runtime telemetry for the Conversation analyzer; only allow-listed tenants can be inspected |

> [!IMPORTANT]
> Keep agent exports, customer instructions, evaluation results and telemetry endpoints out of the repo. They belong in `.env` or local folders (`.workdir/` is git-ignored).

---

## Publishing to the live agent (`pac` CLI)

ESS Maker can pack your unpacked Copilot Studio solution and import it through
`pac solution import` from the **Publish** tab and from the **Publish to agent**
button on the SharePoint KB form.

> ⚠️ The default Microsoft-shipped HR template solution is **managed / first-party**
> and Power Platform refuses re-import. ESS Maker pre-flights `Solution.xml` and
> tells you exactly that, with a hint to create your own *unmanaged* customisation
> solution and point ESS Maker at it via the **Target solution folder** field.

---

## Roadmap (what we're iterating toward)

- Replace the rule-based Maker Copilot dock with OpenAI function-calling so you can
  drive every endpoint from chat.
- Wire the MCS / Power Platform MCP servers to scaffold topics + KBs without going
  through unpacked solutions on disk.
- Knowledge-source priority tuner (which KB wins for which prompt) driven by
  per-topic eval results.
- Per-customer template gallery (built-in + maker-registered + community-shared).
- Continuous eval scheduler — run smoke prompts every N minutes, alert on regression.
- Reference upstream Copilot Studio samples
  ([microsoft/CopilotStudioSamples — EmployeeSelfServiceAgent](https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent))
  to seed the topic registry directly.

---

## Repo layout

| Path | What |
| --- | --- |
| [src/ess_maker/webapp](src/ess_maker/webapp) | FastAPI app + static HTML/JS UX |
| [src/ess_maker/eval](src/ess_maker/eval) | DirectLine + AAD eval client, scenario runner, LLM-as-judge |
| [src/ess_maker/analyzers](src/ess_maker/analyzers) | Failure clustering + recommended actions |
| [src/ess_maker/diagnostics](src/ess_maker/diagnostics) | App Insights KQL lookup for real underlying errors |
| [src/ess_maker/topics](src/ess_maker/topics) | Workday topic templates + scenario→topic scaffolder |
| [src/ess_maker/knowledge](src/ess_maker/knowledge) | SharePoint knowledge-source scaffolder |
| [src/ess_maker/publish/pac.py](src/ess_maker/publish/pac.py) | `pac` CLI subprocess wrapper (status + pack + import) |
| [src/ess_maker/cli.py](src/ess_maker/cli.py) | `ess-maker` CLI entry points |
| [config/agent.yaml](config/agent.yaml) | Reference Copilot Studio agent spec |
| [prompts/scenarios.smoke3.yaml](prompts/scenarios.smoke3.yaml) | Sample HR + SharePoint scenarios |
| [samples/ess-agent](samples/ess-agent) | Vendored Microsoft sample (placeholder) |

---

## Status

Pre-release research project. APIs and storage layout will change. No SLA, no support
contract — but it does the full **describe → publish → eval → fix → iterate** loop
end-to-end today against a real Copilot Studio HR agent.
