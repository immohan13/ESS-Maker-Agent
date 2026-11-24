---
name: conversation-xray
description: Diagnose one ESS agent conversation from a DevUI export or developer mode card and recommend fixes.
---

# ESS conversation X-ray

You explain one conversation turn of an ESS agent, using the evidence the maker uploads.

## Evidence (best first)

1. **DevUI conversation export** (best for declarative agents): in Microsoft 365 Copilot open DevUI for the conversation, select **Download** and upload the `conversationHistory-<id>.json` file (often several MB; upload it as a file, don't paste it). It contains the instructions that ran, tools offered and called, every model call, the search funnel and safety classifiers.
2. **Developer mode card** (quickest, no admin needed): in a chat with the agent send `-developer on`, ask the failing question again, copy the debug card text, then send `-developer off`. Ask the maker for the question and the agent's answer too. The report diagnoses tool selection, each action's HTTP status and latency (401/403/404/429/5xx, timeouts), knowledge searches with zero results, and "no function selected".
3. **Purview audit export**: a compliance admin searches the Purview audit log for `CopilotInteraction` records for the agent and time window and exports CSV. Useful to see which agent version ran when.

Also ask for the agent's **current instructions** when available, so the report can say whether the conversation ran on a different version.

## Run

Save pasted evidence to a file first (for example `inputs/devmode.txt`).

```bash
python3 scripts/essdoctor.py xray inputs/conversationHistory.json --instructions inputs/current.txt
python3 scripts/essdoctor.py xray inputs/conversationHistory.json --question "relocation"   # pick a turn by question text
python3 scripts/essdoctor.py xray inputs/conversationHistory.json --turn 2                   # pick a turn by number
python3 scripts/essdoctor.py xray inputs/audit.csv --agent "Employee Self-Service"
python3 scripts/essdoctor.py xray inputs/devmode.txt --question "<employee question>" --response "<agent answer>" --instructions inputs/current.txt
```

The DevUI report lists all turns when an export has more than one; analyze the one the maker is asking about. For several conversations at once (for example "the agent says sorry too often"), use the `sorry-triage` skill instead.

## Respond

1. Show the report as returned. Both DevUI and developer mode reports use the same layout: user question, agent response, what happened, where it went wrong (stage table), instruction check, key details, planner/tool selection, search/tool execution, synthesis and safety, instruction lines likely responsible, recommended next steps, and what the evidence can't see; DevUI adds the instructions that ran and each model call. Don't drop sections or change verdicts.
2. Then summarize in 3–5 sentences: the stage that failed, the most likely cause, and the specific instruction line and fix.
3. Offer to apply the fix with the `instruction-check` skill (`fix`, then `check`), using the instructions that ran if the maker didn't upload newer ones.

## Rules

- DevUI exports contain employee questions, answers, tenant and user identifiers. Use them only to answer the maker; don't copy them into emails, chats or other documents unless asked, and don't keep them in memory.
- Don't infer backend behavior (ServiceNow, Workday) beyond what the evidence shows; the report lists what the trace can't see.
- If the evidence isn't recognised, say which of the three formats to provide.
