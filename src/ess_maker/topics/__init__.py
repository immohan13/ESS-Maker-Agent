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
        notes="Reads `Get_Workers` then `Get_Absence_Balances` on Absence_Management. Requires the Workday connector to be authorized for the bot author or test user.",
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
        notes="Uses Submit_Absence_Request_Event. Uses the same flow XML pattern as the existing reference under WorkdayEmployeeRequestTimeOff/.",
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

    This is a *starting point* the maker iterates on — not a finished topic.
    It includes:
      • declared inputs from the template
      • a model description grounded in the maker's actual scenario prompt
      • placeholder action node referencing the relevant Workday operation
      • clear TODO markers wherever a connection ref or flow ID must be filled in
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

    triggers = "\n".join(f'  - "{kw}"' for kw in template.trigger_keywords[:8])

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

triggerPhrases:
{triggers}

beginDialog:
  kind: OnRecognizedIntent
  id: main
  intent: {{}}
  actions:
{action_block}
"""


def _render_action_for(template: TopicTemplate) -> str:
    if template.system != "workday":
        return (
            "    - kind: SendActivity\n"
            "      id: not_implemented\n"
            "      activity: \"This topic is a stub. Add the action nodes for {0}.\"\n".format(template.key)
        )
    # Common Workday pattern: set tenant url, call connector / flow, format response.
    return f"""    - kind: SetVariable
      id: set_workday_url
      variable: Topic.WorkdayUrl
      value: =Concatenate("https://impl.workday.com/", Env.WorkdayTenant, "/home.htmld")

    # ---------------------------------------------------------------
    # TODO(maker): wire this to your Workday connector or Power Automate flow.
    # Replace <CONNECTION_REF> with the connection reference name in the solution
    # and <FLOW_ID> with the GUID of the corresponding cloud flow.
    # ---------------------------------------------------------------
    - kind: InvokeFlowAction
      id: call_{template.schema_name.lower()}
      input:
        binding: {{}}
      flowId: <FLOW_ID>            # TODO: paste flow GUID
      connectionReference: <CONNECTION_REF>  # TODO: e.g. cr123_workdayHR

    - kind: SendActivity
      id: reply_ok
      activity: "Done. Open Workday: {{Topic.WorkdayUrl}}"

    - kind: SendActivity
      id: reply_error
      activity: "I couldn't reach Workday. The connection may need to be re-authorized."
"""


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
