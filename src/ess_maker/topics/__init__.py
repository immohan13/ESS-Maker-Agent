"""Scenario → topic scaffolder.

Maps a maker-supplied scenario (free-text prompt or short label) to a
Workday / ServiceNow topic template based on the official samples at
https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent

The scaffolder is intentionally NOT auto-invoked. The maker calls it
explicitly (UI button or via the Maker Copilot side dock) for each scenario
they want to wire up. Output is always written to a *staging* directory
under .workdir/proposed-topics/<topic_id>/ so nothing is silently committed
to the live solution.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ess_maker.settings import settings


# ---------- Template registry ----------------------------------------------

@dataclass
class TopicTemplate:
    key: str
    name: str
    system: str           # 'workday' | 'servicenow' | 'graph' | 'generic'
    sample_url: str       # link to the canonical sample for the maker to read
    schema_name: str      # msdyn_<TopicName> — the Dataverse schema name
    description: str
    trigger_keywords: list[str] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)   # {name, entity, description}
    flow_template: str | None = None  # path under .workdir or None for "no flow yet"
    notes: str = ""
    # Workday integration: which existing scenario to call via the shared
    # WorkdaySystemGetCommonExecution dialog (mirrors live bot pattern).
    # Examples: msdyn_HRWorkdayHCMEmployeeGetVacationBalance,
    #           msdyn_HRWorkdayHCMEmployeeGetLanguageInformation,
    #           msdyn_HRWorkdayAbsenceEnterTimeOff_MultiDay
    workday_scenario: str = ""
    # Extra params (besides Employee_ID + As_Of_Effective_Date) to pass.
    workday_extra_params: list[dict] = field(default_factory=list)
    # Output kind for ParseValue (when the response is parsed into typed fields).
    # Each entry: {"name": str, "type": "String"|"Table"|"Record", "fields": [...]}
    workday_response_fields: list[dict] = field(default_factory=list)


# Canonical Workday samples from the public CopilotStudioSamples repo.
# We list them here so the maker can pick (or so the Maker Copilot can match
# a free-text scenario to one). Adding a new sample = one entry here.
WORKDAY_TEMPLATES: list[TopicTemplate] = [
    TopicTemplate(
        key="workday.absence.balance",
        name="Workday – Check absence balance",
        system="workday",
        sample_url="https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent/Workday/CheckAbsenceBalance",
        schema_name="msdyn_HRWorkdayAbsenceCheckBalance",
        description="Returns the current employee's leave / PTO / sick balance from Workday Absence_Management.",
        trigger_keywords=[
            "leave balance", "pto balance", "vacation balance", "time off balance",
            "how much pto", "remaining vacation", "sick leave balance", "absence balance",
        ],
        inputs=[
            {"name": "EmployeeName", "entity": "PersonNamePrebuiltEntity",
             "description": "The employee whose balance to fetch (defaults to caller)."},
            {"name": "AsOfEffectiveDate", "entity": "DateTimePrebuiltEntity",
             "description": "Effective date for balance lookup. Defaults to Today()."},
        ],
        # Reuse the live bot's pattern: BeginDialog → WorkdaySystemGetCommonExecution
        # with this scenario name. The shared dialog already owns the connector,
        # auth, retries and OnError redirect — we just wire the scenarioName.
        workday_scenario="msdyn_HRWorkdayHCMEmployeeGetVacationBalance",
        workday_response_fields=[
            {"name": "PlanDescriptor", "type": "Table", "value_field": "Value"},
            {"name": "PlanID", "type": "Table", "value_field": "Value"},
            {"name": "RemainingBalance", "type": "Table", "value_field": "Value"},
            {"name": "UnitOfTime", "type": "Table", "value_field": "Value"},
        ],
        notes="Calls the existing `msdyn_HRWorkdayHCMEmployeeGetVacationBalance` scenario via the shared `WorkdaySystemGetCommonExecution` topic. No new flow or connection reference required — reuses the same plumbing as live Workday topics on this bot.",
    ),
    TopicTemplate(
        key="workday.absence.request",
        name="Workday – Request time off",
        system="workday",
        sample_url="https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent/Workday/RequestTimeOff",
        schema_name="msdyn_HRWorkdayAbsenceEnterTimeOff",
        description="Submits a time-off request (single or multi-day) to Workday Absence_Management.",
        trigger_keywords=[
            "request time off", "submit pto", "vacation request", "request vacation",
            "time off from", "i need time off", "request sick leave", "submit time off",
        ],
        inputs=[
            {"name": "InputStartDate", "entity": "DateTimePrebuiltEntity", "description": "Start date."},
            {"name": "InputEndDate", "entity": "DateTimePrebuiltEntity", "description": "End date."},
            {"name": "InputHoursPerDay", "entity": "NumberPrebuiltEntity", "description": "Hours per day."},
            {"name": "InputTimeOffType", "entity": "StringPrebuiltEntity", "description": "Vacation, sick, PTO, etc."},
            {"name": "InputLeaveReason", "entity": "StringPrebuiltEntity", "description": "Reason / comment."},
        ],
        flow_template="reference:WorkdayEmployeeRequestTimeOff/msdyn_HRWorkdayAbsenceEnterTimeOff_MultiDay.xml",
        workday_scenario="msdyn_HRWorkdayAbsenceEnterTimeOff_MultiDay",
        workday_extra_params=[
            {"key": "{Start_Date}", "value_expr": "Text(Topic.InputStartDate, \"yyyy-MM-dd\")"},
            {"key": "{End_Date}", "value_expr": "Text(Topic.InputEndDate, \"yyyy-MM-dd\")"},
            {"key": "{Hours_Per_Day}", "value_expr": "Text(Topic.InputHoursPerDay)"},
            {"key": "{Time_Off_Type}", "value_expr": "Topic.InputTimeOffType"},
            {"key": "{Comment}", "value_expr": "Topic.InputLeaveReason"},
        ],
        notes="Calls `msdyn_HRWorkdayAbsenceEnterTimeOff_MultiDay` via the shared dialog. Mirrors `WorkdayEmployeeRequestTimeOff/topic.yaml` in this repo.",
    ),
    TopicTemplate(
        key="workday.absence.cancel",
        name="Workday – Cancel time off",
        system="workday",
        sample_url="https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent/Workday/CancelTimeOff",
        schema_name="msdyn_HRWorkdayAbsenceCancel",
        description="Cancels a previously submitted time-off request.",
        trigger_keywords=["cancel time off", "cancel vacation", "cancel pto", "cancel my leave"],
        inputs=[
            {"name": "RequestId", "entity": "StringPrebuiltEntity", "description": "The Workday absence event ID to cancel."},
        ],
        workday_scenario="msdyn_HRWorkdayAbsenceCancelTimeOff",
        workday_extra_params=[
            {"key": "{Absence_Event_ID}", "value_expr": "Topic.RequestId"},
        ],
    ),
    TopicTemplate(
        key="workday.pay.statement",
        name="Workday – View pay statement",
        system="workday",
        sample_url="https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent/Workday/ViewPayStatement",
        schema_name="msdyn_HRWorkdayPayStatement",
        description="Returns the employee's most recent pay statement summary and a deep link to Workday.",
        trigger_keywords=["pay statement", "paystub", "pay stub", "my paycheck", "last paycheck", "salary statement"],
        inputs=[
            {"name": "PeriodStart", "entity": "DateTimePrebuiltEntity", "description": "Period start (optional)."},
        ],
        workday_scenario="msdyn_HRWorkdayHCMEmployeeGetPayStatement",
    ),
    TopicTemplate(
        key="workday.profile.benefits",
        name="Workday – View benefits",
        system="workday",
        sample_url="https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent/Workday/ViewBenefits",
        schema_name="msdyn_HRWorkdayBenefits",
        description="Lists the employee's current benefit elections (medical, dental, retirement).",
        trigger_keywords=["my benefits", "health insurance plan", "dental coverage", "retirement plan", "401k", "view benefits"],
        inputs=[],
        workday_scenario="msdyn_HRWorkdayHCMEmployeeGetBenefits",
    ),
]


def list_templates() -> list[dict]:
    out = [asdict(t) for t in WORKDAY_TEMPLATES]
    out.extend(_load_custom_templates_serialized())
    return out


# ---------- Custom (maker-registered) templates ----------------------------
#
# Persisted to .workdir/ux/custom-templates.json so makers can extend the
# registry without code changes or a server restart.

def _custom_path() -> Path:
    p = settings.ess_maker_workdir / "ux"
    p.mkdir(parents=True, exist_ok=True)
    return p / "custom-templates.json"


def _load_custom_raw() -> list[dict]:
    p = _custom_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _load_custom_templates() -> list[TopicTemplate]:
    out = []
    for d in _load_custom_raw():
        try:
            out.append(TopicTemplate(
                key=d["key"],
                name=d["name"],
                system=d.get("system", "generic"),
                sample_url=d.get("sample_url", ""),
                schema_name=d.get("schema_name") or f"msdyn_{_slug(d['key'])}",
                description=d.get("description", ""),
                trigger_keywords=d.get("trigger_keywords", []),
                inputs=d.get("inputs", []),
                flow_template=d.get("flow_template"),
                notes=d.get("notes", ""),
                workday_scenario=d.get("workday_scenario", ""),
                workday_extra_params=d.get("workday_extra_params", []),
                workday_response_fields=d.get("workday_response_fields", []),
            ))
        except KeyError:
            continue
    return out


def _load_custom_templates_serialized() -> list[dict]:
    return [asdict(t) for t in _load_custom_templates()]


def _all_templates() -> list[TopicTemplate]:
    return WORKDAY_TEMPLATES + _load_custom_templates()


def register_custom_template(payload: dict) -> dict:
    """Append (or upsert by key) a maker-supplied template."""
    if not payload.get("key") or not payload.get("name"):
        return {"ok": False, "error": "key and name are required"}
    payload.setdefault("trigger_keywords", [])
    if isinstance(payload["trigger_keywords"], str):
        payload["trigger_keywords"] = [
            s.strip() for s in payload["trigger_keywords"].split(",") if s.strip()
        ]
    if not payload["trigger_keywords"]:
        return {"ok": False, "error": "at least one trigger keyword is required"}

    raw = _load_custom_raw()
    raw = [r for r in raw if r.get("key") != payload["key"]]
    raw.append(payload)
    _custom_path().write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return {"ok": True, "template": payload, "total_custom": len(raw)}


def delete_custom_template(key: str) -> dict:
    raw = _load_custom_raw()
    new = [r for r in raw if r.get("key") != key]
    if len(new) == len(raw):
        return {"ok": False, "error": f"no custom template with key {key}"}
    _custom_path().write_text(json.dumps(new, indent=2), encoding="utf-8")
    return {"ok": True, "removed": key}


# ---------- Matching --------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _norm(s: str) -> str:
    return " ".join(_WORD_RE.findall(s.lower()))


def match_template(prompt: str) -> tuple[TopicTemplate | None, float, list[tuple[str, float]]]:
    """Return (best_template, score, ranked_candidates).

    Pure keyword overlap — deterministic, no LLM needed. Score is the count
    of matched keyword phrases divided by 3 (capped at 1.0).
    """
    p = _norm(prompt)
    ranked: list[tuple[TopicTemplate, float]] = []
    for t in _all_templates():
        hits = sum(1 for kw in t.trigger_keywords if _norm(kw) in p)
        if hits == 0:
            continue
        score = min(1.0, hits / 3.0)
        ranked.append((t, score))
    ranked.sort(key=lambda x: x[1], reverse=True)
    best = ranked[0][0] if ranked else None
    best_score = ranked[0][1] if ranked else 0.0
    return best, best_score, [(t.key, s) for t, s in ranked]


# ---------- Generation ------------------------------------------------------

def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:48] or uuid.uuid4().hex[:8]


def render_topic_yaml(template: TopicTemplate, scenario_prompt: str, display_name: str) -> str:
    """Produce a Copilot Studio topic YAML for the given template + scenario.

    For Workday topics, mirrors the structure of live HR-bot topics like
    `Workday Get LanguageInformation` and the in-repo
    `WorkdayEmployeeRequestTimeOff/topic.yaml`:

      • `intent: {}` + a strong `modelDescription` (the orchestrator's signal)
      • Example trigger phrases live INSIDE modelDescription (Copilot Studio's
        actual `triggerPhrases:` schema differs across versions; embedding the
        phrases inside the description is the safe path that always works).
      • Action chain reuses the shared `WorkdaySystemGetCommonExecution`
        dialog with the canonical `scenarioName` for this operation. No new
        flow, no `<FLOW_ID>` placeholder, no `<CONNECTION_REF>` to fill in.
    """
    inputs_yaml = []
    for i in template.inputs:
        inputs_yaml.append(
            f"  - kind: AutomaticTaskInput\n"
            f"    propertyName: {i['name']}\n"
            f"    description: {i['description']}\n"
            f"    entity: {i['entity']}\n"
            f"    shouldPromptUser: false"
        )
    inputs_block = "\n\n".join(inputs_yaml) if inputs_yaml else "  []"

    example_prompts = "\n  ".join(f'"{kw}"' for kw in template.trigger_keywords[:8])
    action_block = _render_action_for(template)

    return f"""kind: AdaptiveDialog
# Generated by ESS Maker from scenario:
#   "{scenario_prompt}"
# Source template: {template.key}  ({template.sample_url})
# Display name:    {display_name}
# Schema name:     {template.schema_name}

inputs:
{inputs_block}

modelDescription: |-
  {template.description}

  Maker scenario this was created for:
  "{scenario_prompt}"

  Only respond when the user is asking about their own data. Do not respond
  to questions about other people's data.

  Example prompts:
  {example_prompts}

beginDialog:
  kind: OnRecognizedIntent
  id: main
  intent: {{}}
  actions:
{action_block}
"""


def _render_action_for(template: TopicTemplate) -> str:
    """Render the action chain.

    Workday topics on this bot DO NOT call `InvokeFlowAction` directly. They
    `BeginDialog` into the shared `WorkdaySystemGetCommonExecution` topic which
    owns the connector, OAuth, retry and OnError redirect. We only need to
    pass `parameters` (a JSON string of {key,value} pairs) and `scenarioName`.

    Pattern verified against live HR-bot topics (Get LanguageInformation,
    Get CompensationRatio, Get Certifications, Get Vacation Balance) on
    2026-04-26.
    """
    if template.system != "workday":
        return (
            "    - kind: SendActivity\n"
            "      id: not_implemented\n"
            "      activity: \"This topic is a stub. Add the action nodes for {0}.\"\n".format(template.key)
        )

    # Topic must be on a bot whose schemaname has the topic-namespace prefix.
    # We can't always know the live bot schema at scaffold time, so we point
    # at the HR bot's known shared dialog by default and let the user override.
    bot_namespace = settings.mcs_bot_schema or "msdyn_copilotforemployeeselfservicehr"
    common_dialog = f"{bot_namespace}.topic.WorkdaySystemGetCommonExecution"

    # Build the parameters JSON. Always include Employee_ID + As_Of_Effective_Date
    # (these mirror the live topics) plus any extras declared on the template.
    params: list[tuple[str, str]] = [
        ("{Employee_ID}", "Global.ESS_UserContext_Employee_Id"),
        ("{As_Of_Effective_Date}", "Text(Today(), \"yyyy-MM-dd\")"),
    ]
    for p in template.workday_extra_params or []:
        params.append((p["key"], p["value_expr"]))

    # Power Fx string concat for the JSON payload.
    json_pairs = []
    for key, expr in params:
        # Each becomes:  {""key"":""<key>"",""value"":""" & <expr> & """}
        # When the expr is already a literal call to Text(...) we still wrap.
        json_pairs.append(f"{{\"\"key\"\":\"\"{key}\"\",\"\"value\"\":\"\"\" & {expr} & \"\"\"}}")
    params_value = "=\"{\"\"params\"\":[" + ",".join(json_pairs) + "]}\""

    scenario_name = template.workday_scenario or template.schema_name

    # Optional ParseValue block for typed response fields.
    parse_block = ""
    if template.workday_response_fields:
        props_lines = []
        for f in template.workday_response_fields:
            if f.get("type") == "Table":
                props_lines.append(
                    f"          {f['name']}:\n"
                    f"            type:\n"
                    f"              kind: Table\n"
                    f"              properties:\n"
                    f"                {f.get('value_field', 'Value')}: String"
                )
            else:
                props_lines.append(f"          {f['name']}: String")
        parse_block = (
            "\n    - kind: ParseValue\n"
            "      id: parseWorkdayResponse\n"
            "      displayName: Parse Workday response\n"
            "      variable: Topic.parsedResponse\n"
            "      valueType:\n"
            "        kind: Record\n"
            "        properties:\n"
            + "\n".join(props_lines) + "\n"
            "      value: =Topic.workdayResponse\n"
        )

    return f"""    # Reuse the shared Workday execution dialog. This pattern matches
    # every other Workday topic on this bot (e.g. "Workday Get LanguageInformation",
    # "Workday Get CompensationRatio") — the shared dialog owns the connector,
    # the auth, the retry, and the OnError redirect. We only supply:
    #   • parameters  — the JSON payload (Employee_ID, As_Of_Effective_Date, extras)
    #   • scenarioName — the canonical Workday operation key
    - kind: BeginDialog
      id: invokeWorkday
      displayName: Call Workday — {scenario_name}
      input:
        binding:
          parameters: {params_value}
          scenarioName: {scenario_name}

      dialog: {common_dialog}
      output:
        binding:
          errorResponse: Topic.errorResponse
          isSuccess: Topic.isSuccess
          workdayResponse: Topic.workdayResponse
{parse_block}"""


# ---------- Public API ------------------------------------------------------

def staging_dir() -> Path:
    p = settings.ess_maker_workdir / "proposed-topics"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class ScaffoldResult:
    ok: bool
    template_key: str | None
    template_name: str | None
    sample_url: str | None
    confidence: float
    candidates: list[dict]
    topic_id: str | None
    schema_name: str | None
    proposed_dir: str | None
    files: list[dict]      # [{name, path, language, contents}]
    notes: list[str]
    error: str | None = None


def scaffold_from_scenario(prompt: str, display_name: str | None = None,
                           force_template: str | None = None) -> ScaffoldResult:
    """Match a scenario prompt to a template and write a staged topic to disk.

    Nothing is published or imported — the maker reviews the staged files first.
    """
    if force_template:
        chosen = next((t for t in _all_templates() if t.key == force_template), None)
        confidence = 1.0 if chosen else 0.0
        ranked = [(force_template, confidence)] if chosen else []
    else:
        chosen, confidence, ranked = match_template(prompt)

    if chosen is None:
        return ScaffoldResult(
            ok=False,
            template_key=None,
            template_name=None,
            sample_url=None,
            confidence=0.0,
            candidates=[{"key": k, "score": s} for k, s in ranked],
            topic_id=None,
            schema_name=None,
            proposed_dir=None,
            files=[],
            notes=[
                "No matching template found. Available systems: Workday absence balance, "
                "request time off, cancel time off, pay statement, view benefits.",
                "If you need a different scenario, reference the canonical samples at "
                "https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent and "
                "add a new entry to ess_maker/topics/__init__.py:WORKDAY_TEMPLATES.",
            ],
            error="no_template_matched",
        )

    topic_id = f"{chosen.key}-{_slug(prompt)[:24]}-{uuid.uuid4().hex[:6]}"
    out_dir = staging_dir() / topic_id
    out_dir.mkdir(parents=True, exist_ok=True)

    display = display_name or chosen.name
    yaml_text = render_topic_yaml(chosen, prompt, display)
    yaml_path = out_dir / "topic.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    files = [{
        "name": "topic.yaml",
        "path": str(yaml_path),
        "language": "yaml",
        "contents": yaml_text,
    }]

    # If the template references a known flow XML in this repo, copy it in as a
    # starter the maker can edit.
    if chosen.flow_template and chosen.flow_template.startswith("reference:"):
        ref_rel = chosen.flow_template.split(":", 1)[1]
        # Resolve from repo root (4 levels up from this file: topics/ess_maker/src/ess-maker/<repo>)
        repo_root = Path(__file__).resolve().parents[4]
        src = repo_root / ref_rel
        if src.exists():
            dst = out_dir / src.name
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            files.append({
                "name": src.name,
                "path": str(dst),
                "language": "xml",
                "contents": dst.read_text(encoding="utf-8"),
            })

    meta = {
        "topic_id": topic_id,
        "scenario_prompt": prompt,
        "display_name": display,
        "template_key": chosen.key,
        "schema_name": chosen.schema_name,
        "created_at": time.time(),
        "applied": False,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    notes = [
        f"Matched template: **{chosen.name}** (confidence {confidence:.0%}).",
        f"Reference sample: {chosen.sample_url}",
        "Staged at " + str(out_dir),
        "Next: review the YAML, fill in the FLOW_ID and CONNECTION_REF placeholders, "
        "then click **Apply to solution**.",
    ]
    if chosen.notes:
        notes.append(chosen.notes)

    return ScaffoldResult(
        ok=True,
        template_key=chosen.key,
        template_name=chosen.name,
        sample_url=chosen.sample_url,
        confidence=confidence,
        candidates=[{"key": k, "score": s} for k, s in ranked],
        topic_id=topic_id,
        schema_name=chosen.schema_name,
        proposed_dir=str(out_dir),
        files=files,
        notes=notes,
    )


def list_proposed() -> list[dict]:
    out = []
    base = staging_dir()
    for d in sorted(base.iterdir(), reverse=True) if base.exists() else []:
        meta_p = d / "meta.json"
        if not meta_p.exists():
            continue
        try:
            out.append(json.loads(meta_p.read_text(encoding="utf-8")) | {"path": str(d)})
        except Exception:
            pass
    return out


def apply_to_solution(topic_id: str, target_solution_subdir: str | None = None) -> dict:
    """Copy a staged topic into the target Copilot Studio solution folder.

    This stops short of repacking + importing — that requires PAC CLI auth which
    is out of band. The maker takes the resulting files and either commits them
    to the solution and runs `pac solution pack` + import, or pastes the topic
    into the Copilot Studio web authoring canvas.
    """
    src = staging_dir() / topic_id
    if not src.exists():
        return {"ok": False, "error": f"no such proposed topic: {topic_id}"}
    meta_p = src / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}

    # Default destination: WorkingESSSolution\Solution\BotComponent_*\
    repo_root = Path(__file__).resolve().parents[4]
    if target_solution_subdir:
        dst_root = repo_root / target_solution_subdir
    else:
        dst_root = (
            repo_root
            / "sources" / "dev" / "solutions" / "CopilotForEmployeeSelfServiceHR"
            / "Solution" / "BotComponent_msdyn_copilotforemployeeselfservicehr"
            / "TopicsStaged"
        )
    dst_root.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in src.iterdir():
        if f.name == "meta.json":
            continue
        target = dst_root / f.name
        target.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        copied.append(str(target))

    meta["applied"] = True
    meta["applied_at"] = time.time()
    meta["applied_to"] = str(dst_root)
    meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Auto-generate eval prompts for this topic so the maker can re-run the
    # eval and verify the topic actually fires. Best-effort — failures here
    # don't block the apply.
    eval_info: dict = {}
    try:
        tpl = next((t for t in _all_templates() if t.key == meta.get("template_key")), None)
        if tpl is not None:
            eval_info = generate_eval_prompts_for_topic(
                tpl,
                scenario_prompt=meta.get("scenario_prompt", ""),
                topic_id=topic_id,
            )
    except Exception as exc:  # pragma: no cover - non-fatal
        eval_info = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "topic_id": topic_id,
        "applied_to": str(dst_root),
        "files": copied,
        "eval_prompts": eval_info,
        "next_steps": [
            "Open Copilot Studio for the HR agent and import this topic, OR",
            "Run `pac solution pack` against the WorkingESSSolution and import the .zip in Power Apps.",
            "Then re-run the eval from the **Run Eval** tab to verify.",
        ],
    }


# ---------- Auto eval-prompt generation ------------------------------------

def _prompts_dir() -> Path:
    """The ess-maker/prompts directory (sibling of src/)."""
    # ess_maker/topics/__init__.py → topics → ess_maker → src → ess-maker
    return Path(__file__).resolve().parents[2] / "prompts"


def _paraphrases_for(template: TopicTemplate) -> list[str]:
    """Best-effort, deterministic paraphrases derived from trigger keywords."""
    kws = [k.strip() for k in (template.trigger_keywords or []) if k.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for kw in kws[:6]:
        for variant in (
            f"{kw[0].upper()}{kw[1:]}?",
            f"How do I {kw}?",
            f"Can you help me {kw}?",
            f"I want to {kw} please",
        ):
            v = variant.strip()
            key = v.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
            if len(out) >= 5:
                return out
    return out


def generate_eval_prompts_for_topic(template: TopicTemplate,
                                    scenario_prompt: str,
                                    topic_id: str) -> dict:
    """Append paraphrased prompts to prompts/scenarios.<template_key>.yaml.

    Avoids YAML lib dep — emits the same shape used by the loader.
    """
    prompts_dir = _prompts_dir()
    if not prompts_dir.exists():
        return {"ok": False, "error": f"prompts dir not found: {prompts_dir}"}

    safe_key = _slug(template.key).replace("-", ".")
    out_path = prompts_dir / f"scenarios.{safe_key}.yaml"

    paraphrases = [scenario_prompt.strip()] if scenario_prompt.strip() else []
    paraphrases += _paraphrases_for(template)
    # de-dupe preserving order
    seen: set[str] = set()
    paraphrases = [p for p in paraphrases if not (p.lower() in seen or seen.add(p.lower()))]

    expected_topic = template.schema_name.removeprefix("msdyn_")

    lines: list[str]
    if out_path.exists():
        # Append to existing file
        existing = out_path.read_text(encoding="utf-8").rstrip()
        lines = [existing]
    else:
        lines = ["scenarios:"]

    added_ids: list[str] = []
    for p in paraphrases:
        sid = f"auto-{template.key.replace('.', '-')}-{uuid.uuid4().hex[:6]}"
        added_ids.append(sid)
        # Quote-safe: replace any " with '
        safe_prompt = p.replace('"', "'")
        lines.append(
            f"- id: {sid}\n"
            f"  category: HR\n"
            f"  prompt: \"{safe_prompt}\"\n"
            f"  expected_topic: {expected_topic}\n"
            f"  expected_assertions: []\n"
            f"  tags:\n"
            f"  - auto\n"
            f"  - topic:{template.key}\n"
            f"  - source-topic-id:{topic_id}"
        )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "file": str(out_path),
        "added": len(added_ids),
        "ids": added_ids,
        "expected_topic": expected_topic,
    }
