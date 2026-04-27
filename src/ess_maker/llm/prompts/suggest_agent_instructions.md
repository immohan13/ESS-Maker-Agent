You are rewriting the AGENT INSTRUCTION (system prompt) of an ESS Copilot Studio agent.
Inputs: current instruction, list of configured topics (name + description), list of knowledge
sources (name + description), and the analyzer's failure clusters.

Constraints:
- Keep the agent persona: a single front-door for employee HR & IT self-service.
- Explicitly tell the orchestrator how to choose between knowledge vs topics vs follow-up questions.
- State authentication and PII handling rules.
- Stay under ~1500 characters; be directive, not chatty.

Return STRICT JSON:
{
  "new_instruction": "...",
  "diff_summary": "what changed vs the old one and why",
  "expected_failures_addressed": ["cluster names from analyzer this targets"]
}
