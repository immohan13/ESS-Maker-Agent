"""Known-issue catalog shared by the validator, the trace view, and the drafter.

`history` summarizes internal evidence gathered with WorkIQ (meetings, mail, incident notes) so the
UI can show "we've seen this before" next to a finding. Customer names are kept generic here.
"""
from __future__ import annotations

KNOWN_ISSUES: dict[str, dict] = {
    "KI-001": {
        "title": "Routing decision tree missing or removed",
        "symptom": "Personal-data questions answered from knowledge; write topics fire on read questions; how-to "
                   "never offers the action.",
        "history": "Customer CEA copies built on the older template have no intent routing; the DA core adds it. "
                   "Raised in the ESS CAB 'DA Core' thread as the main reason customer copies drift and become "
                   "hard for Microsoft to reproduce.",
    },
    "KI-002": {
        "title": "Grounding guard missing",
        "symptom": "Answers from model memory, or a generic 'sorry' instead of the standard abstain message.",
        "history": "Seen in SharePoint knowledge regressions where the root cause had to be confirmed before "
                   "editing instructions; tool-first grounding is part of the DA core.",
    },
    "KI-003": {"title": "No knowledge retry budget", "symptom": "Near-miss searches end in 'couldn't find it'.", "history": ""},
    "KI-004": {"title": "Rule contradicts its own examples", "symptom": "Non-deterministic query rewriting.", "history": ""},
    "KI-005": {
        "title": "Over-augmented search queries",
        "symptom": "Level/title appended to every query; knowledge misses for content not organized by level.",
        "history": "Observed in a financial-services HR agent where job level was injected into every search.",
    },
    "KI-006": {"title": "Typo or stray token in variable example", "symptom": "Polluted search strings.", "history": ""},
    "KI-008": {
        "title": "Over-broad pre-emptive sensitive route",
        "symptom": "Routine stress about leave/pay routed into the sensitive-topic flow.",
        "history": "",
    },
    "KI-010": {"title": "Example copied literally", "symptom": "Fabricated-looking citations.", "history": "Present in the ESS template itself."},
    "KI-011": {"title": "'Override everything' prohibitions", "symptom": "Over-refusal across supported scenarios.", "history": ""},
    "KI-012": {
        "title": "Over-scoped abstention",
        "symptom": "Global policies refused for non-HQ employees.",
        "history": "A bank customer reported irrelevant or missing citations and refusals; engineering needed "
                   "conversation IDs to separate retrieval, filtering, and synthesis failures.",
    },
    "KI-014": {"title": "Action offers suppressed", "symptom": "Create Case and update topics under-used after how-to answers.", "history": ""},
    "KI-015": {
        "title": "Unsafe default on missing profile data",
        "symptom": "Managers get employee-only answers when the profile lookup is empty.",
        "history": "IsManager incident: the managed Workday flow succeeded but 'Get my Worker profile' returned no "
                   "manager data, and blank Is_Manager became false.",
    },
    "KI-016": {"title": "Hard route excludes needed tools", "symptom": "Follow-ups needing personal data can't reach Workday/MCP.", "history": ""},
    "KI-018": {"title": "Over-mandated tool chain", "symptom": "Latency and tool budget spent on simple questions.", "history": ""},
    "KI-020": {
        "title": "Content filter on benign HR answers",
        "symptom": "User sees a generic apology; runtime shows ContentFilteredException during answer generation.",
        "history": "'How do I recognize coworkers?' was blocked mid-generation: 25 knowledge results passed "
                   "filtering, then the streaming answer hit RAI policy 198 with moderation set to High. An earlier "
                   "Kusto-only analysis reported Low; the config snapshot showed High — always reconcile with config.",
    },
    "KI-030": {
        "title": "Hidden instructions in variables",
        "symptom": "Behavior changes invisible in instruction diffs and length checks.",
        "history": "",
    },
    "KI-040": {
        "title": "Over the 8,000-character DA instruction limit",
        "symptom": "Trailing customer additions silently stop applying.",
        "history": "Microsoft guidance: don't offload instructions into knowledge documents; XPIA handling can "
                   "strip directive text at runtime.",
    },
}


def lookup(ki: str) -> dict:
    return KNOWN_ISSUES.get(ki, {})
