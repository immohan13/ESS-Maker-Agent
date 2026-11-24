---
name: eval-triage
description: Explain failed ESS evaluation questions and compare runs before and after a change.
---

# ESS evaluation triage

## Inputs

- One Copilot Studio evaluation results CSV (export it from the agent's **Evaluation** page), plus the agent instructions the run used; or
- Two results CSVs of the same questions: before and after an instruction change.

## Run

```bash
python3 scripts/essdoctor.py eval-triage inputs/results.csv --instructions inputs/current.txt
python3 scripts/essdoctor.py eval-compare inputs/before.csv inputs/after.csv
```

## Respond

1. Show the report as returned.
2. Summarize: how many questions failed, the top 2–3 causes, and which ones the maker can fix in instructions versus what needs a knowledge-source or connection owner.
3. For instruction causes, offer the `instruction-check` skill to fix them, then suggest re-running the same evaluation set and comparing with `eval-compare`.

## Rules

- Evaluation CSVs can contain employee-like questions and answers; keep them in this session.
- Don't mark a question as fixed until a new evaluation run shows it passing.
