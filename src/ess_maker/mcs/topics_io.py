"""Read & patch Copilot Studio topic YAML files (used when working off vendored samples)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def read_topic(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def write_topic(path: str | Path, data: dict[str, Any]) -> Path:
    p = Path(path)
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return p


def apply_patch(topic: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a suggester patch JSON to a topic dict (best-effort, schema-tolerant)."""
    if "new_description" in patch:
        topic["description"] = patch["new_description"]
    if "new_trigger_phrases" in patch:
        topic.setdefault("trigger", {}).setdefault("intent", {})["examples"] = [
            {"text": x} for x in patch["new_trigger_phrases"]
        ]
    for change in patch.get("node_changes", []) or []:
        nid = change.get("node_id")
        if not nid:
            continue
        for node in _iter_nodes(topic):
            if node.get("id") == nid:
                node["_pendingChange"] = change.get("change")
                node["_rationale"] = change.get("rationale")
    return topic


def _iter_nodes(topic: dict[str, Any]):
    stack = [topic]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "id" in cur and "kind" in cur:
                yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
