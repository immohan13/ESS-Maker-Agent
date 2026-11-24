"""Customer-owned evidence: Purview audit exports and developer-mode diagnostic logs."""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from typing import Any

_WANTED = ("AgentName", "AgentId", "AgentVersion", "ModelName", "ModelProvider", "AppHost", "ThreadId")


def _walk(obj: Any, found: dict[str, list]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _WANTED and isinstance(v, (str, int, float)):
                found.setdefault(k, []).append(str(v))
            elif k == "AccessedResources" and isinstance(v, list):
                found.setdefault("AccessedResources", []).extend(v)
            elif k == "AISystemPlugin" and isinstance(v, list):
                found.setdefault("AISystemPlugin", []).extend(v)
            _walk(v, found)
    elif isinstance(obj, list):
        for x in obj:
            _walk(x, found)


def parse_purview_csv(text: str, agent_filter: str = "") -> dict:
    """Parse a Purview audit search export (CSV with an AuditData JSON column).

    Groups Copilot interactions by AgentVersion so the maker can see when behavior changed.
    """
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"ok": False, "error": "Empty file."}
    col = next((c for c in rows[0] if c and c.lower() == "auditdata"), None)
    if not col:
        return {"ok": False, "error": "No AuditData column. Export from Purview > Audit > Export results."}

    by_version: dict[str, dict] = defaultdict(lambda: {
        "interactions": 0, "first": None, "last": None, "resources": Counter(), "plugins": Counter(),
        "models": Counter(), "hosts": Counter(),
    })
    agents: Counter = Counter()
    skipped = 0
    for r in rows:
        try:
            data = json.loads(r[col])
        except (json.JSONDecodeError, TypeError):
            skipped += 1
            continue
        if "Copilot" not in str(data.get("Operation", "")) + str(data.get("RecordType", "")) + str(r.get("Operations", "")):
            continue
        found: dict[str, list] = {}
        _walk(data, found)
        name = (found.get("AgentName") or [""])[0]
        agent_id = (found.get("AgentId") or [""])[0]
        if agent_filter and agent_filter.lower() not in (name + " " + agent_id).lower():
            continue
        agents[name or agent_id or "(no agent)"] += 1
        ver = (found.get("AgentVersion") or ["(unknown)"])[0]
        when = data.get("CreationTime") or r.get("CreationDate") or ""
        v = by_version[ver]
        v["interactions"] += 1
        v["first"] = min(filter(None, [v["first"], when])) if v["first"] else when
        v["last"] = max(filter(None, [v["last"], when])) if v["last"] else when
        for res in found.get("AccessedResources", []):
            if isinstance(res, dict):
                v["resources"][res.get("SiteUrl") or res.get("Name") or res.get("Type") or "resource"] += 1
        for p in found.get("AISystemPlugin", []):
            if isinstance(p, dict):
                v["plugins"][p.get("Name") or p.get("Id") or "plugin"] += 1
        for m in found.get("ModelName", []):
            v["models"][m] += 1
        for h in found.get("AppHost", []):
            v["hosts"][h] += 1

    versions = []
    for ver, v in sorted(by_version.items(), key=lambda kv: kv[1]["first"] or ""):
        versions.append({
            "version": ver, "interactions": v["interactions"], "first": v["first"], "last": v["last"],
            "top_resources": v["resources"].most_common(8), "plugins": v["plugins"].most_common(8),
            "models": v["models"].most_common(4), "hosts": v["hosts"].most_common(4),
            "avg_resources_per_turn": round(sum(v["resources"].values()) / max(v["interactions"], 1), 2),
        })
    return {"ok": True, "rows": len(rows), "skipped": skipped, "agents": agents.most_common(10), "versions": versions}


_DEV_PATTERNS = {
    "functions_selected": re.compile(r"(matched|selected)\s+functions?\s*[:\-]\s*(.+)", re.I),
    "no_functions": re.compile(r"no functions? selected for execution", re.I),
    "function_status": re.compile(r"(function|action)\s*[:\-]?\s*([\w.\-]+).*?status\s*[:\-]\s*(\w+)", re.I),
    "latency": re.compile(r"latency\s*[:\-]\s*(\d+)\s*ms", re.I),
    "knowledge": re.compile(r"(knowledge|capabilit(y|ies)|enterprise data)\s*[:\-]\s*(.+)", re.I),
    "http": re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(https?://\S+)", re.I),
    "error": re.compile(r"\b(error|failed|failure|exception)\b.*", re.I),
    "conversation": re.compile(r"conversation\s*id\s*[:\-]\s*([0-9a-f-]{36})", re.I),
}


def parse_devmode_log(text: str) -> dict:
    """Tolerant parser for the M365 Copilot developer-mode diagnostic log (-developer on).

    The log format isn't a stable contract, so this extracts recognizable signals and returns the
    unmatched remainder for display rather than guessing.
    """
    out: dict[str, list] = {k: [] for k in _DEV_PATTERNS}
    unmatched = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        hit = False
        for key, rx in _DEV_PATTERNS.items():
            m = rx.search(line)
            if m:
                out[key].append(line[:300])
                hit = True
        if not hit:
            unmatched += 1
    diagnosis = []
    if out["no_functions"]:
        diagnosis.append("Orchestrator selected no functions: the action/topic descriptions don't match the prompt's "
                         "intent, or an instruction rule steered away from tools.")
    if out["error"]:
        diagnosis.append("Errors were reported during execution; check the listed function status lines.")
    if out["knowledge"] and not out["functions_selected"]:
        diagnosis.append("Knowledge capabilities were used without actions; confirm that's expected for this prompt.")
    return {"ok": True, "signals": {k: v[:40] for k, v in out.items()}, "unmatched_lines": unmatched,
            "diagnosis": diagnosis}
