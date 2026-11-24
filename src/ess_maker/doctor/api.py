"""HTTP API for the ESS Doctor page."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from ess_maker import diagnostics as ai_diag
from ess_maker.doctor import counterfactual as cf
from ess_maker.doctor import evidence, kusto, live
from ess_maker.doctor.agent_file import AgentDoc, from_instructions, parse_text
from ess_maker.doctor.blocks import diff_docs, segment
from ess_maker.doctor.drafter import CHANGE_TYPES, Intake, draft
from ess_maker.doctor.known_issues import lookup
from ess_maker.doctor.rules import CORE_BLOCKS, core_presence, validate
from ess_maker.settings import settings

router = APIRouter()
STATIC = Path(__file__).resolve().parents[1] / "webapp" / "static"

AGENTS: dict[str, AgentDoc] = {}
LIVE_META: dict[str, dict] = {}
BASELINES: dict[str, AgentDoc] = {}
JOBS: dict[str, asyncio.Queue] = {}


def _persona(doc: AgentDoc) -> str:
    s = (doc.bot_schema or doc.name).lower()
    if s.endswith("it") or "it." in s or s.endswith("dait"):
        return "it"
    if "core" in s or "hub" in s:
        return "hub"
    return "hr"


def _baseline(persona: str) -> AgentDoc | None:
    if persona in BASELINES:
        return BASELINES[persona]
    path = {"hr": settings.doctor_baseline_hr, "it": settings.doctor_baseline_it,
            "hub": settings.doctor_baseline_hub}.get(persona, "")
    if not path or not Path(path).exists():
        return None
    doc = parse_text(Path(path).read_text(encoding="utf-8"), f"ESS template ({persona.upper()})", f"baseline:{path}")
    BASELINES[persona] = doc
    return doc


def _agent(agent_id: str) -> AgentDoc:
    if agent_id not in AGENTS:
        raise HTTPException(404, "Agent not loaded. Load it again.")
    return AGENTS[agent_id]


def _register(doc: AgentDoc) -> dict:
    aid = uuid.uuid4().hex[:10]
    AGENTS[aid] = doc
    return {"agent_id": aid, "summary": doc.summary(), "persona": _persona(doc),
            "lines": [{"no": ln.no, "text": ln.text} for ln in doc.instructions],
            "blocks": [{"title": b.title, "start": b.start, "end": b.end} for b in segment(doc)]}


@router.get("/doctor", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    return HTMLResponse((STATIC / "doctor.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/api/doctor/status")
async def status() -> dict:
    org_url, _ = await live.resolve_org_url()
    return {
        "samples_dir": settings.doctor_samples_dir,
        "baselines": {p: bool(_baseline(p)) for p in ("hr", "it", "hub")},
        "kusto": {"allowed_tenants": len(kusto.allowed_tenants()), "region": settings.doctor_kusto_region,
                  "db": settings.doctor_kusto_db},
        "dataverse": {"org_url": org_url, "bot_schema": settings.mcs_bot_schema},
        "copilot_studio_client": bool(settings.mcs_env_id and settings.mcs_bot_schema),
        "appinsights": ai_diag.is_configured(),
        "azure_openai": bool(settings.azure_openai_endpoint and settings.azure_openai_api_key),
        "change_types": CHANGE_TYPES,
    }


@router.get("/api/doctor/samples")
async def samples() -> dict:
    d = Path(settings.doctor_samples_dir) if settings.doctor_samples_dir else None
    if not d or not d.exists():
        return {"samples": [], "hint": "Set DOCTOR_SAMPLES_DIR to a folder of agent exports (*.yml)."}
    return {"samples": sorted(p.name for p in d.glob("*.yml"))}


class LoadReq(BaseModel):
    sample: str = ""
    text: str = ""
    name: str = "Uploaded agent"
    instructions_only: bool = False


@router.post("/api/doctor/load")
async def load(req: LoadReq) -> dict:
    if req.sample:
        base = Path(settings.doctor_samples_dir).resolve()
        p = (base / req.sample).resolve()
        if base not in p.parents or not p.exists():
            raise HTTPException(400, "Unknown sample")
        doc = parse_text(p.read_text(encoding="utf-8"), p.stem, f"file:{p.name}")
    elif req.text and req.instructions_only:
        doc = from_instructions(req.text, req.name, "pasted")
    elif req.text:
        doc = parse_text(req.text, req.name, "upload")
    else:
        raise HTTPException(400, "Provide a sample name or file text.")
    if not doc.instructions:
        raise HTTPException(400, "No instructions block found (expected a GptComponent with `instructions: |`).")
    return _register(doc)


@router.post("/api/doctor/live/load")
async def live_load() -> dict:
    """Load the agent's saved instructions: read Copilot Studio directly, else the last pushed snapshot."""
    target = _cs_target()
    direct_error = ""
    if target:
        try:
            d = await live.fetch_agent_definition(*target)
            meta = {"environment_id": target[1], "agent_id": target[2], "name": d["name"], "url": "",
                    "captured": d["modified"], "platform_chars": None, "content_moderation": d["content_moderation"],
                    "config": {"knowledge": d["knowledge"], "switches": d["capabilities"]}, "source": "copilotstudio-api",
                    "published": d["published"], "topics_total": d["topics_total"], "topics_active": d["topics_active"],
                    "topic_list": d.get("topic_list") or []}
            snap = live.save_snapshot(target[2], d["text"], d["modified"], kind="text", config=meta["config"])
            LAST_PUSH.clear()
            LAST_PUSH.update({"text": d["text"], "meta": meta, "snapshot": snap})
        except live.LiveError as e:
            direct_error = str(e)
    if not LAST_PUSH:
        snaps = sorted(live.SNAP_ROOT.glob("*/*.json"), key=lambda p: p.stat().st_mtime)
        if snaps:
            j = json.loads(snaps[-1].read_text(encoding="utf-8"))
            if j.get("kind") == "text":
                agent_id = snaps[-1].parent.name
                LAST_PUSH.update({"text": j["data"], "snapshot": {"new": False, "hash": j["hash"], "count": len(list(snaps[-1].parent.glob("*.json")))},
                                  "meta": {"agent_id": agent_id, "environment_id": "", "name": "Copilot Studio agent",
                                           "url": "", "captured": j["captured"], "platform_chars": None,
                                           "config": j.get("config") or {}}})
    if not LAST_PUSH:
        raise HTTPException(400, direct_error or "Nothing received from Copilot Studio yet. Open the agent's Configure "
                                 "page in Copilot Studio and click the 'Send to Doctor' bookmarklet.")
    meta = LAST_PUSH["meta"]
    doc = _pushed_doc(LAST_PUSH["text"], meta)
    out = _register(doc)
    LIVE_META[out["agent_id"]] = {"bot_id": meta["agent_id"], "component_id": "", "published_on": meta["captured"],
                                  "snapshot": LAST_PUSH["snapshot"], "source": meta.get("source", "bookmark")}
    out["live"] = LIVE_META[out["agent_id"]] | {"snapshots": live.list_snapshots(meta["agent_id"])}
    out["direct_error"] = direct_error
    return out


def _cs_target() -> tuple[str, str, str] | None:
    """(tenant, environment, agent) for the direct Copilot Studio read, from settings or the last capture."""
    tenant = settings.doctor_cs_tenant_id or settings.dataverse_tenant_id
    meta = LAST_PUSH.get("meta") or {}
    env = settings.doctor_cs_environment_id or meta.get("environment_id", "")
    agent = settings.doctor_cs_agent_id or meta.get("agent_id", "")
    if not agent:
        snaps = sorted(live.SNAP_ROOT.glob("*/*.json"), key=lambda p: p.stat().st_mtime)
        agent = snaps[-1].parent.name if snaps else ""
    return (tenant, env, agent) if tenant and env and agent else None


LAST_PUSH: dict = {}


def _pushed_doc(text: str, meta: dict) -> AgentDoc:
    doc = from_instructions(text, meta.get("name") or "Copilot Studio agent", f"copilotstudio:{meta['agent_id']}")
    doc.modified_utc = meta["captured"]
    doc.platform_char_count = meta.get("platform_chars")
    cfg = meta.get("config") or {}
    doc.knowledge_list = cfg.get("knowledge") or []
    doc.knowledge_sources = len(doc.knowledge_list)
    tc = _topic_counts(meta["agent_id"])
    if tc:
        doc.topics_custom, doc.topics_system = tc["custom"], tc["system"]
    elif meta.get("topics_total") is not None:
        doc.topics_custom, doc.topics_system = meta["topics_total"], 0
    doc.capabilities = {k: v for k, v in (cfg.get("switches") or {}).items() if k and k != "Activity"}
    doc.content_moderation = meta.get("content_moderation", "")
    doc.topic_list = meta.get("topic_list") or []
    return doc


_TOPICS_DIR = Path(settings.workdir) / "doctor" / "topics"


def _topic_counts(agent_id: str) -> dict | None:
    p = _TOPICS_DIR / f"{agent_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


class TopicsReq(BaseModel):
    url: str
    custom: int = 0
    system: int = 0


@router.post("/api/doctor/push-topics")
async def push_topics(req: TopicsReq) -> dict:
    """Topic counts read from the Copilot Studio Topics tab (the Configure page lists only a few)."""
    import re as _re
    from datetime import datetime, timezone

    m = _re.search(r"/environments/[0-9a-f-]{36}/(?:copilots|bots|agents)/([0-9a-f-]{36})", req.url)
    if not m:
        raise HTTPException(400, "Open the agent's Topics tab in Copilot Studio first.")
    _TOPICS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"custom": req.custom, "system": req.system,
            "captured": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (_TOPICS_DIR / f"{m.group(1)}.json").write_text(json.dumps(data) + "\n", encoding="utf-8")
    return {"ok": True, **data}


class PushReq(BaseModel):
    text: str
    url: str = ""
    name: str = ""
    config: dict = Field(default_factory=dict)


@router.post("/api/doctor/push")
async def push(req: PushReq) -> dict:
    """Receive instructions read from the Copilot Studio page (bookmarklet / extension)."""
    import re as _re
    from datetime import datetime, timezone

    m = _re.search(r"/environments/([0-9a-f-]{36})/(?:copilots|bots|agents)/([0-9a-f-]{36})", req.url)
    if not m:
        raise HTTPException(400, "Open an agent's page in Copilot Studio first.")
    text = req.text.strip()
    platform_chars = None
    if pc := _re.search(r"([\d,]+) of ([\d,]+) characters entered", text) or _re.search(r"^\s*(\d[\d,]*)/(8,?000)\s*$", text, _re.M):
        platform_chars = int(pc.group(1).replace(",", ""))
    chrome = _re.compile(r"^(Use / to add tools.*|[\d,]+ of [\d,]+ characters entered.*|\d[\d,]*/\d[\d,]*|Instructions|Edit|Save|Cancel|Add|Undo|Redo)$")
    text = "\n".join(ln for ln in text.splitlines() if not chrome.match(ln.strip())).strip()
    if "#" not in text or len(text) < 50:
        raise HTTPException(400, "Couldn't find the instructions on this page. Open the agent's Configure tab.")
    meta = {"environment_id": m.group(1), "agent_id": m.group(2), "name": req.name or "Copilot Studio agent",
            "url": req.url, "captured": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platform_chars": platform_chars, "config": req.config}
    snap = live.save_snapshot(meta["agent_id"], text, meta["captured"], kind="text", config=req.config)
    LAST_PUSH.clear()
    LAST_PUSH.update({"text": text, "meta": meta, "snapshot": snap})
    return {"ok": True, "chars": len(text), "new_version": snap["new"], "versions": snap["count"]}


@router.post("/api/doctor/live/snapshot/{agent_id}/{snap_id}")
async def live_snapshot(agent_id: str, snap_id: str) -> dict:
    meta = LIVE_META.get(agent_id)
    if not meta:
        raise HTTPException(400, "Load the live agent first.")
    doc = live.snapshot_doc(meta["bot_id"], snap_id, _agent(agent_id).name)
    doc.topics, doc.globals_defined = _agent(agent_id).topics, _agent(agent_id).globals_defined
    return _register(doc)


@router.get("/api/doctor/live/history/{agent_id}")
async def live_history(agent_id: str) -> dict:
    meta = LIVE_META.get(agent_id)
    if not meta:
        raise HTTPException(400, "Load the live agent first.")
    h = await live.change_history(meta["component_id"])
    if h.get("ok"):
        for e in h["entries"]:
            e.pop("old_data", None)
            e.pop("new_data", None)
    return h


class ValidateReq(BaseModel):
    agent_id: str
    baseline: str = "auto"  # auto|hr|it|hub|none
    compare_to: str = ""  # agent_id of previous version


def _decorate(findings) -> list[dict]:
    out = []
    for f in findings:
        d = f.to_dict()
        d["known_issue_detail"] = lookup(f.known_issue) if f.known_issue else {}
        out.append(d)
    return out


@router.post("/api/doctor/validate")
async def run_validate(req: ValidateReq) -> dict:
    doc = _agent(req.agent_id)
    persona = _persona(doc) if req.baseline == "auto" else req.baseline
    base = None if persona == "none" else _baseline(persona)
    findings = validate(doc, base)
    presence = core_presence(doc)
    from ess_maker.doctor import practices

    gaps, scorecard = practices.check(doc)
    base_gaps = set(practices.check(base)[1]["gaps"]) if base else set()
    gap_dicts = _decorate(gaps)
    for g in gap_dicts:
        g["template_gap"] = g["rule_id"] in base_gaps
    resp: dict[str, Any] = {
        "persona": persona,
        "baseline": base.summary() if base else None,
        "findings": _decorate(findings) + gap_dicts,
        "practices": scorecard,
        "core": [{"id": b["id"], "title": b["title"], "present": bool(presence[b["id"]]),
                  "lines": presence[b["id"]][:5]} for b in CORE_BLOCKS],
        "counts": {s: sum(1 for f in findings if f.severity == s) for s in ("high", "medium", "low", "info")},
        "maker_vs_template": {"maker": sum(1 for f in findings if f.origin == "maker"),
                              "template": sum(1 for f in findings if f.origin == "template")},
    }
    if base:
        resp["diff_vs_baseline"] = diff_docs(base, doc)
    if req.compare_to:
        prev = _agent(req.compare_to)
        resp["diff_vs_previous"] = diff_docs(prev, doc)
        prev_ids = {(f.rule_id, f.current) for f in validate(prev, base)}
        resp["new_since_previous"] = [f["rule_id"] for f in resp["findings"]
                                      if (f["rule_id"], f["current"]) not in prev_ids]
    return resp


@router.get("/api/doctor/kusto/conversations")
async def kusto_conversations(region: str = "", bot: str = "", hours: int = 24) -> dict:
    try:
        return await kusto.recent_conversations(region or settings.doctor_kusto_region, bot, hours)
    except kusto.KustoError as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/doctor/kusto/near")
async def kusto_near(start: str, minutes: int = 60, region: str = "", bot: str = "") -> dict:
    try:
        return await kusto.sessions_near(region or settings.doctor_kusto_region, start, minutes, bot)
    except kusto.KustoError as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/doctor/versions/{bot_id}")
async def versions(bot_id: str, days: int = 7, region: str = "") -> dict:
    """Version timeline from runtime telemetry, matched to instruction snapshots captured from the page."""
    try:
        tl = await kusto.version_timeline(region or settings.doctor_kusto_region, bot_id, days)
    except kusto.KustoError as e:
        return {"ok": False, "error": str(e)}
    if not tl.get("ok"):
        return tl
    snaps = live.list_snapshots(bot_id)

    def stamp(s: str) -> str:  # snapshot ids look like 20261119T201539Z
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}"

    for v in tl["versions"]:
        lo = str(v.get("saved_at") or v["first_seen"])[:19]
        hi = str(v["last_seen"])[:19]
        nxt = next((s for s in tl["saves"] if str(s["t"])[:19] > lo), None)
        hi2 = str(nxt["t"])[:19] if nxt else "9999"
        match = [s for s in snaps if lo <= stamp(s["captured"]) < hi2]
        v["snapshot"] = match[-1]["id"] if match else None
        v["window_end"] = hi
    tl["snapshots"] = snaps
    return tl


@router.post("/api/doctor/versions/{bot_id}/load/{snap_id}")
async def load_version(bot_id: str, snap_id: str) -> dict:
    doc = live.snapshot_doc(bot_id, snap_id, "Copilot Studio agent")
    out = _register(doc)
    LIVE_META[out["agent_id"]] = {"bot_id": bot_id, "component_id": "", "published_on": snap_id, "snapshot": {}}
    return out


@router.get("/api/doctor/kusto/trace/{cid}")
async def kusto_trace(cid: str, region: str = "", days: int = 7) -> dict:
    try:
        return await kusto.conversation_trace(cid, region or settings.doctor_kusto_region, days)
    except kusto.KustoError as e:
        return {"ok": False, "error": str(e)}


class ExplainReq(BaseModel):
    agent_id: str
    conversation_id: str
    region: str = ""
    days: int = 7


@router.post("/api/doctor/explain")
async def explain(req: ExplainReq) -> dict:
    """Join a conversation's runtime verdict with instruction findings that affect the same stage."""
    try:
        trace = await kusto.conversation_trace(req.conversation_id, req.region or settings.doctor_kusto_region, req.days)
    except kusto.KustoError as e:
        return {"ok": False, "error": str(e)}
    if not trace.get("ok"):
        return trace
    doc = _agent(req.agent_id)
    base = _baseline(_persona(doc))
    findings = validate(doc, base)
    stage = trace["verdict"]["stage"]
    related_stages = {
        "rai": {"rai", "synthesis"}, "tool": {"tool", "routing"}, "search": {"search", "routing"},
        "routing": {"routing", "synthesis", "tool"}, "synthesis": {"synthesis", "routing", "search", "tool"},
        "runtime": {"tool", "platform"},
    }.get(stage, {stage})
    linked = [f for f in findings if f.stage in related_stages and f.origin == "maker"]
    conv_schema = (trace.get("bot") or {}).get("schema", "")
    mismatch = bool(doc.bot_schema and conv_schema and doc.bot_schema.split("_", 1)[-1] != conv_schema.split("_", 1)[-1])
    return {"ok": True, "trace": trace, "linked_findings": [] if mismatch else _decorate(linked),
            "agent_mismatch": f"Loaded agent is {doc.bot_schema} but this conversation ran on {conv_schema}; load "
                              "that agent's export to link instruction findings." if mismatch else "",
            "agent_version_note": "Compare the published version timestamp with the instruction change time to "
                                  "confirm this conversation ran on the edited instructions."}


class FixReq(BaseModel):
    agent_id: str
    rule_ids: list[str] = Field(default_factory=list)


@router.post("/api/doctor/fix")
async def fix_agent(req: FixReq) -> dict:
    from ess_maker.doctor.fixes import fix

    doc = _agent(req.agent_id)
    r = fix(doc, _baseline(_persona(doc)), req.rule_ids or None)
    fixed = r.pop("fixed_doc")
    reg = _register(fixed)
    AGENTS[reg["agent_id"]].topics = doc.topics
    AGENTS[reg["agent_id"]].bot_schema = doc.bot_schema
    AGENTS[reg["agent_id"]].source = doc.source
    r["fixed_agent_id"] = reg["agent_id"]
    r["fixed_lines"] = reg["lines"]
    r["old_instructions"] = doc.instruction_text
    return r


PENDING: dict = {}
_PENDING_FILE = Path(settings.workdir) / "doctor" / "pending.json"


class PendingReq(BaseModel):
    text: str
    label: str = ""


@router.post("/api/doctor/pending")
async def set_pending(req: PendingReq) -> dict:
    """Stage instructions for the 'Apply from Doctor' bookmarklet / extension to paste into Copilot Studio."""
    from datetime import datetime, timezone

    PENDING.clear()
    PENDING.update({"text": req.text, "label": req.label,
                    "staged": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_FILE.write_text(json.dumps(PENDING) + "\n", encoding="utf-8")
    return {"ok": True, "chars": len(req.text)}


@router.get("/api/doctor/pending")
async def get_pending() -> dict:
    if not PENDING and _PENDING_FILE.exists():
        PENDING.update(json.loads(_PENDING_FILE.read_text(encoding="utf-8")))
    if not PENDING:
        raise HTTPException(404, "Nothing staged. In the Doctor, click 'Send to Copilot Studio' first.")
    return PENDING


class EvalSetReq(BaseModel):
    prompts: list[dict]  # [{"prompt": str, "expect": "changed|unchanged|answer|offer|refusal", "expected": str}]
    include_golden: bool = True


GOLDEN_HR = [
    ("How many vacation days do I have left?", "Pulls the balance from Workday and cites it."),
    ("What is the parental leave policy?", "Answers from the parental leave policy with a citation."),
    ("How do I report a payroll issue?", "Explains from the KB, then offers to create an HR case."),
    ("Who is my manager?", "Answers from the employee's profile or Workday."),
    ("What are the public holidays this year?", "Lists holidays for the user's country with a citation."),
    ("My manager is harassing me", "Acknowledges, shares approved reporting channels and the HR support route."),
    ("How do I update my home address?", "Explains, then offers to start the address update."),
    ("What does PTO stand for?", "Defines PTO from approved content, not general knowledge."),
]


@router.post("/api/doctor/evalset.csv")
async def evalset(req: EvalSetReq):
    import csv
    import io

    from fastapi.responses import Response

    rows: list[tuple[str, str]] = []
    expect_text = {
        "offer": "Answers and offers the supported action.",
        "refusal": "Declines with the approved message and support route.",
        "unchanged": "Same behavior as before the instruction change.",
        "changed": "Reflects the new instruction.",
        "answer": "Answers from approved sources with a citation.",
    }
    for p in req.prompts:
        q = (p.get("prompt") or "").strip()
        if q:
            rows.append((q[:1000], p.get("expected") or expect_text.get(p.get("expect", "answer"), "")))
    if req.include_golden:
        have = {q.lower() for q, _ in rows}
        rows += [(q, e) for q, e in GOLDEN_HR if q.lower() not in have]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Question", "Expected response"])
    w.writerows(rows[:100])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="ess_doctor_evalset.csv"'})


class ReportReq(BaseModel):
    conversation_id: str
    agent_id: str = ""
    question: str = ""
    response: str = ""
    region: str = ""
    days: int = 7


@router.post("/api/doctor/report")
async def conversation_report(req: ReportReq) -> dict:
    """Investigation report (RAI_Event_Analysis style) for one conversation."""
    from ess_maker.doctor import report as report_mod

    try:
        trace = await kusto.conversation_trace(req.conversation_id, req.region or settings.doctor_kusto_region, req.days)
    except kusto.KustoError as e:
        return {"ok": False, "error": str(e)}
    if not trace.get("ok"):
        return trace
    findings, name, moderation, mismatch, agent_doc = [], "", "", "", None
    if req.agent_id and req.agent_id in AGENTS:
        doc = AGENTS[req.agent_id]
        conv_schema = (trace.get("bot") or {}).get("schema", "")
        same = not (doc.bot_schema and conv_schema and doc.bot_schema.split("_", 1)[-1] != conv_schema.split("_", 1)[-1])
        if same:
            findings = validate(doc, _baseline(_persona(doc)))
            name, moderation, agent_doc = doc.name, doc.content_moderation, doc
        else:
            mismatch = f"Loaded agent ({doc.bot_schema}) doesn't match this conversation's agent ({conv_schema}); " \
                       "instruction findings aren't included."
    text = report_mod.build(trace, question=req.question.strip(), findings=findings, moderation=moderation,
                            agent_name=name, agent=agent_doc, response=req.response.strip())
    return {"ok": True, "report": text, "trace": trace, "agent_mismatch": mismatch}


class TextReq(BaseModel):
    text: str
    agent_filter: str = ""


DEVUI: dict[str, dict] = {}


def _devui_report(parsed: dict, turn: int, agent_id: str) -> dict:
    from ess_maker.doctor import devui

    t = parsed["turns"][turn]
    served = from_instructions(t["instructions"], "Instructions that ran (DevUI)", "devui") if t["instructions"] else None
    current = AGENTS.get(agent_id) if agent_id else None
    basis = served or current
    findings = validate(basis, _baseline(_persona(current or basis))) if basis else []
    served_reg = _register(served) if served else None
    return {"ok": True, "conversation_id": parsed["conversation_id"], "turn": turn,
            "turns": [{"question": x["question"][:120], "request_id": x["request_id"]} for x in parsed["turns"]],
            "report": devui.build(parsed, turn, current=current, findings=findings),
            "served": served_reg, "summary": {k: t[k] for k in ("tools_offered", "tool_calls", "topics", "rai", "citations", "grounding")},
            "llm_calls": [{k: c[k] for k in ("purpose", "model", "latency_ms", "prompt_tokens", "completion_tokens", "tool_calls", "response")} for c in t["llm_calls"]]}


@router.post("/api/doctor/devui")
async def devui_upload(request: Request, agent_id: str = "", turn: int = 0) -> dict:
    """Analyze a Copilot DevUI conversation export (the JSON from DevUI → Download)."""
    from ess_maker.doctor import devui

    try:
        data = json.loads(await request.body())
    except ValueError:
        raise HTTPException(400, "Not a JSON file. In DevUI, click Download and upload the conversationHistory-<id>.json file.")
    if not isinstance(data, dict) or "requests" not in data:
        raise HTTPException(400, "This JSON isn't a DevUI conversation export (no 'requests').")
    parsed = devui.parse(data)
    if not parsed["turns"]:
        raise HTTPException(400, "The export has no user turns to analyze (only commands like /debug).")
    DEVUI[parsed["conversation_id"]] = parsed
    try:
        return _devui_report(parsed, min(turn, len(parsed["turns"]) - 1), agent_id)
    except Exception as e:  # noqa: BLE001 - unknown export variants shouldn't surface as a bare 500
        raise HTTPException(422, f"Couldn't analyze this export ({type(e).__name__}: {e}). Share the file so the parser can be updated.")


DEVUI_LATEST: dict = {}


@router.post("/api/doctor/devui/push")
async def devui_push(request: Request) -> dict:
    """Receive a DevUI export from the 'Send DevUI to Doctor' bookmark running in the maker's own browser."""
    from ess_maker.doctor import devui

    try:
        data = json.loads(await request.body())
    except ValueError:
        return {"ok": False, "detail": "Not a DevUI export."}
    if not isinstance(data, dict) or "requests" not in data:
        return {"ok": False, "detail": "Not a DevUI conversation export."}
    parsed = devui.parse(data)
    if not parsed["turns"]:
        return {"ok": False, "detail": "The conversation has no user turns to analyze."}
    DEVUI[parsed["conversation_id"]] = parsed
    DEVUI_LATEST.update({"conversation_id": parsed["conversation_id"], "at": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "conversation_id": parsed["conversation_id"], "turns": len(parsed["turns"])}


@router.get("/api/doctor/devui/latest")
async def devui_latest(since: str = "", agent_id: str = "") -> dict:
    """The newest export pushed by the bookmark after `since` (ISO time), as a full report."""
    at = DEVUI_LATEST.get("at", "")
    if not at or (since and at <= since):
        return {"ok": False, "waiting": True}
    return _devui_report(DEVUI[DEVUI_LATEST["conversation_id"]], 0, agent_id)


@router.get("/api/doctor/devui/{cid}")
async def devui_turn(cid: str, turn: int = 0, agent_id: str = "") -> dict:
    parsed = DEVUI.get(cid)
    if not parsed:
        raise HTTPException(404, "Upload the DevUI export again (the server was restarted).")
    return _devui_report(parsed, min(turn, len(parsed["turns"]) - 1), agent_id)


@router.post("/api/doctor/evidence/purview")
async def purview(req: TextReq) -> dict:
    return evidence.parse_purview_csv(req.text, req.agent_filter)


@router.post("/api/doctor/evidence/devmode")
async def devmode(req: TextReq) -> dict:
    return evidence.parse_devmode_log(req.text)


@router.get("/api/doctor/appinsights/{cid}")
async def appinsights(cid: str, lookback_min: int = 1440) -> dict:
    return await ai_diag.fetch_errors_for_conversation(cid, lookback_min=lookback_min)


class DraftReq(BaseModel):
    agent_id: str
    change_type: str
    intent: str
    should_affect: list[str] = Field(default_factory=list)
    must_not_affect: list[str] = Field(default_factory=list)
    content_sample: str = ""
    terms: list[list[str]] = Field(default_factory=list)
    subject: str = ""
    allowed: str = ""
    scope: str = ""
    format_spec: str = ""
    source_name: str = ""
    route: str = ""
    mode: str = ""
    target_chars: int = 0
    disclaimer_text: str = ""
    disclaimer_style: str = ""
    evals: list[dict] = Field(default_factory=list)
    use_llm: bool = True


class TriageReq(BaseModel):
    agent_id: str
    csv: str


@router.post("/api/doctor/evals/triage")
async def evals_triage(req: TriageReq) -> dict:
    """Why each failed question in a Copilot Studio evaluation run failed, and who fixes it."""
    from ess_maker.doctor.evaltriage import triage

    return triage(req.csv, _agent(req.agent_id))


class CompareReq(BaseModel):
    before_csv: str
    after_csv: str


@router.post("/api/doctor/evals/compare")
async def evals_compare(req: CompareReq) -> dict:
    """Same prompts, two instruction versions: which questions the fix repaired or broke."""
    from ess_maker.doctor.evaltriage import compare

    r = compare(req.before_csv, req.after_csv)
    if not r["rows"]:
        raise HTTPException(400, "No questions found. Upload two Copilot Studio evaluation results CSVs.")
    return r


@router.get("/api/doctor/practices")
async def practices_catalog() -> dict:
    from dataclasses import asdict

    from ess_maker.doctor.practices import PRACTICES

    return {"practices": [asdict(p) for p in PRACTICES]}


@router.post("/api/doctor/draft")
async def draft_change(req: DraftReq) -> dict:
    if req.change_type not in CHANGE_TYPES:
        raise HTTPException(400, f"change_type must be one of {list(CHANGE_TYPES)}")
    doc = _agent(req.agent_id)
    intake = Intake(**req.model_dump(exclude={"agent_id", "use_llm"}))
    result = await draft(intake, doc, _baseline(_persona(doc)), use_llm=req.use_llm)
    if result.get("needs_input"):
        return result
    result["old_instructions"] = doc.instruction_text
    reg = _register(from_instructions(result["new_instructions"], doc.name + " (draft)", "draft"))
    AGENTS[reg["agent_id"]].topics = doc.topics
    result["draft_agent_id"] = reg["agent_id"]
    return result


class PlanReq(BaseModel):
    previous_id: str
    current_id: str


@router.post("/api/doctor/counterfactual/plan")
async def cf_plan(req: PlanReq) -> dict:
    variants = cf.build_variants(_agent(req.previous_id), _agent(req.current_id))
    return {"variants": [{k: v for k, v in x.items() if k != "instructions"} | {"chars": len(x["instructions"])}
                         for x in variants]}


class RunReq(PlanReq):
    prompts: list[dict]
    repeats: int = 1
    confirm_test_agent: bool = False


@router.post("/api/doctor/counterfactual/run")
async def cf_run(req: RunReq) -> dict:
    if not req.confirm_test_agent:
        raise HTTPException(400, "This publishes instruction variants to the configured agent. Confirm it is a "
                                 "TEST agent (confirm_test_agent=true).")
    if not (settings.mcs_env_id and settings.mcs_bot_schema):
        raise HTTPException(400, "Set MCS_ENV_ID and MCS_BOT_SCHEMA for the test agent.")
    variants = cf.build_variants(_agent(req.previous_id), _agent(req.current_id))
    job = uuid.uuid4().hex[:8]
    q: asyncio.Queue = asyncio.Queue()
    JOBS[job] = q

    async def progress(msg: str) -> None:
        await q.put({"type": "progress", "message": msg})

    async def worker() -> None:
        try:
            run = await cf.run_live(variants, req.prompts, req.repeats, progress)
            await q.put({"type": "done", "run_id": run["id"]})
        except Exception as e:  # surfaced to the UI stream
            await q.put({"type": "error", "message": str(e)[:500]})

    asyncio.create_task(worker())
    return {"job_id": job, "variants": len(variants)}


@router.get("/api/doctor/jobs/{job}/stream")
async def job_stream(job: str):
    q = JOBS.get(job)
    if not q:
        raise HTTPException(404, "Unknown job")

    async def gen():
        while True:
            ev = await q.get()
            yield f"data: {json.dumps(ev)}\n\n"
            if ev["type"] in ("done", "error"):
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/doctor/counterfactual/runs")
async def cf_runs() -> dict:
    return {"runs": cf.list_runs()}


@router.get("/api/doctor/counterfactual/runs/{run_id}")
async def cf_run_get(run_id: str) -> dict:
    try:
        return cf.load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "Run not found")
