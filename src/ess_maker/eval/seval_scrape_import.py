"""Importer for SEVAL "Scrape Data & Conversation" exports.

Each JSON file in `scraping_raw_data_output/` is one (query, exp_name) pair with shape:
  {
    "conversation_id": "...",
    "query": {"id": "<the user prompt>", "segment": "...", "query_hash": "..."},
    "exp_name": "control" | "experiment",
    "requests": [
       {
         "utterance": "...",
         "result": {"value": {"message": "<bot reply>", ...}, "value": "Success"|...}
       }
    ]
  }

We deduplicate by query_hash and keep one Scenario per unique prompt, capturing the original
SEVAL conversation_ids and (optionally) the original bot reply as a baseline.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ess_maker.eval.schemas import Scenario


def _category_from_segment(segment: str) -> str:
    s = (segment or "").lower()
    if "hr" in s:
        return "HR"
    if "it" in s:
        return "IT"
    return "Mixed"


def _extract_message(req: dict) -> str:
    """SEVAL's result.value can be a dict ({'message': '...'}) or a string status. Return text."""
    res = req.get("result") or {}
    val = res.get("value")
    if isinstance(val, dict):
        return str(val.get("message") or "")
    if isinstance(val, str) and "message" in res:
        return str(res.get("message") or "")
    return ""


def from_scrape_dir(
    scrape_dir: str | Path,
    *,
    keep_baseline_replies: bool = True,
    only_exp_name: str | None = "control",
) -> list[Scenario]:
    """Walk a SEVAL scrape directory and return deduplicated Scenarios.

    `only_exp_name`: if set, only include records from that experiment leg.
    """
    root = Path(scrape_dir)
    if not root.exists():
        raise FileNotFoundError(root)

    by_hash: dict[str, dict[str, Any]] = {}
    for f in sorted(root.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        exp = data.get("exp_name")
        if only_exp_name and exp != only_exp_name:
            continue
        q = data.get("query") or {}
        prompt = q.get("id")
        if not prompt:
            continue
        qhash = q.get("query_hash") or prompt
        segment = q.get("segment") or ""
        baseline_reply = ""
        if keep_baseline_replies and data.get("requests"):
            baseline_reply = _extract_message(data["requests"][0])

        existing = by_hash.get(qhash)
        if existing is None:
            by_hash[qhash] = {
                "prompt": prompt,
                "segment": segment,
                "conversation_ids": [data.get("conversation_id")],
                "baseline_reply": baseline_reply,
                "exp_names": [exp] if exp else [],
            }
        else:
            existing["conversation_ids"].append(data.get("conversation_id"))
            if exp:
                existing["exp_names"].append(exp)

    scenarios: list[Scenario] = []
    for i, (qhash, info) in enumerate(by_hash.items(), start=1):
        tags = ["seval", f"segment:{info['segment']}"]
        if info["exp_names"]:
            tags.append(f"seval_exp:{','.join(sorted(set(info['exp_names'])))}")
        scenarios.append(
            Scenario(
                id=f"seval-{qhash[:10]}",
                category=_category_from_segment(info["segment"]),
                prompt=info["prompt"],
                expected_assertions=(
                    [f"BASELINE (SEVAL control reply, for reference only):\n{info['baseline_reply']}"]
                    if info["baseline_reply"]
                    else []
                ),
                tags=tags,
            )
        )
    return scenarios


def attach_leo_metrics(
    scenarios: list[Scenario],
    sbsleo_details_csv: str | Path,
) -> list[Scenario]:
    """Best-effort: attach SEVAL LEO per-query scores to scenario tags so the analyzer can
    compare our verdict against SEVAL's. The CSV is huge; we read it streaming.
    """
    import csv

    path = Path(sbsleo_details_csv)
    if not path.exists():
        return scenarios

    by_prompt: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            prompt = row.get("query") or row.get("utterance") or row.get("query_id")
            if not prompt:
                continue
            by_prompt.setdefault(prompt, {}).update(
                {
                    k: row[k]
                    for k in (
                        "Accuracy",
                        "Completeness",
                        "Relevance",
                        "Usefulness",
                        "Conversationality",
                    )
                    if k in row and row[k]
                }
            )

    for s in scenarios:
        scores = by_prompt.get(s.prompt)
        if not scores:
            continue
        summary = ", ".join(f"{k}={v}" for k, v in scores.items())
        s.tags.append(f"leo:{summary}")
    return scenarios
