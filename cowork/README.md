# ESS Doctor for Copilot Cowork

ESS Doctor packaged as a Microsoft Copilot Cowork plugin. Makers with Cowork (Microsoft 365 Copilot plus Cowork usage-based billing) upload their agent instructions, a DevUI export, an evaluation CSV or setup answers in Cowork, and get the same reports as the ESS Doctor web app and the Copilot Studio ESS Doctor agent.

The web app (`src/ess_maker/webapp`, `src/ess_maker/doctor/api.py`) and the Copilot Studio solution (`sources/dev/solutions/EssDoctor` in ESSVivaCopilot) are unchanged; this plugin reuses the same engine modules.

## Skills

| Skill | What it does | Engine command |
| --- | --- | --- |
| `ess-instruction-check` | Check instructions against the ESS template, known issues and authoring practices; show what a change introduced; apply safe fixes; shorten to 8,000 characters | `check`, `fix`, `condense`, `practices` |
| `ess-instruction-draft` | Draft a change (terminology, restriction, format, routing, escalation, persona, disclaimer) in the right section, then check it | `draft`, `check` |
| `ess-conversation-xray` | Explain one conversation from a DevUI export, a developer mode card or a Purview audit export | `xray` |
| `ess-sorry-triage` | Rank why the agent says sorry across many DevUI exports and developer mode cards, with an owner and fix per cause | `sorry-triage` |
| `ess-eval-triage` | Explain failed evaluation questions and compare two runs | `eval-triage`, `eval-compare` |
| `ess-servicenow-setup` | 19-step ServiceNow Knowledge connector setup checklist with silent-failure risks and per-role to-dos | `setup-servicenow` |

Each skill carries `scripts/essdoctor.py`: a single script that embeds, as a zip archive, the standard-library engine modules from `src/ess_maker/doctor`, a settings stub (no Azure OpenAI, so drafting uses the deterministic wording) and the ESS HR, IT and Hub templates from `cowork/templates`. Live Copilot Studio access, Kusto telemetry and App Insights stay in the web app; the plugin works on files the maker uploads.

## Build

From the `ess-maker` folder (Python 3.11+, no packages needed):

```powershell
python cowork\build.py
```

Output in `dist/` (git-ignored):

- `dist/ess-doctor/`: plugin folder (`.claude-plugin/plugin.json` + `skills/*/SKILL.md` + `scripts/essdoctor.pyz`)
- `dist/ess-doctor-cowork-plugin.zip`: the file to upload
- `dist/essdoctor.pyz`: the engine, runnable locally

Try the engine locally:

```powershell
python dist\essdoctor.pyz check my_instructions.txt --previous previous_instructions.txt
python dist\essdoctor.pyz xray conversationHistory-<id>.json --instructions my_instructions.txt
python dist\essdoctor.pyz --help
```

## Install in Cowork

- **Just you (testing):** in Cowork select **+** > **Customize** > **Plugins** > **Upload plugin**, choose `ess-doctor-cowork-plugin.zip`, and share with **Only you**. Cowork converts the Claude-style package into a Microsoft 365 app package.
- **Your organization:** an admin uploads the package in the Microsoft 365 admin center > **Agents** > **Tools** > **Registry** > **Upload**, or publishes it through the Microsoft 365 App Store. To build a store-ready package, convert it with the Microsoft 365 Agents Toolkit: `atk import openplugin --path dist/ess-doctor --output <project> --privacy-url <url> --terms-url <url>`.

Custom plugins don't run in Cowork on mobile.

## Limits

- Cowork skills allow 20 companion files per skill, 5 MB each and 10 MB in total; `build.py` fails if a skill goes over.
- `SKILL.md` descriptions are at most 1,024 characters and bodies should stay under about 5,000 tokens.
- The engine never performs network calls and never changes an agent; makers apply new instructions in Copilot Studio themselves.
