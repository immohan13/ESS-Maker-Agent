"""Line-preserving parser for Copilot Studio agent exports.

Handles two shapes:
- Full bot content export (``kind: BotDefinition`` + ``components:``), as produced by
  Copilot Studio / the Dataverse bot-content API.
- A single ``GptComponent`` export (instructions only).

The parser is line-based (not a YAML load) so every finding can point at the exact line in
the file the maker uploaded, and so very large exports (1 MB+) parse fast.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_TOPIC_REF = re.compile(r"\{System\.Bot\.Components\.Topics\.'([^']+)'\.DisplayName\}")
_GLOBAL_REF = re.compile(r"\{Global\.(\w+)\}")
_COMPONENT_START = re.compile(r"^\s*- kind: (\w+Component)\s*$")


@dataclass
class Line:
    no: int  # 1-based line number in the source file
    text: str  # instruction text with block indentation removed


@dataclass
class TopicInfo:
    schema: str
    display_name: str = ""
    state: str = ""
    line: int = 0
    description: str = ""
    description_line: int = 0


@dataclass
class AgentDoc:
    name: str
    source: str
    instructions: list[Line] = field(default_factory=list)
    instructions_line: int = 0
    content_moderation: str = ""
    content_moderation_line: int = 0
    model_hint: str = ""
    bot_schema: str = ""
    template: str = ""
    is_full_export: bool = False
    topics: dict[str, TopicInfo] = field(default_factory=dict)
    globals_defined: set[str] = field(default_factory=set)
    knowledge_sources: int = 0
    gpt_display_name: str = ""
    modified_utc: str = ""
    # Character count reported by Copilot Studio's editor, when read from the live page
    platform_char_count: int | None = None
    # Read from the Copilot Studio Configure page (live capture)
    knowledge_list: list = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    topics_partial: bool = False
    # Every topic read directly from Copilot Studio: {name, schema, state, description}
    topic_list: list = field(default_factory=list)
    topics_custom: int | None = None
    topics_system: int | None = None

    @property
    def instruction_text(self) -> str:
        return "\n".join(ln.text for ln in self.instructions)

    @property
    def char_count(self) -> int:
        return self.platform_char_count or len(self.instruction_text)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.instruction_text.encode("utf-8")).hexdigest()[:12]

    def topic_refs(self) -> list[tuple[str, int]]:
        return [(m.group(1), ln.no) for ln in self.instructions for m in _TOPIC_REF.finditer(ln.text)]

    def global_refs(self) -> list[tuple[str, int]]:
        return [(m.group(1), ln.no) for ln in self.instructions for m in _GLOBAL_REF.finditer(ln.text)]

    def summary(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "bot_schema": self.bot_schema,
            "template": self.template,
            "gpt_display_name": self.gpt_display_name,
            "is_full_export": self.is_full_export,
            "instructions_line": self.instructions_line,
            "instruction_lines": len(self.instructions),
            "char_count": self.char_count,
            "content_moderation": self.content_moderation,
            "content_moderation_line": self.content_moderation_line,
            "model_hint": self.model_hint,
            "topics": len(self.topics),
            "globals_defined": len(self.globals_defined),
            "knowledge_sources": self.knowledge_sources,
            "knowledge_list": self.knowledge_list,
            "topic_names": [t.display_name or t.schema for t in self.topics.values()][:50],
            "topics_partial": self.topics_partial,
            "topics_custom": self.topics_custom,
            "topics_system": self.topics_system,
            "topics_total": (self.topics_custom or 0) + (self.topics_system or 0) if self.topics_custom is not None else None,
            "capabilities": self.capabilities,
            "fingerprint": self.fingerprint,
            "modified_utc": self.modified_utc,
        }


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _value(stripped: str) -> str:
    return stripped.split(":", 1)[1].strip().strip('"')


def parse_text(text: str, name: str, source: str = "upload") -> AgentDoc:
    lines = text.splitlines()
    doc = AgentDoc(name=name, source=source)
    doc.is_full_export = any(ln.strip() == "kind: BotDefinition" for ln in lines[:5])

    comp_kind = ""
    comp_display = ""
    comp_state = ""
    comp_desc, comp_desc_line, comp_topic = "", 0, None
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        m = _COMPONENT_START.match(raw)
        if m:
            comp_kind, comp_display, comp_state = m.group(1), "", ""
            comp_desc, comp_desc_line, comp_topic = "", 0, None
            if comp_kind == "KnowledgeSourceComponent":
                doc.knowledge_sources += 1
        elif stripped.startswith("modelDescription:") and comp_kind == "DialogComponent" and not comp_desc:
            inline = _value(stripped)
            comp_desc_line = i + 1
            if inline in ("|", "|-", ">", ">-", ""):
                base, parts, j = _indent(raw), [], i + 1
                while j < n and (not lines[j].strip() or _indent(lines[j]) > base):
                    parts.append(lines[j].strip())
                    j += 1
                comp_desc = " ".join(p for p in parts if p)
            else:
                comp_desc = inline
            if comp_topic is not None:
                comp_topic.description, comp_topic.description_line = comp_desc, comp_desc_line
        elif stripped.startswith("displayName:") and comp_kind and not comp_display:
            comp_display = _value(stripped)
        elif stripped.startswith("state:") and comp_kind and not comp_state:
            comp_state = _value(stripped)
        elif stripped.startswith("schemaName:"):
            value = _value(stripped)
            if not doc.bot_schema and i < 10:
                doc.bot_schema = value
            elif ".topic." in value and comp_kind == "DialogComponent":
                comp_topic = TopicInfo(value, comp_display, comp_state, i + 1, comp_desc, comp_desc_line)
                doc.topics[value] = comp_topic
            elif comp_kind == "GptComponent" and not doc.bot_schema:
                doc.bot_schema = value.split(".gpt.")[0]
        elif stripped.startswith("contentModeration:") and not doc.content_moderation:
            doc.content_moderation = _value(stripped)
            doc.content_moderation_line = i + 1
        elif stripped.startswith("modelNameHint:") and not doc.model_hint:
            doc.model_hint = _value(stripped)
        elif stripped.startswith("template:") and not doc.template and i < 60:
            doc.template = _value(stripped)
        elif comp_kind == "GlobalVariableComponent" and stripped.startswith("name:"):
            doc.globals_defined.add(_value(stripped))
        elif stripped.startswith("variable: Global."):
            doc.globals_defined.add(stripped.split("Global.", 1)[1].strip())
        elif comp_kind == "GptComponent" and stripped.startswith("modifiedTimeUtc:") and not doc.modified_utc:
            doc.modified_utc = _value(stripped)

        if (
            comp_kind == "GptComponent"
            and stripped.startswith("instructions:")
            and _value(stripped).startswith("|")
            and not doc.instructions
        ):
            doc.gpt_display_name = comp_display
            doc.instructions_line = i + 1
            key_indent = _indent(raw)
            body_indent = None
            j = i + 1
            while j < n:
                ln = lines[j]
                if ln.strip():
                    ind = _indent(ln)
                    if ind <= key_indent:
                        break
                    if body_indent is None:
                        body_indent = ind
                    doc.instructions.append(Line(j + 1, ln[body_indent:] if ind >= body_indent else ln.strip()))
                else:
                    doc.instructions.append(Line(j + 1, ""))
                j += 1
            while doc.instructions and not doc.instructions[-1].text.strip():
                doc.instructions.pop()
            i = j
            continue
        i += 1
    return doc


def from_instructions(text: str, name: str, source: str = "draft") -> AgentDoc:
    """Build an AgentDoc from raw instruction text (drafts, pasted edits)."""
    doc = AgentDoc(name=name, source=source, instructions_line=0)
    doc.instructions = [Line(k + 1, t) for k, t in enumerate(text.splitlines())]
    return doc
