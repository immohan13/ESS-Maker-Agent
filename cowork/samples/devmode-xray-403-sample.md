```text
Conversation Investigation (developer mode card) — 11111111-2222-3333-4444-555555555555
Source: Microsoft 365 Copilot developer mode debug card
Generated: 2026-11-24 21:16 UTC

==============================================================
USER QUESTION
==============================================================
"Open an HR case about my payslip"

==============================================================
AGENT RESPONSE
==============================================================
Sorry, something went wrong. Please try again later.

==============================================================
WHAT HAPPENED
==============================================================
The ServiceNowHRSD.createCase action reached contoso.service-now.com and signed in, but ServiceNow refused the call (HTTP 403). The connection works; the backend doesn't allow this request for this user or app.
- Configured capabilities: OneDriveAndSharePoint.
- Matched functions: ServiceNowHRSD.createCase.
- Selected for execution: ServiceNowHRSD.createCase.
- ServiceNowHRSD.createCase ran: Failed, HTTP 403, 412 ms, POST contoso.service-now.com.

==============================================================
WHERE IT WENT WRONG
==============================================================
  1 Planner / tool selection     ok
  2 Search / tool execution      ◀ FAILED HERE
  3 Synthesis                    unknown
  4 Render                       affected (downstream)

==============================================================
KEY DETAILS
==============================================================
Agent ID:               T_contoso.ess.hr
Agent scope:            Tenant-scoped (org catalog)
Agent version:          1.0.7
Conversation ID:        11111111-2222-3333-4444-555555555555
Capabilities:           OneDriveAndSharePoint
Matched functions:      ServiceNowHRSD.createCase
Selected functions:     ServiceNowHRSD.createCase
Executed actions:       ServiceNowHRSD.createCase (Failed, HTTP 403, 412 ms, POST contoso.service-now.com)

==============================================================
1 PLANNER / TOOL SELECTION
==============================================================
Capabilities: OneDriveAndSharePoint
Actions: ServiceNowHRSD
Matched functions: ServiceNowHRSD.createCase
Selected for execution: ServiceNowHRSD.createCase

==============================================================
2 SEARCH / TOOL EXECUTION
==============================================================
  Action: ServiceNowHRSD.createCase — Failed, HTTP 403, 412 ms  POST https://contoso.service-now.com/api/sn_hr_core/case

==============================================================
3 SYNTHESIS & SAFETY
==============================================================
The card doesn't show safety classifiers or how the answer was written. The answer you added was checked for abstain wording.

==============================================================
INSTRUCTION LINES LIKELY RESPONSIBLE
==============================================================
None: the failure is in the connection or backend the action called, not in the agent's instructions.

==============================================================
RECOMMENDED NEXT STEPS
==============================================================
1. In ServiceNow, check the REST API access policies for this API. If a policy limits it to specific OAuth scopes, the OAuth application needs a custom OAuth scope that the policy allows, and the connection must request that scope.
2. Check the signed-in user has the ServiceNow roles this API needs.
3. Run the same request as the same user outside Copilot to confirm it's a backend permission and not the agent.
4. For the instructions that ran, every model call and the search funnel, export the same conversation from DevUI.

==============================================================
WHAT THIS CARD CAN'T SEE
==============================================================
- The answer text, and how the model used what came back. Pass the agent's response to check that step.
- Why the planner picked or skipped each function; the card shows only the outcome.
- The content of search results and which documents were cited.
- Steps inside Copilot Studio topics or flows after the action was called.
```
