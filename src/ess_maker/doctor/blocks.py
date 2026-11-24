"""Segment instructions into heading-delimited blocks and diff them."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ess_maker.doctor.agent_file import AgentDoc, Line

_HEADING = re.compile(r"^\s*(#{1,4})\s*([^#].*?)\s*$|^\s*<([A-Za-z_]+)>\s*$")


@dataclass
class Block:
    title: str
    start: int
    end: int
    lines: list[Line] = field(default_factory=list)

    @property
    def key(self) -> str:
        return re.sub(r"[^a-z0-9]+", " ", self.title.lower()).strip()

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


def segment(doc: AgentDoc) -> list[Block]:
    blocks: list[Block] = []
    cur: Block | None = None
    for ln in doc.instructions:
        m = _HEADING.match(ln.text)
        if m:
            title = (m.group(2) or m.group(3) or "").strip()
            cur = Block(title, ln.no, ln.no, [ln])
            blocks.append(cur)
            continue
        if cur is None:
            cur = Block("(preamble)", ln.no, ln.no, [])
            blocks.append(cur)
        cur.lines.append(ln)
        cur.end = ln.no
    return blocks


def block_range(doc: AgentDoc, line_no: int) -> tuple[int, int, str]:
    for b in segment(doc):
        if b.start <= line_no <= b.end:
            return b.start, b.end, b.title
    return line_no, line_no, ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def line_in(doc: AgentDoc, text: str) -> bool:
    target = _norm(text)
    return bool(target) and any(_norm(ln.text) == target for ln in doc.instructions)


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def diff_docs(old: AgentDoc, new: AgentDoc) -> dict:
    """Block-level diff: which sections were added, removed, or modified, with line-level hunks."""
    ob, nb = segment(old), segment(new)
    used: set[int] = set()
    rows: list[dict] = []
    for b in nb:
        best, score = None, 0.0
        for k, o in enumerate(ob):
            if k in used:
                continue
            s = max(_similar(o.key, b.key), 0.0)
            if s > score:
                best, score = k, s
        if best is not None and score >= 0.6:
            used.add(best)
            o = ob[best]
            if _norm(o.text) == _norm(b.text):
                rows.append({"status": "unchanged", "title": b.title, "old": [o.start, o.end], "new": [b.start, b.end]})
                continue
            hunks = _hunks(o.lines, b.lines)
            rows.append({"status": "modified", "title": b.title, "old": [o.start, o.end], "new": [b.start, b.end], "hunks": hunks})
        else:
            rows.append({
                "status": "added", "title": b.title, "new": [b.start, b.end],
                "hunks": [{"op": "+", "line": ln.no, "text": ln.text} for ln in b.lines if ln.text.strip()],
            })
    for k, o in enumerate(ob):
        if k not in used:
            rows.append({
                "status": "removed", "title": o.title, "old": [o.start, o.end],
                "hunks": [{"op": "-", "line": ln.no, "text": ln.text} for ln in o.lines if ln.text.strip()],
            })
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("added", "removed", "modified", "unchanged")}
    return {
        "old": old.summary(), "new": new.summary(), "counts": counts, "blocks": rows,
        "char_delta": new.char_count - old.char_count,
        "moderation_changed": (old.content_moderation or "") != (new.content_moderation or ""),
    }


def _hunks(old: list[Line], new: list[Line]) -> list[dict]:
    a = [_norm(x.text) for x in old]
    b = [_norm(x.text) for x in new]
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for x in old[i1:i2]:
            if x.text.strip():
                out.append({"op": "-", "line": x.no, "text": x.text})
        for x in new[j1:j2]:
            if x.text.strip():
                out.append({"op": "+", "line": x.no, "text": x.text})
    return out
