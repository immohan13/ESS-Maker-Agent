# Reduce "sorry" answers with ESS Doctor

A two-week playbook for an ESS team whose agent too often answers with an apology, an error or no answer. It uses the ESS Doctor Cowork plugin and evidence makers can collect themselves.

## What you need

- Microsoft 365 Copilot with Cowork and usage-based billing for the makers doing the work.
- The ESS Doctor plugin added in Cowork (Plugins > Add > Upload, then share with the maker group), or deployed by an admin.
- The agent's current instructions (copy from Copilot Studio > Overview > Instructions).

## Week 1: find out why

1. **Pick 20–30 recent sorry conversations** from feedback (thumbs down, "no answer" themes) across the top question areas.
2. **Collect evidence for each**, one file per conversation:
   - DevUI export (`conversationHistory-<id>.json`) when available, or
   - a developer mode card: send `-developer on`, ask the same question, copy the card into a `.txt` with `Question:` and `Response:` lines at the top, send `-developer off`.
3. **Run the triage**: in Cowork upload the files (or one `.zip`) and the current instructions, and ask "Why does our ESS agent keep saying sorry, and what should we fix first?"
4. **Read the cause table.** Each sorry is assigned one cause and an owner:

   | Cause | Owner |
   | --- | --- |
   | Declined by an instruction rule; declined without searching; no topic matched; found content but still declined | Agent maker |
   | Knowledge search found nothing | Knowledge owner (maker if the query was enriched) |
   | Topic, action or connector failed | Connection or backend admin |
   | Safety filter blocked; throttled | Maker (moderation) or platform |

## Week 2: fix and measure

1. **Maker-owned causes**: ask ESS Doctor to fix the instruction rules the triage lists ("fix these rules and give me the file"), review the diff, paste the new instructions in Copilot Studio and publish.
2. **Content causes**: send the knowledge owner the questions and search queries that returned nothing.
3. **Connector causes**: send the connection admin the developer mode cards with the HTTP status.
4. **Measure**: ask the same questions again after publishing, collect new cards or exports, and run the triage again. Compare the sorry share and the cause table with week 1. Repeat for the next biggest cause.

## Tips

- Fix one cause at a time and re-test, so you know which change helped.
- Keep a fixed set of the same questions (including ones that should still be declined) so the before and after are comparable.
- Treat "declined by an instruction rule" as working as designed only when the rule is still wanted; otherwise narrow it.
- Evidence files contain employee questions; keep them in the Cowork task and delete them when done.

See [../samples/](../samples/) for example reports.
