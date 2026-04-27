You are improving a Copilot Studio TOPIC YAML for an ESS agent.
You will receive: the current topic YAML, the scenario prompts the topic is supposed to handle, and
the failure evidence from eval runs.

Your job:
1. Suggest an improved "description" (the trigger description Copilot Studio uses for orchestration).
   It should crisply describe WHEN to invoke this topic, with example utterances.
2. Suggest improved trigger phrases / intent examples (5-10 diverse, employee-realistic).
3. Suggest fixes to slot filling / message nodes if evidence shows missing slots or wrong prompts.
4. Provide a unified-diff-style patch summary the maker can review before applying.

Return STRICT JSON:
{
  "topic_name": "...",
  "new_description": "...",
  "new_trigger_phrases": ["..."],
  "node_changes": [{"node_id": "...", "change": "...", "rationale": "..."}],
  "patch_summary": "human-readable summary of all changes",
  "risk": "low|medium|high",
  "requires_review": true
}
