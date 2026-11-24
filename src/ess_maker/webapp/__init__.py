"""FastAPI web app for ESS Maker — non-technical maker UX."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ess_maker.eval.cs_client import CopilotStudioClient, ping as cs_ping
from ess_maker.eval.runner import (
    _heuristic_score,
    _run_one,
    _select_client,
    load_scenarios,
)
from ess_maker.eval.schemas import EvalReport, ScoredResult
from ess_maker.settings import settings
from ess_maker import diagnostics as ai_diag


APP_ROOT = Path(__file__).parent
STATIC_DIR = APP_ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI(title="ESS Maker")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

# Lets the Copilot Studio page / browser extension call the local Doctor API.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https://copilotstudio(\.preview)?\.microsoft\.com|https://[a-z0-9.-]*devui[a-z0-9.-]*\.(office\.net|office\.com|microsoft\.com)|chrome-extension://[a-p]{32}|extension://[a-p]{32})$",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    allow_private_network=True,
)


@app.middleware("http")
async def _private_network_access(request: Request, call_next):
    # Chromium Private Network Access: public origin -> localhost preflight needs this header.
    response = await call_next(request)
    if request.headers.get("access-control-request-private-network") == "true":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

from ess_maker.doctor.api import router as doctor_router  # noqa: E402

app.include_router(doctor_router)


# ---------- Storage (file-backed, KISS) ----------

def _store_path(name: str) -> Path:
    p = settings.ess_maker_workdir / "ux"
    p.mkdir(parents=True, exist_ok=True)
    return p / name


def _read_json(name: str, default: Any) -> Any:
    p = _store_path(name)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(name: str, data: Any) -> None:
    _store_path(name).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------- Models ----------

class KnowledgeSource(BaseModel):
    id: str
    name: str
    description: str
    url: str | None = None
    priority: int = 1


class AgentConfig(BaseModel):
    display_name: str = "Employee Self-Service HR"
    description: str = ""
    instruction: str = ""
    knowledge: list[KnowledgeSource] = []
    business_scenarios: str = ""


class JobStatus(BaseModel):
    id: str
    state: str  # queued|running|done|error
    total: int = 0
    done: int = 0
    started_at: float = 0
    finished_at: float = 0
    error: str = ""
    report_path: str | None = None


# ---------- Job state (in-memory; one eval at a time is fine for a single user) ----------

JOBS: dict[str, JobStatus] = {}
JOB_QUEUES: dict[str, asyncio.Queue] = {}


# ---------- Pages ----------

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# ---------- API: setup ----------

@app.get("/api/config")
async def get_config() -> dict:
    return {
        "agent": _read_json("agent.json", AgentConfig().model_dump()),
        "target": {
            "env_id": settings.mcs_env_id,
            "schema": settings.mcs_bot_schema,
            "tenant": settings.tenant_id,
        },
        "diagnostics": {
            "appinsights_configured": ai_diag.is_configured(),
            "appinsights_app_id": settings.appinsights_app_id[:8] + "…" if settings.appinsights_app_id else "",
        },
    }


@app.post("/api/config")
async def save_config(cfg: AgentConfig) -> dict:
    _write_json("agent.json", cfg.model_dump())
    return {"ok": True}


@app.post("/api/test-connection")
async def test_connection() -> dict:
    try:
        return await cs_ping()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------- API: scenarios ----------

@app.get("/api/scenarios")
async def list_scenarios() -> list[dict]:
    p = Path("prompts")
    if not p.exists():
        return []
    files = []
    for f in p.glob("*.yaml"):
        try:
            scs = load_scenarios(f)
            files.append({"name": f.name, "path": str(f), "count": len(scs)})
        except Exception:
            pass
    return files


# ---------- API: run eval (background + SSE progress) ----------

class RunEvalReq(BaseModel):
    scenarios_path: str
    concurrency: int = 3


@app.post("/api/run-eval")
async def start_eval(req: RunEvalReq) -> dict:
    scenarios = load_scenarios(req.scenarios_path)
    job_id = uuid.uuid4().hex[:8]
    job = JobStatus(id=job_id, state="queued", total=len(scenarios), started_at=time.time())
    JOBS[job_id] = job
    JOB_QUEUES[job_id] = asyncio.Queue(maxsize=200)
    asyncio.create_task(_run_job(job_id, scenarios, req.concurrency))
    return {"job_id": job_id, "total": len(scenarios)}


async def _run_job(job_id: str, scenarios, concurrency: int) -> None:
    job = JOBS[job_id]
    q = JOB_QUEUES[job_id]
    job.state = "running"
    try:
        client = _select_client()
        sem = asyncio.Semaphore(concurrency)
        items: list[ScoredResult] = []

        async def _one(s):
            async with sem:
                run = await _run_one(client, s)
                sc = _heuristic_score(s, run)
                sr = ScoredResult(scenario=s, result=run, score=sc)
                items.append(sr)
                job.done = len(items)
                await q.put({
                    "id": s.id,
                    "prompt": s.prompt[:120],
                    "passed": sc.passed,
                    "failure_mode": sc.failure_mode,
                    "latency_ms": run.latency_ms,
                    "done": job.done,
                    "total": job.total,
                })

        await asyncio.gather(*(_one(s) for s in scenarios))
        report = EvalReport(agent_id=settings.mcs_bot_schema or "unknown", items=items)
        out = settings.ess_maker_workdir / "eval-runs"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"ux-{job_id}.json"
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        (out / "latest.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        job.report_path = str(path)
        job.state = "done"
        job.finished_at = time.time()
        aclose = getattr(client, "aclose", None)
        if aclose:
            await aclose()
    except Exception as e:
        job.state = "error"
        job.error = f"{type(e).__name__}: {e}"
        job.finished_at = time.time()
    finally:
        await q.put({"_end": True, "state": job.state, "error": job.error, "report_path": job.report_path})


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    if job_id not in JOB_QUEUES:
        raise HTTPException(404, "no such job")
    q = JOB_QUEUES[job_id]

    async def gen():
        while True:
            evt = await q.get()
            yield f"data: {json.dumps(evt)}\n\n"
            if evt.get("_end"):
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- API: results & insights ----------

@app.get("/api/results/latest")
async def latest_results() -> dict:
    p = settings.ess_maker_workdir / "eval-runs" / "latest.json"
    if not p.exists():
        return {"items": [], "summary": {}}
    report = json.loads(p.read_text(encoding="utf-8"))
    items = report["items"]
    pass_count = sum(1 for i in items if _is_pass(i))
    return {
        "items": [_brief_item(i) for i in items],
        "summary": {
            "total": len(items),
            "pass": pass_count,
            "fail": len(items) - pass_count,
            "pass_rate": pass_count / len(items) if items else 0,
            "failure_modes": dict(Counter(i["score"]["failure_mode"] for i in items)),
            "latency_p50": _percentile([i["result"]["latency_ms"] for i in items], 50),
            "latency_p95": _percentile([i["result"]["latency_ms"] for i in items], 95),
            "topic_failures": _summarize_failures_by_topic(items),
        },
    }


def _summarize_failures_by_topic(items: list[dict]) -> list[dict]:
    """Group results by expected_topic and surface high-fail-rate topics."""
    buckets: dict[str, dict] = {}
    for it in items:
        topic = (it.get("scenario") or {}).get("expected_topic") or "(unspecified)"
        b = buckets.setdefault(topic, {"topic": topic, "total": 0, "pass": 0, "fail": 0,
                                       "failure_modes": Counter(), "sample_ids": []})
        b["total"] += 1
        if _is_pass(it):
            b["pass"] += 1
        else:
            b["fail"] += 1
            b["failure_modes"][it["score"]["failure_mode"]] += 1
            if len(b["sample_ids"]) < 3:
                b["sample_ids"].append(it["scenario"]["id"])
    out = []
    for b in buckets.values():
        b["failure_modes"] = dict(b["failure_modes"])
        b["fail_rate"] = b["fail"] / b["total"] if b["total"] else 0
        out.append(b)
    # Sort: most-failing topics first
    out.sort(key=lambda x: (-x["fail_rate"], -x["fail"], x["topic"]))
    return out


@app.get("/api/results/{idx}")
async def get_result(idx: int) -> dict:
    p = settings.ess_maker_workdir / "eval-runs" / "latest.json"
    if not p.exists():
        raise HTTPException(404, "no eval yet")
    report = json.loads(p.read_text(encoding="utf-8"))
    if idx < 0 or idx >= len(report["items"]):
        raise HTTPException(404, "no such item")
    item = report["items"][idx]
    insight = _build_insight(item, _read_json("agent.json", AgentConfig().model_dump()))

    # If App Insights is configured, ALWAYS fetch logs for this conversation.
    # Even on PASS results, the topic trace tells the maker WHICH path the
    # orchestrator took (KB vs Workday topic vs custom topic) — critical for
    # understanding why a "passing" eval actually didn't exercise the topic
    # they're trying to test.
    reply = (item["result"].get("raw_reply") or "")
    conv_id = item["result"].get("conversation_id")
    if conv_id and ai_diag.is_configured():
        diag = await ai_diag.fetch_errors_for_conversation(conv_id)
        insight["app_insights"] = diag
        if diag.get("ok") and diag.get("rows") and _detect_system_on_error(reply):
            s = diag["summary"]
            insight["insights"].insert(0, {
                "severity": "high",
                "title": f"Underlying error from App Insights: {s['headline'][:100]}",
                "why": s.get("details") or s["headline"],
                "fix": s.get("fix_hint") or "",
                "target": "topic",
            })
    return {**item, "insight": insight}


@app.get("/api/diagnostics/{conv_id}")
async def diagnostics(conv_id: str, lookback_min: int = 60) -> dict:
    """On-demand App Insights lookup for any conversation_id."""
    return await ai_diag.fetch_errors_for_conversation(conv_id, lookback_min=lookback_min)


class DiagnosticsConfigReq(BaseModel):
    app_id: str
    api_key: str


@app.post("/api/diagnostics-config")
async def set_diagnostics_config(req: DiagnosticsConfigReq) -> dict:
    """Save App Insights creds at runtime (and persist for next restart)."""
    if not req.app_id or not req.api_key:
        return {"ok": False, "error": "app_id and api_key are required"}
    # Mutate the live settings object so subsequent queries pick it up immediately.
    settings.appinsights_app_id = req.app_id.strip()
    settings.appinsights_api_key = req.api_key.strip()
    # Persist to a side file so it survives a server restart (kept out of .env to avoid leaking the key into VCS).
    _write_json("appinsights.json", {"app_id": settings.appinsights_app_id, "api_key": settings.appinsights_api_key})
    return {"ok": True, "configured": ai_diag.is_configured()}


# On import, hydrate settings from the side file if env didn't supply them.
def _hydrate_appinsights_from_disk() -> None:
    if settings.appinsights_app_id and settings.appinsights_api_key:
        return
    try:
        data = _read_json("appinsights.json", None)
    except Exception:
        return
    if isinstance(data, dict):
        settings.appinsights_app_id = data.get("app_id") or settings.appinsights_app_id
        settings.appinsights_api_key = data.get("api_key") or settings.appinsights_api_key


_hydrate_appinsights_from_disk()


def _brief_item(item: dict) -> dict:
    return {
        "id": item["scenario"]["id"],
        "prompt": item["scenario"]["prompt"],
        "passed": _is_pass(item),
        "failure_mode": item["score"]["failure_mode"],
        "latency_ms": item["result"]["latency_ms"],
        "category": item["scenario"].get("category"),
        "reply_preview": (item["result"].get("raw_reply") or "")[:200],
    }


def _is_pass(item: dict) -> bool:
    s = item["score"]
    return s["failure_mode"] == "none" and s["relevance"] >= 3 and s["groundedness"] >= 3


def _percentile(vals: list[int], pct: int) -> int:
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * pct / 100))]


# ---------- Insight engine (rule-based; LLM swap-in later) ----------

REFUSAL_MARKERS = ("not able to find", "i can't help", "i cannot", "out of scope", "rephrase")

# [System] - On Error topic in Copilot Studio fires this exact text whenever any
# action in any topic throws (auth failure, null variable, missing connection, etc.).
SYSTEM_ON_ERROR_MARKERS = (
    "an unexpected error has occurred",
    "contact the owner of this bot",
    "something went wrong",
)

CONNECTOR_AUTH_MARKERS = (
    "sign in", "authorize", "connection is not configured", "needs to be authorized",
)


def _detect_system_on_error(reply: str) -> bool:
    r = reply.lower()
    return any(m in r for m in SYSTEM_ON_ERROR_MARKERS)


def _detect_connector_auth(reply: str) -> bool:
    r = reply.lower()
    return any(m in r for m in CONNECTOR_AUTH_MARKERS) and "http" in r


def _build_insight(item: dict, agent: dict) -> dict:
    """Plain-English, action-oriented insight a maker can act on."""
    prompt = item["scenario"]["prompt"]
    reply = (item["result"].get("raw_reply") or "").strip()
    err = item["result"].get("error")
    fm = item["score"]["failure_mode"]
    cat = item["scenario"].get("category") or "?"
    insights: list[dict] = []

    if err:
        insights.append({
            "severity": "high",
            "title": "Connection error",
            "why": f"The agent could not be reached: {err}",
            "fix": "Check tenant access, schema name, and that you can sign in to the test account.",
            "target": "infra",
        })

    elif _detect_system_on_error(reply):
        insights.append({
            "severity": "high",
            "title": "Topic crashed → [System] On Error fired",
            "why": (
                "The agent matched a topic, but an action inside it threw and the global "
                "**[System] - On Error** topic returned its generic message. The user never sees the real cause."
            ),
            "fix": (
                "1) In Copilot Studio → **Activity** tab, open the most recent failed conversation "
                "and look at the topic trace to find which node threw.  "
                "2) Common causes: a Power Automate flow returned 4xx/5xx, a connector "
                "(ServiceNow / Workday / Graph) isn't authorized for the test user, or a "
                "variable was used before being set.  "
                "3) In **[System] - On Error**, temporarily set `Topic.varShowDebugInfo = true` "
                "so the message shows the underlying error string while you debug.  "
                "4) Add a Question node before the failing action to validate inputs, and use "
                "an `On Error` handler inside the calling topic so the user gets a useful message "
                "instead of the generic one."
            ),
            "target": "topic",
        })

    elif _detect_connector_auth(reply):
        insights.append({
            "severity": "high",
            "title": "Connector requires sign-in",
            "why": (
                "The reply is asking the user to authorize a connector (ServiceNow, Workday, etc.). "
                "The test account has not consented, or the connection is set to per-user auth without a working refresh token."
            ),
            "fix": (
                "Open the agent → **Tools / Connections** and confirm the connector is set to "
                "**Bot author authentication** (single sign-on for the whole agent) if you don't "
                "want each user to authorize. For per-user, make sure the test account has run the "
                "auth flow at least once. Re-run the eval after the test user signs in."
            ),
            "target": "infra",
        })

    elif not reply:
        insights.append({
            "severity": "high",
            "title": "Empty reply",
            "why": "The agent returned no text. The prompt likely matched no topic and no fallback was triggered.",
            "fix": "Add a Conversational Boosting / fallback topic, or extend Generative Answers to cover this scope.",
            "target": "topic",
        })

    elif any(m in reply.lower() for m in REFUSAL_MARKERS):
        insights.append({
            "severity": "medium",
            "title": "Out-of-scope refusal",
            "why": "The agent told the user it can't help. Either the prompt truly is out of scope, or your knowledge description doesn't cover this question.",
            "fix": _kb_fix_suggestion(prompt, agent),
            "target": "knowledge",
        })

    elif "?" in reply.split("\n")[0] and len(reply) < 400:
        insights.append({
            "severity": "low",
            "title": "Asked clarifying question",
            "why": "The agent asked the user to clarify before answering. Often correct, but if the question is well-defined this might be over-asking.",
            "fix": "Tighten the topic's trigger phrases or instruction so it skips the clarifier when the prompt is unambiguous.",
            "target": "instruction",
        })

    if fm == "knowledge_miss":
        insights.append({
            "severity": "high",
            "title": "Knowledge gap",
            "why": f"This {cat} question wasn't matched against any KB source.",
            "fix": _kb_fix_suggestion(prompt, agent),
            "target": "knowledge",
        })

    if not insights:
        insights.append({
            "severity": "info",
            "title": "Looks fine",
            "why": "The agent responded with relevant content. No action needed.",
            "fix": "",
            "target": "",
        })
    return {"insights": insights, "passed": _is_pass(item)}


def _kb_fix_suggestion(prompt: str, agent: dict) -> str:
    sources = agent.get("knowledge", [])
    if not sources:
        return "Add a Knowledge source covering this topic in **Knowledge & Scope**, then re-run the eval."
    names = ", ".join(s["name"] for s in sources[:3])
    return (
        f"Verify your KB sources ({names}) actually contain this content. "
        "If yes, edit the source's **description** to mention the keywords from this prompt — "
        "the orchestrator uses descriptions to pick which source to cite."
    )


# ---------- API: suggestions (rule-based now; LLM-backed later) ----------

class SuggestReq(BaseModel):
    target: str  # 'agent_description' | 'knowledge' | 'instruction' | 'topic'
    failed_item_ids: list[str] = []
    topic: str | None = None  # when target='topic', scope to this expected_topic


@app.post("/api/suggest")
async def suggest(req: SuggestReq) -> dict:
    p = settings.ess_maker_workdir / "eval-runs" / "latest.json"
    if not p.exists():
        return {"suggestion": "Run an eval first."}
    report = json.loads(p.read_text(encoding="utf-8"))
    fails = [i for i in report["items"] if not _is_pass(i)]
    if req.failed_item_ids:
        fails = [i for i in fails if i["scenario"]["id"] in req.failed_item_ids]
    if req.target == "topic" and req.topic:
        fails = [i for i in fails
                 if (i.get("scenario") or {}).get("expected_topic") == req.topic
                 or (req.topic == "(unspecified)" and not (i.get("scenario") or {}).get("expected_topic"))]

    agent = _read_json("agent.json", AgentConfig().model_dump())
    if req.target == "agent_description":
        return {"suggestion": _suggest_agent_description(agent, fails)}
    if req.target == "knowledge":
        return {"suggestion": _suggest_knowledge_changes(agent, fails)}
    if req.target == "instruction":
        return {"suggestion": _suggest_instruction(agent, fails)}
    if req.target == "topic":
        return {"suggestion": _suggest_topic_fixes(req.topic or "(unspecified)", fails)}
    return {"suggestion": "Unsupported target."}


def _suggest_topic_fixes(topic: str, fails: list[dict]) -> str:
    if not fails:
        return f"No failures recorded for topic `{topic}`. Nothing to suggest."
    crashed = sum(1 for f in fails if _detect_system_on_error(f["result"].get("raw_reply") or ""))
    auth = sum(1 for f in fails if _detect_connector_auth(f["result"].get("raw_reply") or ""))
    refused = sum(1 for f in fails if any(m in (f["result"].get("raw_reply") or "").lower() for m in REFUSAL_MARKERS))
    out = [f"### Suggested fixes for topic `{topic}` ({len(fails)} failure(s))\n"]
    if crashed:
        out.append(
            f"- **{crashed}/{len(fails)} hit `[System] On Error`.** "
            "Open Copilot Studio → Activity for one of these conversations and find the failing action node. "
            "Common root causes: missing `connectionReference`, wrong `flowId`, expired connector auth, "
            "or a Power Automate flow returning `FlowDisabled`/`Unauthorized`. "
            "If App Insights is configured, the **Real error** card on each failed item shows the underlying message."
        )
    if auth:
        out.append(
            f"- **{auth}/{len(fails)} look like connector auth issues.** "
            "Re-authorize the connector in Copilot Studio → Settings → Connections."
        )
    if refused:
        out.append(
            f"- **{refused}/{len(fails)} were refusals.** "
            "Check the topic's trigger phrases — the orchestrator may be routing to the wrong topic. "
            "Add 2-3 more paraphrases of the failing prompts to this topic's `triggerPhrases`."
        )
    if not (crashed or auth or refused):
        out.append(
            "- Failures look like quality/relevance issues. Tighten the topic's `modelDescription` "
            "to spell out exactly what data it returns, and add 2-3 more `triggerPhrases` covering the "
            "wording the user actually used."
        )
    out.append("\n**Failed prompts:**")
    for f in fails[:5]:
        out.append(f"- `{f['scenario']['id']}` — {f['scenario']['prompt']}")
    return "\n".join(out)


def _topics_from_prompts(prompts: list[str]) -> list[str]:
    """Crude: pull frequent 2-grams from failed prompts."""
    words = []
    for p in prompts:
        words.extend([w.lower().strip(".?!,") for w in p.split() if len(w) > 4])
    return [w for w, _ in Counter(words).most_common(8)]


def _suggest_agent_description(agent: dict, fails: list[dict]) -> str:
    keys = _topics_from_prompts([f["scenario"]["prompt"] for f in fails])
    base = agent.get("description") or "An HR self-service assistant for employees."
    if not keys:
        return base
    return (
        f"{base}\n\n"
        f"This assistant should also confidently answer questions about: "
        f"{', '.join(keys)}. If a user asks something outside HR/IT employee scope, "
        f"politely redirect them to the right team."
    )


def _suggest_knowledge_changes(agent: dict, fails: list[dict]) -> str:
    if not fails:
        return "No failures to derive changes from."
    out = ["**Knowledge improvements based on the failed prompts:**\n"]
    for src in agent.get("knowledge", []):
        out.append(f"- **{src['name']}** — current description: \"{src.get('description','')}\"")
    out.append("")
    by_cat = {}
    for f in fails:
        cat = f["scenario"].get("category") or "Other"
        by_cat.setdefault(cat, []).append(f["scenario"]["prompt"])
    for cat, prompts in by_cat.items():
        keys = _topics_from_prompts(prompts)
        out.append(f"### {cat} — add coverage for: {', '.join(keys)}")
        for p in prompts[:5]:
            out.append(f"  - {p}")
    out.append(
        "\n**Tip:** Edit each KB source's description to mention these keywords. "
        "Copilot Studio's orchestrator uses the description to pick which source to cite — "
        "vague descriptions = wrong source picked = bad answers."
    )
    return "\n".join(out)


def _suggest_instruction(agent: dict, fails: list[dict]) -> str:
    replies = [(f["result"].get("raw_reply") or "") for f in fails]
    refused = sum(1 for r in replies if any(m in r.lower() for m in REFUSAL_MARKERS))
    asked = sum(1 for r in replies if "?" in r.split("\n")[0])
    crashed = sum(1 for r in replies if _detect_system_on_error(r))
    auth = sum(1 for r in replies if _detect_connector_auth(r))
    notes = []
    if crashed:
        notes.append(
            f"- **{crashed} prompts hit the [System] - On Error topic** (generic 'unexpected error' message). "
            "These are NOT instruction problems — a topic action is throwing. "
            "Open Copilot Studio → **Activity** → click a failed conversation → find the topic that errored. "
            "Most common root causes: (a) connector not authorized for the test user, "
            "(b) variable referenced before set, (c) Power Automate flow returning non-200. "
            "Temporarily set `Topic.varShowDebugInfo = true` in the On Error topic to surface the real error."
        )
    if auth:
        notes.append(
            f"- **{auth} prompts asked the user to sign in to a connector.** "
            "Switch the connector to bot-author auth, or have the test account complete the auth flow once."
        )
    if refused:
        notes.append(
            f"- **{refused} prompts got out-of-scope refusals** → add explicit scope guidance to the "
            "system instruction (HR + IT employee questions are in scope; general internet trivia is not)."
        )
    if asked:
        notes.append(
            f"- **{asked} prompts triggered clarifying questions** when the answer was probably unambiguous → add: "
            "'Only ask clarifying questions when the user's request is genuinely ambiguous; otherwise answer directly.'"
        )
    if not notes:
        notes.append("- No common pattern detected; keep current instruction.")
    return "**Suggested fixes based on this run:**\n\n" + "\n\n".join(notes)


# ---------- Publish (stub) ----------

class PublishReq(BaseModel):
    target: str  # 'test' | 'prod'


@app.post("/api/publish")
async def publish(req: PublishReq) -> dict:
    # Real impl would use ess_maker.publish.* against the Power Platform API.
    return {
        "ok": True,
        "target": req.target,
        "message": (
            "Publish stub — wire up against ess_maker.publish.publisher when AOAI/PP creds are available. "
            "For now, copy the suggestions and apply them in Copilot Studio manually."
        ),
    }


# ---------- API: topic scaffolding (scenario → topic) ----------

from ess_maker import topics as topics_mod


class ScaffoldTopicReq(BaseModel):
    prompt: str
    display_name: str | None = None
    template_key: str | None = None  # force a specific template


class ApplyTopicReq(BaseModel):
    topic_id: str
    target_solution_subdir: str | None = None


@app.get("/api/topics/templates")
async def topic_templates() -> dict:
    return {"templates": topics_mod.list_templates()}


@app.post("/api/topics/scaffold")
async def scaffold_topic(req: ScaffoldTopicReq) -> dict:
    if not (req.prompt or "").strip():
        raise HTTPException(400, "prompt is required")
    res = topics_mod.scaffold_from_scenario(
        prompt=req.prompt.strip(),
        display_name=req.display_name,
        force_template=req.template_key,
    )
    # asdict-friendly serialization (ScaffoldResult is a dataclass)
    from dataclasses import asdict
    return asdict(res)


@app.get("/api/topics/proposed")
async def list_proposed_topics() -> dict:
    return {"items": topics_mod.list_proposed()}


@app.post("/api/topics/apply")
async def apply_topic(req: ApplyTopicReq) -> dict:
    return topics_mod.apply_to_solution(req.topic_id, req.target_solution_subdir)


class PublishTopicDirectReq(BaseModel):
    topic_id: str
    bot_schema: str | None = None


@app.post("/api/topics/publish-direct")
async def publish_topic_direct(req: PublishTopicDirectReq) -> dict:
    """Publish a staged topic straight into the live agent via Dataverse Web API."""
    from pathlib import Path
    import json as _json
    src = topics_mod.staging_dir() / req.topic_id
    if not src.exists():
        raise HTTPException(404, f"no such proposed topic: {req.topic_id}")
    yaml_path = src / "topic.yaml"
    if not yaml_path.exists():
        raise HTTPException(400, f"topic.yaml not found in {src}")
    topic_yaml = yaml_path.read_text(encoding="utf-8")
    meta_p = src / "meta.json"
    meta = _json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    name = (meta.get("display_name") or req.topic_id).strip()
    description = (meta.get("scenario_prompt") or "").strip()
    res = await dv_mod.create_topic(
        name=name,
        description=description,
        topic_yaml=topic_yaml,
        bot_schema=req.bot_schema,
    )
    if res.get("ok"):
        # Mark as applied so the UI shows the badge
        try:
            meta["applied"] = True
            from time import time as _now
            meta["applied_at"] = _now()
            meta["applied_to"] = "dataverse: " + (res.get("location") or "")
            meta["botcomponentid"] = res.get("botcomponentid")
            meta_p.write_text(_json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass
    return res


# ---------- API: register custom templates --------------------------------

class RegisterTemplateReq(BaseModel):
    key: str
    name: str
    system: str = "generic"
    sample_url: str = ""
    schema_name: str | None = None
    description: str = ""
    trigger_keywords: list[str] | str = []
    notes: str = ""


@app.post("/api/topics/templates/register")
async def register_template(req: RegisterTemplateReq) -> dict:
    return topics_mod.register_custom_template(req.model_dump())


@app.delete("/api/topics/templates/{key}")
async def unregister_template(key: str) -> dict:
    return topics_mod.delete_custom_template(key)


# ---------- API: real publish via pac CLI ---------------------------------

from ess_maker.publish import pac as pac_mod
from ess_maker.publish import dataverse as dv_mod


class PacPublishReq(BaseModel):
    target_solution_subdir: str | None = None  # default: HR working solution
    publish: bool = True


def _repo_root() -> Path:
    # webapp/__init__.py → webapp → ess_maker → src → ess-maker → repo
    return Path(__file__).resolve().parents[4]


@app.get("/api/pac/status")
async def pac_status_endpoint() -> dict:
    return await pac_mod.pac_status()


@app.post("/api/pac/publish")
async def pac_publish(req: PacPublishReq) -> dict:
    """Pack the HR solution and import it into the active environment."""
    repo = _repo_root()
    if req.target_solution_subdir:
        src_dir = repo / req.target_solution_subdir
    else:
        src_dir = pac_mod.default_solution_dir(repo)
    out_zip = settings.ess_maker_workdir / "publish" / f"{src_dir.name}.zip"
    return await pac_mod.pack_and_import(
        solution_src_dir=src_dir,
        out_zip=out_zip,
        environment_id=settings.mcs_env_id or None,
        publish=req.publish,
    )


class SolutionInitReq(BaseModel):
    solution_name: str
    publisher_prefix: str


@app.post("/api/pac/solution-init")
async def pac_solution_init(req: SolutionInitReq) -> dict:
    """Scaffold a new unmanaged customisation solution under .workdir/solutions/<name>/Solution."""
    name = (req.solution_name or "").strip()
    prefix = (req.publisher_prefix or "").strip()
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "", name)
    if not safe_name:
        return {"ok": False, "error": "solution name must contain alphanumerics"}
    repo = _repo_root()
    target_rel = Path(".workdir") / "solutions" / safe_name / "Solution"
    out_dir = settings.ess_maker_workdir / "solutions" / safe_name / "Solution"
    res = await pac_mod.init_unmanaged_solution(out_dir, safe_name, prefix)
    if not res.get("ok"):
        return res
    # Compute the path relative to repo root for the UI's target_solution_subdir field.
    try:
        rel = out_dir.resolve().relative_to(repo.resolve())
        target_sub = str(rel).replace("\\", "/")
    except Exception:
        target_sub = str(target_rel).replace("\\", "/")
    return {
        "ok": True,
        "path": res["path"],
        "target_solution_subdir": target_sub,
    }


# ---------- API: SharePoint knowledge sources ----------

from ess_maker import knowledge as kb_mod


class ScaffoldKbReq(BaseModel):
    name: str
    description: str = ""
    url: str
    priority: int = 1


class ApplyKbReq(BaseModel):
    kb_id: str
    target_solution_subdir: str | None = None


class PublishKbReq(BaseModel):
    name: str
    description: str = ""
    url: str
    priority: int = 1
    publish: bool = True  # whether to also call pac pack+import
    target_solution_subdir: str | None = None  # repo-relative path to an unmanaged solution folder


from dataclasses import asdict as _asdict


@app.post("/api/knowledge/scaffold")
async def scaffold_kb(req: ScaffoldKbReq) -> dict:
    res = kb_mod.scaffold_kb_source(
        name=req.name.strip(),
        description=(req.description or "").strip(),
        url=req.url.strip(),
        priority=int(req.priority or 1),
    )
    return _asdict(res)


@app.get("/api/knowledge/proposed")
async def list_proposed_kb() -> dict:
    return {"items": kb_mod.list_proposed()}


@app.post("/api/knowledge/apply")
async def apply_kb(req: ApplyKbReq) -> dict:
    return kb_mod.apply_kb_to_solution(req.kb_id, req.target_solution_subdir)


@app.post("/api/knowledge/publish")
async def publish_kb(req: PublishKbReq) -> dict:
    """One-shot: scaffold → apply → pac publish (if requested)."""
    sc = kb_mod.scaffold_kb_source(
        name=req.name.strip(),
        description=(req.description or "").strip(),
        url=req.url.strip(),
        priority=int(req.priority or 1),
    )
    if not sc.ok:
        return {"ok": False, "stage": "scaffold", "error": sc.error}
    ap = kb_mod.apply_kb_to_solution(sc.kb_id, req.target_solution_subdir)
    if not ap.get("ok"):
        return {"ok": False, "stage": "apply", "error": ap.get("error"), "scaffold": _asdict(sc)}
    out: dict = {"ok": True, "scaffold": _asdict(sc), "apply": ap}
    if req.publish:
        if not req.target_solution_subdir:
            out["publish"] = {
                "ok": False,
                "step": "validate target",
                "error": (
                    "`Also pack & import` is ticked but no target solution folder is set. "
                    "The default HR solution is managed/first-party and can't be re-imported."
                ),
                "hint": (
                    "Open Advanced \u2192 set a name + prefix \u2192 click Create unmanaged solution. "
                    "The folder will auto-fill, then click Generate & apply again."
                ),
                "solution": {"blocked": True},
            }
            out["ok"] = False
            return out
        repo = _repo_root()
        src_dir = repo / req.target_solution_subdir
        out_zip = settings.ess_maker_workdir / "publish" / f"{src_dir.name}.zip"
        pub = await pac_mod.pack_and_import(
            solution_src_dir=src_dir,
            out_zip=out_zip,
            environment_id=settings.mcs_env_id or None,
            publish=True,
        )
        out["publish"] = pub
        out["ok"] = bool(pub.get("ok"))
    return out


# ---------- API: direct Dataverse Web API publish (no solution needed) ----

class PublishKbDirectReq(BaseModel):
    name: str
    description: str = ""
    url: str
    bot_schema: str | None = None  # override settings.mcs_bot_schema


@app.get("/api/dataverse/status")
async def dataverse_status() -> dict:
    """Diagnostics: org URL resolution, token, bot lookup."""
    return await dv_mod.status()


@app.post("/api/knowledge/publish-direct")
async def publish_kb_direct(req: PublishKbDirectReq) -> dict:
    """Create a SharePoint knowledge source on the live agent via Dataverse Web API.

    Bypasses pac solution pack/import. Result is visible in Copilot Studio's
    Knowledge tab immediately; the maker still has to click Publish on the
    agent itself for the change to reach live chat.
    """
    return await dv_mod.create_sharepoint_kb(
        name=req.name,
        description=req.description,
        sharepoint_url=req.url,
        bot_schema=req.bot_schema,
    )


# ---------- API: Maker Copilot (agentic side dock) ----------
#
# Rule-based / function-calling chat surface so the maker can drive ESS Maker
# from a chat panel instead of the form UX. When AOAI is configured, the same
# tools can be exposed as OpenAI function-calling tools (TODO). Until then we
# parse simple imperative phrases and route to the same backend functions the
# UI already uses — so behavior is identical regardless of how it's invoked.

class MakerChatReq(BaseModel):
    message: str
    context: dict | None = None  # e.g. {"current_tab": "results", "selected_idx": 3}


def _maker_help() -> str:
    return (
        "Try one of these:\n"
        "• **make a topic for `<scenario>`** — e.g. *make a topic for what is my leave balance*\n"
        "• **list topic templates** — show every Workday sample I can scaffold from\n"
        "• **list proposed topics** — show topics I've staged but not applied\n"
        "• **apply topic `<topic_id>`** — copy a staged topic into the solution folder\n"
        "• **run eval `<file>`** — kick off the evaluator on a scenarios file\n"
        "• **summarize results** — failure modes & latency from the latest run\n"
        "• **diagnose `<conversation_id>`** — pull the real error from App Insights\n"
        "• **add scenario `<prompt>`** — append a smoke-test scenario\n"
        "• **suggest knowledge** / **suggest instruction** — generate fixes from the latest run\n"
        "• **help** — this message"
    )


def _summarize_results_text() -> str:
    p = settings.ess_maker_workdir / "eval-runs" / "latest.json"
    if not p.exists():
        return "No eval run yet. Use **run eval `<file.yaml>`**."
    report = json.loads(p.read_text(encoding="utf-8"))
    items = report["items"]
    if not items:
        return "Latest run has no items."
    pass_count = sum(1 for i in items if _is_pass(i))
    fm = Counter(i["score"]["failure_mode"] for i in items)
    lat = sorted(i["result"]["latency_ms"] for i in items)
    p50 = lat[len(lat)//2]
    p95 = lat[min(len(lat)-1, int(len(lat)*0.95))]
    lines = [
        f"**Latest eval:** {pass_count}/{len(items)} passed ({pass_count/len(items):.0%})",
        f"Latency p50 / p95: {p50} ms / {p95} ms",
        "Failure modes: " + ", ".join(f"{k}={v}" for k, v in fm.most_common()) if fm else "",
    ]
    return "\n".join(l for l in lines if l)


_SCENARIOS_SMOKE = Path("prompts") / "scenarios.smoke3.yaml"


def _append_smoke_scenario(prompt: str) -> str:
    if not _SCENARIOS_SMOKE.exists():
        return f"Can't find {_SCENARIOS_SMOKE}."
    sid = "maker-" + uuid.uuid4().hex[:8]
    block = (
        f"\n- id: {sid}\n"
        f"  category: HR\n"
        f"  prompt: {prompt}\n"
        f"  expected_assertions: []\n"
        f"  tags:\n"
        f"  - smoke\n"
        f"  - maker-added\n"
    )
    with _SCENARIOS_SMOKE.open("a", encoding="utf-8") as f:
        f.write(block)
    return f"Added scenario `{sid}` to `{_SCENARIOS_SMOKE.name}`."


@app.post("/api/maker-chat")
async def maker_chat(req: MakerChatReq) -> dict:
    """Single-turn chat. Returns text + optional 'tool_calls' the UI shows as preview cards."""
    msg = (req.message or "").strip()
    low = msg.lower()
    if not msg:
        return {"text": _maker_help(), "tool_calls": []}

    # ---- routing ----
    if low in ("help", "?", "what can you do"):
        return {"text": _maker_help(), "tool_calls": []}

    if low.startswith("list topic templates") or low == "templates":
        tpls = topics_mod.list_templates()
        bullets = "\n".join(f"• `{t['key']}` — {t['name']}" for t in tpls)
        return {"text": f"**Available topic templates ({len(tpls)}):**\n{bullets}", "tool_calls": []}

    if low.startswith("list proposed") or low == "proposed":
        items = topics_mod.list_proposed()
        if not items:
            return {"text": "No staged topics yet. Try **make a topic for `<scenario>`**.", "tool_calls": []}
        bullets = "\n".join(
            f"• `{i['topic_id']}` — {i.get('display_name','')}  "
            f"({'applied' if i.get('applied') else 'staged'})"
            for i in items
        )
        return {"text": f"**Proposed topics:**\n{bullets}", "tool_calls": []}

    m = re.match(r"(?:make|create|scaffold|generate)\s+(?:a\s+)?topic\s+(?:for|from)\s+(.+)", low)
    if m:
        scenario_text = msg[m.start(1):].strip().strip('"').strip("'")
        res = topics_mod.scaffold_from_scenario(prompt=scenario_text)
        from dataclasses import asdict
        if not res.ok:
            tip = "Add a new entry to `ess_maker/topics/__init__.py:WORKDAY_TEMPLATES` if your scenario isn't covered."
            return {"text": f"❌ Couldn't match a template for *{scenario_text}*.\n\n{tip}", "tool_calls": []}
        return {
            "text": (
                f"✅ Drafted topic **{res.template_name}** (confidence {res.confidence:.0%}).\n"
                f"Reference: {res.sample_url}\n\n"
                f"Review the staged files below, then say "
                f"**apply topic {res.topic_id}** to copy them into the solution."
            ),
            "tool_calls": [{
                "tool": "scaffold_topic",
                "result": asdict(res),
            }],
        }

    m = re.match(r"apply\s+topic\s+([\w\-\.]+)", low)
    if m:
        tid = m.group(1)
        res = topics_mod.apply_to_solution(tid)
        if not res.get("ok"):
            return {"text": f"❌ {res.get('error','apply failed')}", "tool_calls": []}
        return {
            "text": (
                f"✅ Copied **{tid}** to `{res['applied_to']}`.\n"
                "Next: pack the solution with `pac solution pack` and import into your environment, "
                "or open Copilot Studio and import the topic YAML directly."
            ),
            "tool_calls": [{"tool": "apply_topic", "result": res}],
        }

    m = re.match(r"diagnose\s+([\w\-]+)", low)
    if m:
        conv = m.group(1)
        if not ai_diag.is_configured():
            return {"text": "App Insights isn't configured yet. Open **Setup → Runtime diagnostics** to add the connection string.", "tool_calls": []}
        diag = await ai_diag.fetch_errors_for_conversation(conv)
        if not diag.get("ok") or not diag.get("rows"):
            return {"text": f"No App Insights events found for `{conv}`.", "tool_calls": [{"tool": "diagnose", "result": diag}]}
        s = diag["summary"]
        return {
            "text": f"**Conversation `{conv}`**\n{s['headline']}\n\n*Fix:* {s.get('fix_hint','(no auto-fix)')}",
            "tool_calls": [{"tool": "diagnose", "result": diag}],
        }

    if low.startswith("summarize") or low.startswith("results") or low == "summary":
        return {"text": _summarize_results_text(), "tool_calls": []}

    m = re.match(r"add\s+scenario\s+(.+)", low)
    if m:
        scenario_prompt = msg[m.start(1):].strip().strip('"').strip("'")
        return {"text": _append_smoke_scenario(scenario_prompt), "tool_calls": []}

    m = re.match(r"run\s+eval(?:\s+(\S+))?", low)
    if m:
        path = m.group(1) or "prompts/scenarios.smoke3.yaml"
        # Don't actually start the job from chat — surface a click-through so
        # the UI's progress stream stays the source of truth. Less surprising.
        return {
            "text": f"To run the eval on `{path}`, switch to **3. Run Eval** and click Start. (Triggering jobs from chat wired up next.)",
            "tool_calls": [{"tool": "navigate", "result": {"tab": "run", "scenarios_path": path}}],
        }

    if low in ("suggest knowledge", "suggest knowledge changes"):
        agent = _read_json("agent.json", AgentConfig().model_dump())
        p = settings.ess_maker_workdir / "eval-runs" / "latest.json"
        if not p.exists():
            return {"text": "Run an eval first.", "tool_calls": []}
        report = json.loads(p.read_text(encoding="utf-8"))
        fails = [i for i in report["items"] if not _is_pass(i)]
        return {"text": _suggest_knowledge_changes(agent, fails), "tool_calls": []}

    if low in ("suggest instruction", "suggest instructions"):
        agent = _read_json("agent.json", AgentConfig().model_dump())
        p = settings.ess_maker_workdir / "eval-runs" / "latest.json"
        if not p.exists():
            return {"text": "Run an eval first.", "tool_calls": []}
        report = json.loads(p.read_text(encoding="utf-8"))
        fails = [i for i in report["items"] if not _is_pass(i)]
        return {"text": _suggest_instruction(agent, fails), "tool_calls": []}

    # ---- fallback ----
    return {
        "text": (
            f"I didn't understand `{msg}`. "
            "Type **help** to see what I can do.\n\n"
            "Tip: try `make a topic for what is my leave balance`."
        ),
        "tool_calls": [],
    }
