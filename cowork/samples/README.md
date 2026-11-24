# ESS Doctor sample reports

Example outputs from the ESS Doctor Cowork plugin, generated from **synthetic Contoso data** (no customer content). Share these with makers so they know what ESS Doctor returns before they collect their own evidence.

| Sample | What the maker uploads | What the report tells them |
| --- | --- | --- |
| [devmode-xray-sample.md](devmode-xray-sample.md) | A developer mode card for one question that got "I couldn't find…" | Which stage failed (tool selection), why, which instruction lines or descriptions to fix, and what the card can't show |
| [devmode-xray-403-sample.md](devmode-xray-403-sample.md) | A developer mode card where creating an HR case returned "Sorry, something went wrong" | The ServiceNow action was refused (HTTP 403), so the fix belongs to the ServiceNow admin, not the instructions |
| [devui-xray-sample.md](devui-xray-sample.md) | A DevUI conversation export | The full investigation: the instructions that ran, every model call, the search funnel (found → admitted → kept), safety classifiers, the instruction lines behind the failure and numbered fixes |
| [sorry-triage-sample.md](sorry-triage-sample.md) | Several cards and exports where the agent said sorry | Causes of the sorry answers biggest first, who fixes each, the instruction rules behind the maker-owned ones, and every sorry turn with its evidence |
| [instruction-check-sample.md](instruction-check-sample.md) | The agent instructions | Findings by severity with line numbers, what employees see and the fix; ESS core blocks; authoring practices |

The inputs are in [inputs/](inputs/). Regenerate the samples after an engine change with the commands in [../README.md](../README.md).

## How makers collect evidence

### Developer mode card (fastest, any maker)

1. In Microsoft 365 Copilot or Teams, open a chat with the ESS agent.
2. Send `-developer on`.
3. Ask the question that went wrong. A debug card appears under the answer.
4. Copy the card text into a `.txt` file. Add two lines at the top:

   ```text
   Question: <what the employee asked>
   Response: <what the agent replied>
   ```

5. Send `-developer off`.
6. In Cowork, upload the file and ask "Why did my ESS agent say sorry here?"

The card shows which topics, actions and knowledge searches Copilot matched, selected and ran, with each action's HTTP status and latency and each search's result count. It doesn't show the instructions that ran or the answer-writing step; use a DevUI export for those.

### DevUI conversation export (most detail)

1. Open the conversation in DevUI and select **Download**.
2. Upload `conversationHistory-<id>.json` in Cowork (it's often several MB; upload it as a file).

### Many conversations ("our agent says sorry too often")

Collect 10–30 cards or exports of conversations that ended in sorry, upload them together (or as one `.zip`) with the current instructions, and ask "Why does our agent say sorry so often?" ESS Doctor runs the sorry triage and ranks the causes.

## Sorry causes ESS Doctor tells apart

| Cause | Typical evidence | Who fixes it |
| --- | --- | --- |
| Declined by an instruction rule | The question matches a restriction; no tool ran | Maker: narrow the rule |
| Declined without searching | Tools offered, none called, no rule asked for it | Maker: routing and grounding rules |
| No topic, action or knowledge matched | "No functions selected" on the card | Maker: topic and action descriptions |
| Knowledge search found nothing | Search ran, 0 results kept | Knowledge owner; maker if the query was enriched (job level, country) |
| Search found content but the agent still declined | Results kept, answer still abstained | Maker: abstain rules or query |
| Topic, action or connector failed | HTTP 401/403/404/429/5xx or timeout on the card | Connection or backend admin |
| Safety filter blocked the answer | Classifier triggered in DevUI | Maker: moderation and wording |
| Copilot throttled the request | Throttling on the card, nothing ran | Platform |
