---
name: sorry-triage
description: Find why an ESS agent says sorry or gives no answer across many conversations, with owner and fix.
---

# ESS sorry triage

You help a team cut the share of "sorry" or no-answer replies from their ESS agent by finding which causes are behind most of them and who can fix each.

## Collect evidence

Ask for one file per failing conversation, uploaded together or as one `.zip`:

1. **DevUI conversation exports** (best): open DevUI on the conversation, select **Download**, upload the `conversationHistory-<id>.json`. Every turn in the export is checked.
2. **Developer mode cards**: in a chat with the agent send `-developer on`, ask the question that got a sorry, copy the debug card into a `.txt` file and send `-developer off`. Add two lines at the top of the file so the answer stage can be judged:

   ```text
   Question: <what the employee asked>
   Response: <what the agent replied>
   ```

Also ask for the agent's **current instructions** so the report can say which implicated rules are already fixed. 10–30 conversations give a reliable picture; a few are enough to start.

## Run

```bash
python3 scripts/essdoctor.py sorry-triage inputs/*.json inputs/*.txt --instructions inputs/current.txt
python3 scripts/essdoctor.py sorry-triage inputs/evidence.zip --instructions inputs/current.txt
```

## Respond

1. Show the report as returned: causes biggest first, the maker-owned share, the instruction rules behind them, every sorry turn, and next steps.
2. Summarize in 3–5 sentences: the share of sorries the maker can fix in instructions, the top cause, and the top rule to fix first.
3. Offer the next actions:
   - Fix the instruction rules with the `instruction-check` skill (`fix`, then `check`).
   - X-ray any single turn in depth with the `conversation-xray` skill.
   - Draft short messages to the knowledge owner (questions where search found nothing, with the queries) and to the connection admin (connector failures, with the HTTP status). Send only if the maker asks.

## Rules

- Exports and cards contain employee questions and identifiers; keep them in this session and don't copy them elsewhere unless asked.
- Don't promise a specific reduction; after fixes, ask the maker to re-run the same questions and triage the new exports to measure it.
