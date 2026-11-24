"""Counterfactual evaluation: prove which instruction change caused which regression.

Variants: previous (known good), current, and current-minus-change_i for each changed block. Each
variant's instructions are swapped onto a TEST agent, published, and the prompts replayed through
the Copilot Studio client. A prompt is attributed to change_i when it fails on current, passes on
current-minus-change_i, and passes on previous.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ess_maker.doctor.agent_file import AgentDoc
from ess_maker.doctor.blocks import diff_docs, segment
from ess_maker.settings import settings

RUNS = Path(settings.workdir) / "doctor" / "runs"

_REFUSAL = re.compile(
    r"couldn['’]t find|could not find|can['’]t help|cannot help|unable to (help|find|answer)|not able to|"
    r"i don['’]t have (that|information)|no (relevant|information)|outside (of )?my scope|can['’]t provide",
    re.I,
)
_ERROR = re.compile(r"something (went|has gone) wrong|unexpected error|an error occurred|sorry, (i|something)", re.I)
_OFFER = re.compile(r"(would you like|want me to|shall i|i can (create|open|submit|start)|do you want me)", re.I)
_CLARIFY = re.compile(r"\?\s*$|could you (tell|clarify|confirm)|which (one|of)", re.I)


def classify_reply(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "empty"
    if _ERROR.search(t[:200]):
        return "error"
    if _REFUSAL.search(t) and len(t) < 600:
        return "refusal"
    if _OFFER.search(t):
        return "answer+offer"
    if _CLARIFY.search(t) and len(t) < 400:
        return "clarification"
    return "answer"


def passes(label: str, expect: str = "answer") -> bool:
    if expect == "offer":
        return label == "answer+offer"
    if expect == "refusal":
        return label == "refusal"
    return label in ("answer", "answer+offer", "clarification")


def _revert_block(current: AgentDoc, previous: AgentDoc, block: dict) -> str:
    """Instructions = current with one changed block restored to previous."""
    cur_lines = [ln.text for ln in current.instructions]
    first = current.instructions[0].no if current.instructions else 0
    prev_blocks = {(b.start, b.end): b for b in segment(previous)}
    if block["status"] == "added":
        s, e = block["new"]
        return "\n".join(cur_lines[: s - first] + cur_lines[e - first + 1:])
    if block["status"] == "modified":
        s, e = block["new"]
        pb = prev_blocks.get(tuple(block["old"]))
        old_text = [ln.text for ln in pb.lines] if pb else []
        return "\n".join(cur_lines[: s - first] + old_text + cur_lines[e - first + 1:])
    if block["status"] == "removed":
        pb = prev_blocks.get(tuple(block["old"]))
        old_text = [ln.text for ln in pb.lines] if pb else []
        return "\n".join(cur_lines + [""] + old_text)
    return current.instruction_text


def build_variants(previous: AgentDoc, current: AgentDoc, max_changes: int = 6) -> list[dict]:
    d = diff_docs(previous, current)
    changed = [b for b in d["blocks"] if b["status"] != "unchanged"][:max_changes]
    variants = [
        {"id": "previous", "label": "Previous (known good)", "instructions": previous.instruction_text},
        {"id": "current", "label": "Current", "instructions": current.instruction_text},
    ]
    for k, b in enumerate(changed, 1):
        variants.append({
            "id": f"minus_{k}", "label": f"Current without change #{k}: {b['status']} '{b['title'][:60]}'",
            "change": {"n": k, "status": b["status"], "title": b["title"], "old": b.get("old"), "new": b.get("new")},
            "instructions": _revert_block(current, previous, b),
        })
    return variants


async def run_live(variants: list[dict], prompts: list[dict], repeats: int = 1, progress=None) -> dict:
    """Swap each variant onto the configured TEST agent, publish, and replay prompts."""
    from ess_maker.doctor import live
    from ess_maker.eval.cs_client import CopilotStudioClient

    info = await live.load_live()
    original = info["data"]
    results: dict[str, list[dict]] = {}
    try:
        for v in variants:
            if progress:
                await progress(f"Publishing variant: {v['label']}")
            await live.swap_and_publish(info["bot_id"], info["component_id"],
                                        live.replace_instructions(original, v["instructions"]))
            rows = []
            for p in prompts:
                for r in range(repeats):
                    client = CopilotStudioClient()
                    try:
                        conv = await client.start()
                        await client.send(conv, p["prompt"])
                        turns, _ = await client.poll_reply(conv, max_wait_s=60)
                    finally:
                        await client.aclose()
                    text = "\n".join(t.text for t in turns)
                    label = classify_reply(text)
                    rows.append({"prompt": p["prompt"], "expect": p.get("expect", "answer"), "repeat": r,
                                 "label": label, "pass": passes(label, p.get("expect", "answer")),
                                 "reply": text[:1200], "conversation_id": conv.conversation_id})
                    if progress:
                        await progress(f"{v['id']}: {p['prompt'][:50]} -> {label}")
            results[v["id"]] = rows
    finally:
        if progress:
            await progress("Restoring original instructions")
        await live.swap_and_publish(info["bot_id"], info["component_id"], original, wait_s=5)
    return save_run(variants, prompts, results)


def attribute(variants: list[dict], results: dict[str, list[dict]]) -> list[dict]:
    def rate(vid: str, prompt: str) -> float:
        rows = [r for r in results.get(vid, []) if r["prompt"] == prompt]
        return sum(r["pass"] for r in rows) / len(rows) if rows else float("nan")

    prompts = sorted({r["prompt"] for rows in results.values() for r in rows})
    out = []
    for p in prompts:
        cur, prev = rate("current", p), rate("previous", p)
        row: dict[str, Any] = {"prompt": p, "previous": prev, "current": cur, "regressed": prev > cur, "causes": []}
        if prev > cur:
            for v in variants:
                if v["id"].startswith("minus_") and rate(v["id"], p) > cur:
                    row["causes"].append({"change": v["change"], "pass_rate_without": rate(v["id"], p)})
        out.append(row)
    return out


def save_run(variants: list[dict], prompts: list[dict], results: dict) -> dict:
    RUNS.mkdir(parents=True, exist_ok=True)
    run = {
        "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "variants": [{k: v for k, v in x.items() if k != "instructions"} for x in variants],
        "prompts": prompts, "results": results, "attribution": attribute(variants, results),
        "created": time.time(),
    }
    (RUNS / f"{run['id']}.json").write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
    return run


def list_runs() -> list[dict]:
    if not RUNS.exists():
        return []
    out = []
    for p in sorted(RUNS.glob("*.json"), reverse=True):
        j = json.loads(p.read_text(encoding="utf-8"))
        out.append({"id": j["id"], "variants": len(j["variants"]), "prompts": len(j["prompts"]),
                    "regressions": sum(1 for a in j["attribution"] if a["regressed"])})
    return out


def load_run(run_id: str) -> dict:
    return json.loads((RUNS / f"{run_id}.json").read_text(encoding="utf-8"))
