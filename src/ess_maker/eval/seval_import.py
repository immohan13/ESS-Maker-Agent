"""Import SEVAL job utterances + response assertions into our Scenario schema.

Two entry points:
  - from_json_file(path): for a JSON exported from the SEVAL job page (Download button).
  - from_seval_api(job_id, cookie): pulls live via the SEVAL site cookie the user pastes.

Both are tolerant of multiple SEVAL export shapes: we look for keys like
"utterance"/"prompt"/"query" and "assertions"/"expectedResponse"/"groundTruth".
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml

from ess_maker.eval.schemas import Scenario


_PROMPT_KEYS = ("utterance", "prompt", "query", "input", "userInput", "question")
_ASSERTION_KEYS = (
    "assertions",
    "responseAssertions",
    "expectedAssertions",
    "expected",
    "groundTruth",
    "expectedResponse",
)
_TOPIC_KEYS = ("expectedTopic", "topic", "intent")
_KS_KEYS = ("expectedKnowledgeSource", "knowledgeSource", "source")
_CATEGORY_KEYS = ("category", "scenarioCategory", "domain", "tag")
_ID_KEYS = ("id", "scenarioId", "name", "testId")


def _first(d: dict, keys: Iterable[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
        # case-insensitive fallback
        for actual in d:
            if actual.lower() == k.lower():
                if d[actual] not in (None, "", []):
                    return d[actual]
    return default


def _normalize_assertions(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        out = []
        for item in val:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                txt = _first(item, ("text", "assertion", "description", "value"))
                if txt:
                    out.append(str(txt))
        return out
    if isinstance(val, dict):
        return [str(v) for v in val.values() if v]
    return [str(val)]


def _category(raw: Any) -> str:
    s = str(raw or "").lower()
    if "it" in s and "hr" not in s:
        return "IT"
    if "hr" in s:
        return "HR"
    return "Mixed"


def _slugify(text: str, idx: int) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")[:40]
    return f"seval-{idx:04d}-{s}" if s else f"seval-{idx:04d}"


def _walk_records(obj: Any) -> Iterable[dict]:
    """Find dicts that look like a scenario record anywhere in a SEVAL JSON."""
    if isinstance(obj, dict):
        # If this dict already has a prompt-like key, yield it.
        if any(k in obj or any(a.lower() == k for a in obj) for k in _PROMPT_KEYS):
            yield obj
        for v in obj.values():
            yield from _walk_records(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_records(item)


def to_scenarios(records: Iterable[dict]) -> list[Scenario]:
    seen_ids: set[str] = set()
    out: list[Scenario] = []
    for i, r in enumerate(records, start=1):
        prompt = _first(r, _PROMPT_KEYS)
        if not prompt:
            continue
        rid = str(_first(r, _ID_KEYS) or _slugify(str(prompt), i))
        base = rid
        n = 1
        while rid in seen_ids:
            n += 1
            rid = f"{base}-{n}"
        seen_ids.add(rid)
        out.append(
            Scenario(
                id=rid,
                category=_category(_first(r, _CATEGORY_KEYS)),
                prompt=str(prompt),
                expected_topic=(_first(r, _TOPIC_KEYS) or None),
                expected_knowledge_source=(_first(r, _KS_KEYS) or None),
                expected_assertions=_normalize_assertions(_first(r, _ASSERTION_KEYS)),
                tags=["seval"],
            )
        )
    return out


def from_json_file(path: str | Path) -> list[Scenario]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return to_scenarios(list(_walk_records(raw)))


def from_seval_api(job_id: str | int, cookie: str, *, base_url: str = "https://seval.microsoft.com") -> list[Scenario]:
    """Best-effort fetch from the SEVAL job results endpoint using a browser session cookie.

    The SEVAL site requires AAD auth; the easiest way is to copy your `Cookie:` header from
    DevTools -> Network -> any /api/* request on the job page, and pass it here.
    """
    url = f"{base_url.rstrip('/')}/api/jobs/{job_id}/results"
    headers = {"Cookie": cookie, "Accept": "application/json"}
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    return to_scenarios(list(_walk_records(data)))


def write_scenarios_yaml(scenarios: list[Scenario], out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"scenarios": [s.model_dump(exclude_none=True) for s in scenarios]}
    p.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return p
