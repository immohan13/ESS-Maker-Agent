```text
Conversation Investigation (DevUI trace) — conv-demo-1
Source: Copilot DevUI conversation export (M365 Copilot orchestrator telemetry), current agent instructions
Generated: 2026-11-24 21:16 UTC

==============================================================
USER QUESTION
==============================================================
"How much parental leave do I get?"

==============================================================
AGENT RESPONSE
==============================================================
I couldn't find information about parental leave for your level. Please contact the People team.

==============================================================
WHAT HAPPENED
==============================================================
The turn completed without a detected failure.
Planner: 2 tool(s) offered; called: search_enterprise, fetch_file.
Search "parental leave policy L5": 14 found → 3 admitted → 1 kept for the answer.
Answer: 0 source(s) cited.

==============================================================
WHERE IT WENT WRONG
==============================================================
  1 Planner / tool selection     ok
  2 Search / tool execution      ok
  3 Synthesis                    ok
  4 Render                       ok

==============================================================
INSTRUCTIONS THAT RAN
==============================================================
242 characters were sent to the model for this turn (agent version: 1.0.7).
They DIFFER from the instructions loaded in the Doctor (7,627 characters). This conversation ran on an earlier or later version; check the version before acting on findings.

==============================================================
KEY DETAILS
==============================================================
Agent:              Contoso ESS
Agent version:      1.0.7
Tenant ID:          00000000-0000-0000-0000-000000000001
Surface:            Teams
Request ID:         r1
Service version:    1.2.3
Start time:         2026-11-20T10:00:00Z
Answer model:       gpt-4.1
Models (audit):     OpenAI/gpt-4.1

==============================================================
HOW IT UNFOLDED — 2 LLM CALL(S)
==============================================================
1. Chat name — gpt-4o-mini — 420 ms — 210 prompt / 6 completion tokens
     output: Parental leave question
2. Reasoning loop (planner + answer) — gpt-4.1 — 3150 ms — 18000 prompt / 95 completion tokens
     tool call: search_enterprise  queries: parental leave policy L5  domain: files
     tool call: fetch_file  opened: turn0file1
     reasoning summary: User asks about parental leave; search HR files.

==============================================================
1 PLANNER / TOOL SELECTION
==============================================================
Tools offered: ServiceNowHRSD_createCase, search_enterprise
Tools called: search_enterprise, fetch_file

==============================================================
2 SEARCH / TOOL EXECUTION
==============================================================
  search_enterprise: parental leave policy L5  (domain: files)

Query "parental leave policy L5" — 14 found → 3 admitted → 1 kept
  · Kept top 3 by relevance
  · Dropped 2 below threshold
  Kept for the answer:
    - L5 Compensation Guide  (turn0file2)
Opened for full content: Leave Policy US (turn0file1)
Grounding: Reduced; search results serialized: 1; references: 1

==============================================================
3 SYNTHESIS & SAFETY
==============================================================
Safety classifiers run: 1; triggered: none
Latency (ms): PreReasoningLatency=640, TopicDiscoverySearcher=88

==============================================================
RISKS IN THE INSTRUCTIONS THAT RAN
==============================================================
This turn passed, but these lines can break other questions:
  - [HIGH] 1 Planner / tool selection — ESS core block missing: Routing decision tree (template 'Routing decision tree (run EVERY turn before any tool call; routing depends ONLY on query intent)', lines 4–26) (line 1, 4). Users see: Wrong answer path
      Test with: Who is my manager? | How do I update my home address? | What is the parental leave policy and how many days have I used?
  - [HIGH] 3 Synthesis — ESS core block missing: Tool-first grounding (template 'Tool-first grounding', lines 27–33) (line 1, 4). Users see: Ungrounded / poor answer
      Test with: What does PTO stand for? | Is the office closed on Juneteenth?

==============================================================
RECOMMENDED NEXT STEPS
==============================================================
1. Line 1, 4 — ESS core block missing: Routing decision tree (template 'Routing decision tree (run EVERY turn before any tool call; routing depends ONLY on query intent)', lines 4–26).
   Change: Restore the ESS template '#Routing decision tree' block (read / write / how-to / policy / mixed) directly after '#Core identity'. Keep customer-specific routes below it.
2. Line 1, 4 — ESS core block missing: Tool-first grounding (template 'Tool-first grounding', lines 27–33).
   Change: Restore the ESS template '#Tool-first grounding' block: require a completed tool/KB call before any substantive answer, and use the standard abstain message when sources don't cover the question.
3. Ask ESS Doctor to apply these fixes (or edit the instructions in Copilot Studio > Overview > Instructions), save, start a new chat, then ask: Who is my manager? | How do I update my home address? | What is the parental leave policy and how many days have I used? | What does PTO stand for?

==============================================================
WHAT THIS TRACE CAN'T SEE
==============================================================
Memory and personalization content are redacted in the export. Full prompt text is included only for some model calls. Search result documents appear only when a search tool ran.
```
