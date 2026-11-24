---
name: instruction-draft
description: Draft an ESS instruction change, such as a restriction, routing rule or format, and check side effects.
---

# ESS instruction draft

You turn a maker's intended change into instruction text that fits the ESS template, then check it didn't break anything else.

## Collect the change

Ask only for what's missing, in one message:

| Change type (`change_type`) | Details to collect (`intake` fields) |
| --- | --- |
| `terminology` | Pairs of the employee's word and the word the sources use: `terms` `[["PTO", "paid time off"]]` |
| `restricted` | `subject` to restrict; what is still `allowed`; optional `scope` (country, role) |
| `format` | `format_spec` (for example "bullets, max 5, end with a link"); optional `scope` |
| `routing` | `subject`, `source_name` (knowledge source), `mode` `only` or `exclude` |
| `escalation` | `scope` (which questions) and `route` (where to send employees) |
| `persona` | `intent` describing tone and voice |
| `disclaimer` | exact `disclaimer_text`; optional `disclaimer_style` (`bold`, `italic`); optional `scope` |

Also ask for 2–4 example questions that **should** change (`should_affect`) and 2–4 that **must not** (`must_not_affect`). Always put the maker's own words in `intent`.

Save the instructions to `inputs/current.txt` (or use the uploaded agent file), and the details to `inputs/intake.json`, for example:

```json
{ "change_type": "restricted", "intent": "Don't compare our salaries with other companies", "subject": "salary comparisons with other companies", "allowed": "our own pay bands and policies", "should_affect": ["How does my pay compare to Contoso?"], "must_not_affect": ["What is my pay band?"] }
```

## Run

```bash
python3 scripts/essdoctor.py draft inputs/current.txt --intake inputs/intake.json --out outputs/drafted_instructions.txt
python3 scripts/essdoctor.py check outputs/drafted_instructions.txt --previous inputs/current.txt
```

If the draft report says more detail is needed, ask the maker those questions and run it again.

## Respond

1. Show the draft report (added text, where it went, what changed) as returned.
2. Show the `check` report for the new version, highlighting "Findings introduced by this change". If the change introduced a high finding, say so plainly and propose a narrower wording, then re-run.
3. You may suggest clearer wording for the added clause, but keep it in the same section, and re-run `check` on any edited version before offering it.
4. Attach `outputs/drafted_instructions.txt` and list the example questions to test after publishing.

## Applying

The maker pastes the new instructions in Copilot Studio (**Overview > Instructions > Edit**), saves, publishes and tests in a new chat. You can't change the agent yourself.

## Rules

- Stay within 8,000 characters as Copilot Studio counts them; if the draft goes over, use the `instruction-check` skill's `condense` step.
- Don't remove or rewrite the ESS core blocks (routing, grounding, tool use) to make room for a change.
