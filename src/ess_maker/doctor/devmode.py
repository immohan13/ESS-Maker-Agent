"""Microsoft 365 Copilot developer-mode debug card parser.

Faithful port of ``DevModeParser.cs`` (plus the ``ApplyResponse``/``Build`` steps of ``ConversationAnalyzer.cs``) from
the ESS Doctor Dataverse plugin. Reads the card shown after ``-developer on``: Agent metadata, Capabilities, Actions
(Matched functions, Selected functions for execution) and Execution (Executed capabilities, Executed actions). The card
isn't a stable contract and makers paste it as text or JSON, so parsing is label-based and tolerant: JSON is flattened
to "label: value" lines first.

Standard library only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlsplit

__all__ = ["Card", "ExecEntry", "looks_like_devmode", "parse", "diagnose", "flatten_json"]

DEVMODE = "devmode"
SOURCE_LABEL = "Developer mode debug card"

OK = "ok"
FAILED = "failed"
NOT_RUN = "not run"
UNKNOWN = "unknown"

_ACTION_TIMEOUT_MS = 10000

# .NET Char.IsWhiteSpace set (str.strip() would also remove \x1c-\x1f).
_NET_WS = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def _rx(pattern: str, flags: int = re.I) -> re.Pattern[str]:
    return re.compile(pattern, flags)


def _header(names: str) -> re.Pattern[str]:
    return _rx(r"^(?:" + names + r")\b\s*(?:[:\-–]\s*(?P<rest>.*))?$")


_BULLET = _rx(r"^[\s\-\*•·>|#]+|\*\*|__", 0)

_H_EXEC_CAPS = _header(r"executed\s*capabilit(?:y|ies)|capabilit(?:y|ies)\s*execution(?:\s*details)?")
_H_EXEC_ACTIONS = _header(r"executed\s*(?:actions?|functions?)|(?:action|function)\s*execution(?:\s*details)?")
_H_MATCHED = _header(r"matched\s*functions?")
_H_SELECTED = _header(r"selected\s*functions?(?:\s*for\s*execution)?")
_H_METADATA = _header(r"agent\s*metadata")
_H_CAPABILITIES = _header(r"(?:agent\s*)?capabilities")
_H_ACTIONS = _header(r"(?:agent\s*)?actions")
_H_EXECUTION = _header(r"execution(?:\s*details)?")

_NO_ACTIONS = _rx(r"\bno\s*actions?\s*(?:are\s*)?enabled\b")
_NO_MATCHED = _rx(r"\bno\s*matched\s*functions?\b")
_NO_SELECTED = _rx(r"\bno\s*functions?\s*(?:were\s*)?selected\b")
_NO_EXEC_DETAILS = _rx(r"\bno\s*function\s*execution\s*details\b")
_THROTTLED = _rx(r"throttl|try again later|too many requests")
_NO_MESSAGE = _rx(r"^no\s")

_META = _rx(r"^(?P<k>agent\s*id|agent\s*version|conversation\s*id|request\s*id)\s*(?:[:=]|\s)\s*(?P<v>\S.*)$")

_DETAIL_START = _rx(
    r"^(?:execution\s*)?(?:status(?:\s*code)?|state|results?(?:\s*count)?|count|latency|duration|elapsed|time\s*taken|"
    r"request(?:\s*(?:url|method|headers?|body|endpoint|id))?|response(?:\s*(?:status|code|body|headers?|stats))?|"
    r"search(?:\s*(?:text|query|terms?))?|query|(?:http\s*)?method|http(?:\s*status)?|url|endpoint|host|headers?|body|"
    r"error(?:\s*(?:message|code))?|message|parameters?|params|arguments|code|reason|version)\s*[:=]"
    r"|^(?:authorization|content-type|accept|user-agent|cookie|content-length|cache-control|traceparent|x-[\w-]+|ms-[\w-]+)\s*:"
    r'|^(?:GET|POST|PUT|PATCH|DELETE)\s|^HTTP/\d|^[1-5]\d\d\b|^[{}\[\]"]'
)

_BARE_DETAIL_HEADER = _rx(
    r"^(?:request|response|headers?|body|details?|stats|statistics|parameters?|arguments|execution\s*details|response\s*stats)\s*:?$"
)

_NAME_LINE = _rx(r"^(?:function|action|capability|operation|plugin|name)(?:\s*(?:name|id))?\s*[:=]\s*(?P<n>.+)$")

_NAME_CUT = _rx(
    r"\s*[\(\[,;|–—-]?\s*\b(?:status|latency|duration|request|response|results?|search|http|method|url|endpoint|error|succeeded|success|successful|failed|failure)\b"
)

# .NET reuses the group name "c" across alternatives; Python needs distinct names.
_HTTP = _rx(
    r"\b(?:http(?:\s*status)?|status(?:\s*code)?|response(?:\s*(?:status|code))?|code)\b\s*[:=]?\s*(?:HTTP/\d(?:\.\d)?\s+)?(?P<c1>[1-5]\d\d)\b"
    r"|(?:^|\s)HTTP/\d(?:\.\d)?\s+(?P<c2>[1-5]\d\d)\b"
    r"|\b(?P<c3>[1-5]\d\d)\s*(?:-\s*)?(?:OK|Created|Accepted|No Content|Bad Request|Unauthorized|Forbidden|Not Found|Method Not Allowed|"
    r"Request Timeout|Conflict|Too Many Requests|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout)\b"
    r"|^(?P<c4>[1-5]\d\d)$"
)

_STATUS_VALUE = _rx(r"\b(?:execution\s*)?status\s*[:=]\s*(?P<s>[A-Za-z][A-Za-z ]{0,30})")
_STATUS_WORD = _rx(r"\b(?P<s>succeeded|successful|success|completed|failed|failure|timed\s*out|timeout|throttled|cancell?ed|skipped)\b")
_FAILED_WORD = _rx(r"fail|error|time\s*out|timed\s*out|throttl|cancel|denied|forbidden|unauthori")
_OK_WORD = _rx(r"succe|^ok$|complete")

_LATENCY = _rx(
    r"\b(?:latency|duration|elapsed|time\s*taken)\s*(?:\((?:ms|s)\))?\s*[:=]?\s*(?P<v>\d+(?:\.\d+)?)\s*(?P<u>ms|milliseconds|s|secs?|seconds)?\b"
)

# .NET reuses the group name "n" across alternatives; Python needs distinct names.
_RESULTS = _rx(
    r"\b(?P<n1>\d+)\s+(?:results?|items?|documents?|hits?|references?|records?)\b"
    r"|\b(?:results?(?:\s*count)?|count|items?|documents?|hits?|number\s*of\s*results)\s*(?:returned|found)?\s*[:=]\s*(?P<n2>\d+)\b"
)

_NO_RESULTS = _rx(r"\bno\s+(?:results?|documents?|items?|hits?)\b")
_SEARCH_TEXT = _rx(r"\b(?:search\s*(?:text|query|terms?)|query)\s*[:=]\s*(?P<q>.+)$")
_METHOD_URL = _rx(r"\b(?P<m>GET|POST|PUT|PATCH|DELETE)\b\s+(?P<u>https?://[^\s\"'<>]+)")
_URL_VALUE = _rx(r"\b(?:url|endpoint|request\s*url|host)\s*[:=]\s*(?P<u>https?://[^\s\"'<>]+)")
_ANY_URL = _rx(r"https?://[^\s\"'<>]+")
_METHOD_VALUE = _rx(r"\b(?:http\s*)?method\s*[:=]\s*(?P<m>GET|POST|PUT|PATCH|DELETE)\b")
_ERROR_VALUE = _rx(r"\b(?:error(?:\s*message)?|message|reason)\s*[:=]\s*(?P<e>.+)$")
_EMPTY_VALUE = _rx(r"^(?:none|null|n/?a|-|\{\}|\[\])$")

_NON_KNOWLEDGE = _rx(r"code\s*interpreter|graphic|image|dall")
_SERVICENOW = _rx(r"service-?now|\bsn_|/api/now/|\bhrsd\b|\bitsm\b")
_WORKDAY = _rx(r"workday")

_ACTION_ID = _rx(r"^(?:action\s*)?id\s*[:=]\s*(?P<v>.+)$")
_CAPABILITY_NAMED = _rx(r"^(?:name|type|capability|kind)\s*[:=]\s*(?P<v>.+)$")
_ACTIONISH_NAME = _rx(r"function|action|api")
_TIMED_OUT = _rx(r"time\s*d?\s*out")

# ConversationAnalyzer.DevModeSignals (IgnoreCase | Multiline).
_DEVMODE_SIGNALS = _rx(
    r"agent\s*metadata|^\W*agent\s*id\b|matched\s*functions|selected\s*functions|executed\s*(capabilit|actions)|"
    r"no\s*functions?\s*selected|no\s*actions\s*enabled|debug\s*info|developer\s*mode",
    re.I | re.M,
)

# ConversationAnalyzer.Abstain.
_ABSTAIN = _rx(
    r"couldn'?t find (a definitive answer|any|that|this)|could not find|i (don'?t|do not) have (that|this|any) information|"
    r"unable to (find|provide)|no (relevant )?information (was )?found"
)

_NONE, _METADATA, _CAPABILITIES, _ACTIONS, _MATCHED, _SELECTED, _EXECUTION, _EXEC_CAPS, _EXEC_ACTIONS = range(9)

_HEADER_CHECKS = (
    (_H_EXEC_CAPS, _EXEC_CAPS),
    (_H_EXEC_ACTIONS, _EXEC_ACTIONS),
    (_H_MATCHED, _MATCHED),
    (_H_SELECTED, _SELECTED),
    (_H_METADATA, _METADATA),
    (_H_CAPABILITIES, _CAPABILITIES),
    (_H_ACTIONS, _ACTIONS),
    (_H_EXECUTION, _EXECUTION),
)


@dataclass
class ExecEntry:
    """One executed capability or action."""

    name: str | None = None
    status: str | None = None
    ok: bool | None = None
    http: int | None = None
    latency_ms: float | None = None
    results: int | None = None
    search_text: str | None = None
    method: str | None = None
    url: str | None = None
    error: str | None = None
    lines: list[str] = field(default_factory=list)


@dataclass
class Card:
    """What the developer-mode card says."""

    agent_id: str | None = None
    agent_version: str | None = None
    conversation_id: str | None = None
    request_id: str | None = None
    capabilities: list[str] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    exec_caps: list[ExecEntry] = field(default_factory=list)
    exec_actions: list[ExecEntry] = field(default_factory=list)
    exec_unsorted: list[ExecEntry] = field(default_factory=list)
    no_actions: bool = False
    no_matched: bool = False
    no_selected: bool = False
    no_exec_details: bool = False
    throttled: bool = False


# --------------------------------------------------------------------------- helpers mirroring .NET semantics


def _trim(s: str, chars: str | None = None) -> str:
    return s.strip(_NET_WS if chars is None else chars)


def _head(s: str | None, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n]


def _group(m: re.Match[str], *names: str) -> str:
    for n in names:
        v = m.group(n)
        if v is not None:
            return v
    return ""


def _fmt0(v: float) -> str:
    """double.ToString("0", InvariantCulture) on .NET Framework: 15 significant digits, midpoint away from zero."""
    return str(Decimal(format(v, ".15g")).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _host(url: str | None) -> str | None:
    if url is None:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return parts.hostname or None


# --------------------------------------------------------------------------- JSON flattening


class _Num(str):
    """Raw JSON number text (JsonElement.GetRawText)."""


class _Obj(list):
    """JSON object as ordered (key, value) pairs, keeping duplicate keys like JsonElement.EnumerateObject."""


def _load_json(text: str):
    return json.loads(text, object_pairs_hook=_Obj, parse_int=_Num, parse_float=_Num, parse_constant=_reject_constant)


def _reject_constant(name: str):
    raise ValueError(f"invalid JSON constant {name}")


def _parse_json_document(text: str):
    """First JSON document as ConversationAnalyzer.ParseJsonDocuments finds it, or None."""
    try:
        return _load_json(text)
    except ValueError:
        pass
    for line in text.splitlines():
        candidate = _trim(line).rstrip(",")
        if not candidate.startswith("{"):
            continue
        try:
            return _load_json(candidate)
        except ValueError:
            continue
    return None


def _is_scalar(v) -> bool:
    return not isinstance(v, (_Obj, list))


def _scalar(v) -> str | None:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return None
    if isinstance(v, _Num):
        return str.__str__(v)
    return re.sub(r"[\r\n]+", " ", v)


def _humanize(key: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key or "")
    return _trim(re.sub(r"[_\.]+", " ", s))


def _walk(e, label: str | None, out: list[str], depth: int) -> None:
    if depth > 32:
        return
    if isinstance(e, _Obj):
        if label is not None:
            out.append(label + "\n")
        for name, value in e:
            key = _humanize(name)
            if _is_scalar(value):
                v = _scalar(value)
                if v is not None:
                    out.append(f"{key}: {v}\n")
            else:
                _walk(value, key, out, depth + 1)
    elif isinstance(e, list):
        if label is not None:
            out.append(label + "\n")
        for item in e:
            if _is_scalar(item):
                v = _scalar(item)
                if v is not None:
                    out.append(v + "\n")
            else:
                out.append("---\n")
                _walk(item, None, out, depth + 1)
        out.append("---\n")
    else:
        s = _scalar(e)
        if s is not None:
            out.append((label + ": " if label is not None else "") + s + "\n")


def flatten_json(root) -> str:
    """JSON card (already parsed, or JSON text) → "label: value" lines with section headers; array elements are separated by "---"."""
    if isinstance(root, str):
        root = _load_json(root)
    out: list[str] = []
    _walk(root, None, out, 0)
    return "".join(out)


def _as_card_text(text: str) -> tuple[str, bool]:
    """(text to parse, was JSON) after the BOM/whitespace trim done by ConversationAnalyzer.Analyze."""
    t = _trim((text or "").lstrip("\ufeff"))
    if t.startswith("{") or t.startswith("["):
        doc = _parse_json_document(t)
        if doc is not None:
            return flatten_json(doc), True
    return t, False


# --------------------------------------------------------------------------- parsing


def looks_like_devmode(text: str) -> bool:
    """True when the text is a developer-mode card: the DevModeSignals regex, or a JSON card (JSON falls back to this parser)."""
    t = _trim((text or "").lstrip("\ufeff"))
    if not t:
        return False
    if (t.startswith("{") or t.startswith("[")) and _parse_json_document(t) is not None:
        return True
    return _DEVMODE_SIGNALS.search(t) is not None


def _header_of(line: str) -> tuple[int, str]:
    for rx, section in _HEADER_CHECKS:
        m = rx.search(line)
        if m:
            return section, _trim(m.group("rest") or "")
    return _NONE, ""


def _add_name(lst: list[str], name: str | None) -> None:
    name = _trim(_trim(name or ""), "\"'.,;()")
    if 0 < len(name) <= 160 and not any(x.upper() == name.upper() for x in lst):
        lst.append(name)


def _capability_name(line: str) -> str | None:
    named = _CAPABILITY_NAMED.search(line)
    if named:
        return named.group("v")
    if _DETAIL_START.search(line) or _BARE_DETAIL_HEADER.search(line):
        return None
    colon = line.find(":")
    return line[:colon] if colon > 0 else line


def _add_functions(lst: list[str], line: str) -> None:
    if _BARE_DETAIL_HEADER.search(line):
        return
    named = _NAME_LINE.search(line)
    if named:
        _add_name(lst, named.group("n"))
        return
    if _DETAIL_START.search(line):
        return
    colon = line.find(":")
    body = line[:colon] if colon > 0 else line
    for part in body.split(","):
        cut = _NAME_CUT.search(part)
        _add_name(lst, part[: cut.start()] if cut and cut.start() > 0 else part)


def _exec_line(lst: list[ExecEntry], current: ExecEntry | None, line: str) -> ExecEntry | None:
    if _BARE_DETAIL_HEADER.search(line):
        return current

    named = _NAME_LINE.search(line)
    if _DETAIL_START.search(line) and not named:
        if current is None:
            current = ExecEntry()
            lst.append(current)
    else:
        name = named.group("n") if named else line
        cut = _NAME_CUT.search(name)
        if cut and cut.start() > 0:
            name = name[: cut.start()]
        colon = name.find(":")
        if colon > 0 and "://" not in name:
            name = name[:colon]
        name = _trim(re.sub(r"\s+", " ", name), " -–—,;([|.")
        if current is None or current.name is not None:
            current = ExecEntry()
            lst.append(current)
        current.name = _head(name, 120) if name else None

    current.lines.append(line)
    _extract(current, line)
    return current


def _extract(e: ExecEntry, line: str) -> None:
    h = _HTTP.search(line)
    if h and e.http is None:
        e.http = int(_group(h, "c1", "c2", "c3", "c4"))

    if e.status is None:
        sv = _STATUS_VALUE.search(line)
        if sv and not re.search(r"^\s*code", sv.group("s"), re.I):
            e.status = _trim(sv.group("s"))
        else:
            sw = _STATUS_WORD.search(line)
            if sw:
                e.status = sw.group("s")

    lat = _LATENCY.search(line)
    if lat and e.latency_ms is None:
        v = float(lat.group("v"))
        u = (lat.group("u") or "").lower()
        e.latency_ms = v * 1000 if u.startswith("s") else v

    if e.results is None:
        r = _RESULTS.search(line)
        if r:
            e.results = int(_group(r, "n1", "n2"))
        elif _NO_RESULTS.search(line):
            e.results = 0

    st = _SEARCH_TEXT.search(line)
    if st and e.search_text is None:
        e.search_text = _trim(_trim(st.group("q")), '"“”')

    mu = _METHOD_URL.search(line)
    if mu:
        e.method = e.method if e.method is not None else mu.group("m").upper()
        e.url = e.url if e.url is not None else mu.group("u")

    uv = _URL_VALUE.search(line)
    if uv and e.url is None:
        e.url = uv.group("u")

    mv = _METHOD_VALUE.search(line)
    if mv and e.method is None:
        e.method = mv.group("m").upper()

    if e.url is None:
        anyu = _ANY_URL.search(line)
        if anyu:
            e.url = anyu.group(0)

    ev = _ERROR_VALUE.search(line)
    if ev and e.error is None:
        err = _trim(_trim(ev.group("e")), '",')
        if err and not _EMPTY_VALUE.search(err):
            e.error = err


def _outcome(e: ExecEntry) -> bool | None:
    if e.http is not None:
        return e.http < 400
    if e.status is not None:
        if _FAILED_WORD.search(e.status):
            return False
        if _OK_WORD.search(e.status):
            return True
    return False if e.error is not None else None


def _is_noise(e: ExecEntry) -> bool:
    return (
        e.name is None
        and e.status is None
        and e.http is None
        and e.results is None
        and e.url is None
        and e.error is None
        and e.latency_ms is None
    )


def _parse_lines(text: str) -> Card:
    card = Card()
    section = _NONE
    current: ExecEntry | None = None
    for raw in text.splitlines():
        if _trim(raw) == "---":
            current = None
            continue

        line = _trim(_BULLET.sub("", raw))
        if not line:
            continue

        if _NO_ACTIONS.search(line):
            card.no_actions = True
        if _NO_MATCHED.search(line):
            card.no_matched = True
        if _NO_SELECTED.search(line):
            card.no_selected = True
        if _NO_EXEC_DETAILS.search(line):
            card.no_exec_details = True
        if _THROTTLED.search(line) and not _HTTP.search(line):
            card.throttled = True

        nxt, rest = _header_of(line)
        if nxt != _NONE:
            section = nxt
            current = None
            if not rest:
                continue
            line = rest

        meta = _META.search(line)
        if meta and (section in (_NONE, _METADATA) or not re.search("request", meta.group("k"), re.I)):
            k = re.sub(r"\s+", "", meta.group("k").lower())
            v = _trim(meta.group("v"))
            if k == "agentid" and card.agent_id is None:
                card.agent_id = v
            elif k == "agentversion" and card.agent_version is None:
                card.agent_version = v
            elif k == "conversationid" and card.conversation_id is None:
                card.conversation_id = v
            elif k == "requestid" and card.request_id is None:
                card.request_id = v
            continue

        if _NO_MESSAGE.search(line):
            continue

        if section == _CAPABILITIES:
            _add_name(card.capabilities, _capability_name(line))
        elif section == _ACTIONS:
            m = _ACTION_ID.search(line)
            if m:
                _add_name(card.action_ids, m.group("v"))
        elif section == _MATCHED:
            _add_functions(card.matched, line)
        elif section == _SELECTED:
            _add_functions(card.selected, line)
        elif section == _EXEC_CAPS:
            current = _exec_line(card.exec_caps, current, line)
        elif section == _EXEC_ACTIONS:
            current = _exec_line(card.exec_actions, current, line)
        elif section == _EXECUTION:
            current = _exec_line(card.exec_unsorted, current, line)

    for e in card.exec_unsorted:
        is_action = (
            e.http is not None
            or e.url is not None
            or e.method is not None
            or (
                e.name is not None
                and (e.name in card.selected or e.name in card.matched or _ACTIONISH_NAME.search(e.name) is not None)
            )
        )
        (card.exec_actions if is_action else card.exec_caps).append(e)

    card.exec_caps[:] = [e for e in card.exec_caps if not _is_noise(e)]
    card.exec_actions[:] = [e for e in card.exec_actions if not _is_noise(e)]
    for e in card.exec_caps + card.exec_actions:
        e.ok = _outcome(e)
    return card


def parse(text: str) -> Card:
    """Parse a developer-mode card given as text or JSON (JSON is flattened first, like FlattenJson)."""
    t, _ = _as_card_text(text)
    return _parse_lines(t)


# --------------------------------------------------------------------------- diagnosis


def _describe_entry(e: ExecEntry) -> str:
    return (e.name if e.name is not None else "(unnamed)") + " (" + _describe(e) + ")"


def _describe(e: ExecEntry) -> str:
    parts: list[str] = []
    if e.status is not None:
        parts.append(e.status)
    elif e.ok is not None:
        parts.append("succeeded" if e.ok else "failed")
    if e.http is not None:
        parts.append(f"HTTP {e.http}")
    if e.results is not None:
        parts.append(f"{e.results} results")
    if e.search_text is not None:
        parts.append("search “" + _head(e.search_text, 80) + "”")
    if e.latency_ms is not None:
        parts.append(_fmt0(e.latency_ms) + " ms")
    if e.url is not None:
        host = _host(e.url)
        parts.append((e.method + " " if e.method is not None else "") + (host if host is not None else e.url))
    return "no status shown" if not parts else ", ".join(parts)


def _fn(e: ExecEntry) -> str:
    return f"The {e.name} action" if e.name is not None else "The action"


def _system_of(e: ExecEntry) -> str:
    text = (e.name or "") + " " + (e.url or "") + " " + " ".join(e.lines)
    if _SERVICENOW.search(text):
        return "ServiceNow"
    if _WORKDAY.search(text):
        return "Workday"
    host = _host(e.url)
    return host if host is not None else "the backend system"


def _scope(agent_id: str | None) -> str | None:
    if not agent_id:
        return None
    if agent_id.startswith("U_"):
        return "User-scoped (sideloaded)"
    if agent_id.startswith("T_"):
        return "Tenant-scoped (org catalog)"
    if agent_id.startswith("P_"):
        return "Public"
    return None


class _Diagnosis:
    def __init__(self) -> None:
        self.stages = {"planner": UNKNOWN, "search": UNKNOWN, "synthesis": UNKNOWN, "render": UNKNOWN}
        self.happened: list[str] = []
        self.key_details: dict[str, str] = {}
        self.likely_cause = ""
        self.next_steps: list[str] = []
        self.cannot_see: list[str] = []

    def detail(self, key: str, value: str | None) -> None:
        if value is not None and _trim(value) and key not in self.key_details:
            self.key_details[key] = _trim(value)

    def step(self, step: str) -> None:
        if step and _trim(step) and step not in self.next_steps:
            self.next_steps.append(step)

    def set_stages(self, planner: str, search: str) -> None:
        self.stages["planner"] = planner
        self.stages["search"] = search


def _diagnose_card(card: Card, question: str) -> _Diagnosis:
    d = _Diagnosis()
    d.detail("agentId", card.agent_id)
    d.detail("agentScope", _scope(card.agent_id))
    d.detail("agentVersion", card.agent_version)
    d.detail("conversationId", card.conversation_id)
    d.detail("requestId", card.request_id)
    d.detail("capabilities", ", ".join(card.capabilities))
    d.detail("matchedFunctions", "none" if card.no_matched and not card.matched else ", ".join(card.matched))
    d.detail("selectedFunctions", "none" if card.no_selected and not card.selected else ", ".join(card.selected))
    d.detail("executedCapabilities", ", ".join(_describe_entry(e) for e in card.exec_caps))
    d.detail("executedActions", ", ".join(_describe_entry(e) for e in card.exec_actions))
    d.cannot_see.append("The answer text, and how the model used what came back. Pass the agent's response to check that step.")
    d.cannot_see.append("Why the planner picked or skipped each function; the card shows only the outcome.")
    d.cannot_see.append("The content of search results and which documents were cited.")
    d.cannot_see.append("Steps inside Copilot Studio topics or flows after the action was called.")

    if card.capabilities:
        d.happened.append(f"Configured capabilities: {', '.join(card.capabilities)}.")

    if card.no_actions:
        d.happened.append("No actions are enabled for this agent.")
    elif card.matched:
        d.happened.append(f"Matched functions: {', '.join(card.matched)}.")
    elif card.no_matched:
        d.happened.append("No functions matched the question.")

    if card.selected:
        d.happened.append(f"Selected for execution: {', '.join(card.selected)}.")
    elif card.no_selected:
        d.happened.append("No functions were selected for execution.")

    for c in card.exec_caps:
        d.happened.append(f"{c.name if c.name is not None else 'A capability'} ran: {_describe(c)}.")
    for a in card.exec_actions:
        d.happened.append(f"{a.name if a.name is not None else 'An action'} ran: {_describe(a)}.")

    any_exec = bool(card.exec_caps) or bool(card.exec_actions)
    if not any_exec:
        d.happened.append(
            "No function execution details were returned." if card.no_exec_details else "Nothing was executed for this question."
        )

    failed_actions = [a for a in card.exec_actions if a.ok is False]
    knowledge = [c for c in card.exec_caps if c.name is None or not _NON_KNOWLEDGE.search(c.name)]
    failed_caps = [c for c in card.exec_caps if c.ok is False]
    empty_caps = [c for c in knowledge if c.ok is not False and c.results == 0]
    auth = next((a for a in failed_actions if a.http in (401, 403)), None)
    not_found = next((a for a in failed_actions if a.http == 404), None)
    throttled = next((a for a in failed_actions if a.http == 429), None)
    server = next((a for a in failed_actions if a.http is not None and a.http >= 500), None)
    slow = next(
        (
            a
            for a in card.exec_actions
            if (a.status is not None and _TIMED_OUT.search(a.status))
            or (a.latency_ms is not None and a.latency_ms >= _ACTION_TIMEOUT_MS)
        ),
        None,
    )
    q = f" (for example “{_head(question, 120)}”)" if question else ""

    if auth is not None:
        d.set_stages(OK, FAILED)
        system = _system_of(auth)
        host = _host(auth.url)
        if auth.http == 401:
            d.likely_cause = f"{_fn(auth)} reached {host if host is not None else system} but the call was rejected as not signed in (HTTP 401). The backend didn't accept the connection's token."
            d.step(f"Open the agent's {system} connection in Copilot Studio and sign in again.")
            d.step(f"Check the connection's OAuth settings (client ID, secret, resource or scope) match the {system} instance it calls.")
        else:
            d.likely_cause = f"{_fn(auth)} reached {host if host is not None else system} and signed in, but {system} refused the call (HTTP 403). The connection works; the backend doesn't allow this request for this user or app."

        if system == "ServiceNow":
            d.step("In ServiceNow, check the REST API access policies for this API. If a policy limits it to specific OAuth scopes, the OAuth application needs a custom OAuth scope that the policy allows, and the connection must request that scope.")
            d.step("Check the signed-in user has the ServiceNow roles this API needs.")
        elif system == "Workday":
            d.step("In Workday, check the account the connection uses has permission for this operation.")
        else:
            d.step(f"Check the permissions of the account or app the connection uses in {system}.")

        d.step("Run the same request as the same user outside Copilot to confirm it's a backend permission and not the agent.")
    elif not_found is not None:
        d.set_stages(OK, FAILED)
        target = not_found.url if not_found.url is not None else _system_of(not_found)
        d.likely_cause = f"{_fn(not_found)} called {target} and got HTTP 404: the endpoint or record doesn't exist."
        d.step(f"Check the {_system_of(not_found)} instance URL in the connection or environment variables.")
        d.step("Check the API or plugin the action calls is installed and active on that instance.")
    elif throttled is not None:
        d.set_stages(OK, FAILED)
        d.likely_cause = f"{_system_of(throttled)} throttled the call from {_fn(throttled)} (HTTP 429)."
        d.step(f"Check the rate limits for this integration in {_system_of(throttled)}, then try again later.")
    elif server is not None:
        d.set_stages(OK, FAILED)
        d.likely_cause = f"{_system_of(server)} returned a server error (HTTP {server.http}) to {_fn(server)}. The agent and connection did their part."
        d.step(f"Look up this request in the {_system_of(server)} logs to find the error.")
    elif slow is not None:
        d.set_stages(OK, FAILED)
        took = f"took {_fmt0(slow.latency_ms)} ms" if slow.latency_ms is not None else "timed out"
        d.likely_cause = f"{_fn(slow)} {took}. Copilot stops waiting for an action after 10 seconds."
        d.step(f"Make the {_system_of(slow)} call faster: narrow the query or return fewer fields.")
        d.step(f"Check {_system_of(slow)} performance at the time of the conversation.")
    elif failed_actions or (card.no_exec_details and card.selected):
        d.set_stages(OK, FAILED)
        fa = failed_actions[0] if failed_actions else None
        name = _fn(fa) if fa is not None else f"Copilot selected {', '.join(card.selected)} but it"
        d.likely_cause = (
            f"{name} failed: {_head(fa.error, 200)}"
            if fa is not None and fa.error is not None
            else f"{name} didn't run. Copilot most likely couldn't fill the action's parameters from the question."
        )
        d.step("Make the action and parameter descriptions say exactly which values come from the user's question.")
        d.step("Check the action's host URL and API definition are valid.")
    elif not any_exec and card.throttled:
        d.stages["planner"] = UNKNOWN
        d.stages["search"] = NOT_RUN
        d.likely_cause = "Copilot reported throttling, so it didn't run the agent's knowledge or actions."
        d.step("Wait a few minutes and ask again.")
        d.step("If it keeps happening, report it with thumbs down and the #extensibility tag.")
    elif not any_exec and not card.no_actions and (card.no_selected or card.no_matched):
        d.set_stages(FAILED, NOT_RUN)
        d.likely_cause = (
            "Copilot found candidate functions but didn't select any: their descriptions don't match what the user asked."
            if card.no_selected and card.matched
            else "Copilot didn't pick any action or topic for this question: none of the descriptions match what the user asked."
        )
        d.step(f"Rewrite the description of the topic or action that should handle this so it uses the words users ask with{q}.")
        d.step("Check the instructions don't steer this kind of question away from tools.")
        d.step("If this used to work, ask again later: 'No functions selected' after earlier success can mean throttling.")
    elif failed_caps:
        d.set_stages(OK, FAILED)
        fc = failed_caps[0]
        d.likely_cause = f"The {fc.name if fc.name is not None else 'knowledge'} search failed ({fc.status if fc.status is not None else 'failed'})."
        d.step("Check the knowledge source is still connected and the user has access to it, then ask again.")
    elif knowledge and len(empty_caps) == len(knowledge):
        d.set_stages(OK, FAILED)
        ec = empty_caps[0]
        query = f" for “{_head(ec.search_text, 120)}”" if ec.search_text is not None else ""
        d.likely_cause = f"Knowledge search ran ({ec.name if ec.name is not None else 'knowledge'}{query}) but returned 0 results. Either the content isn't in the knowledge source, or the search wording doesn't match how the documents are written."
        d.step("Check a document that answers this question exists in the knowledge source and has finished indexing.")
        d.step("Compare the search text with your document titles. If users word it differently, add the synonym to the terminology rules in the instructions.")
    elif not any_exec:
        d.set_stages(FAILED, NOT_RUN)
        d.likely_cause = "Copilot answered without searching the agent's knowledge or calling an action, so the answer isn't grounded in your sources."
        d.step("Check the routing and grounding rules in the instructions: this question should search knowledge or call a topic before answering.")
        d.step("Check the knowledge source is added to the agent and has finished indexing.")
    else:
        d.set_stages(OK, OK)
        d.likely_cause = "No failure was found: what Copilot ran for this question succeeded. If the answer is still wrong, the problem is in how the answer was written from the results."
        d.step("Compare the answer with what the search or action returned; the instructions shape the final answer.")
        d.step("Pass the agent's response too, so ESS Doctor can check that step.")

    if (
        not card.no_actions
        and not card.matched
        and not card.selected
        and not card.no_matched
        and not card.no_selected
        and not card.exec_actions
        and not card.action_ids
        and not card.capabilities
        and not any_exec
    ):
        d.happened.append("The card had none of the expected sections; check the whole card was copied.")

    return d


def _apply_response(d: _Diagnosis, response: str) -> None:
    """ConversationAnalyzer.ApplyResponse: uses the agent response to judge synthesis and render."""
    if not response:
        return
    s = d.stages
    if s["render"] == UNKNOWN:
        s["render"] = OK

    abstained = _ABSTAIN.search(_head(response, 600)) is not None
    if abstained and s["search"] == OK and s["synthesis"] != FAILED:
        s["synthesis"] = FAILED
        d.happened.append("The response says it couldn't find the answer, even though the data came back.")
        if not d.likely_cause or d.likely_cause.startswith("No failure"):
            d.likely_cause = (
                "The search or action returned data, but the answer didn't use it. The instructions likely tell the agent to "
                "abstain too eagerly, or the returned content doesn't match the question's wording."
            )
        d.step("Check the grounding and abstain rules in the instructions: the agent should answer from what the search or action returned.")
    elif s["synthesis"] == UNKNOWN and s["search"] != FAILED:
        s["synthesis"] = FAILED if abstained else OK


def _entry_dict(e: ExecEntry) -> dict:
    return {
        "name": e.name,
        "status": e.status,
        "ok": e.ok,
        "http": e.http,
        "latency_ms": e.latency_ms,
        "results": e.results,
        "search_text": e.search_text,
        "url": e.url,
        "method": e.method,
        "error": e.error,
    }


def _card_dict(card: Card) -> dict:
    return {
        "agent_id": card.agent_id,
        "agent_version": card.agent_version,
        "conversation_id": card.conversation_id,
        "request_id": card.request_id,
        "capabilities": list(card.capabilities),
        "action_ids": list(card.action_ids),
        "matched": list(card.matched),
        "selected": list(card.selected),
        "no_matched": card.no_matched,
        "no_selected": card.no_selected,
        "no_actions": card.no_actions,
        "no_exec_details": card.no_exec_details,
        "throttled": card.throttled,
        "exec_caps": [_entry_dict(e) for e in card.exec_caps],
        "exec_actions": [_entry_dict(e) for e in card.exec_actions],
    }


def diagnose(text: str, question: str = "", response: str = "") -> dict:
    """Diagnose a developer-mode card; ``response`` (the agent's answer) lets synthesis and render be judged too."""
    if not _trim((text or "").lstrip("\ufeff")):
        raise ValueError(
            "The evidence is empty. Paste the developer-mode debug card, a Purview audit export or an Application Insights export."
        )
    card = parse(text)
    d = _diagnose_card(card, _trim(question or ""))
    _apply_response(d, _trim(response or ""))
    if not d.likely_cause:
        d.likely_cause = "No failure was found in this evidence."
    return {
        "source": DEVMODE,
        "source_label": SOURCE_LABEL,
        "stages": dict(d.stages),
        "happened": d.happened,
        "key_details": d.key_details,
        "likely_cause": d.likely_cause,
        "next_steps": d.next_steps,
        "cannot_see": d.cannot_see,
        "card": _card_dict(card),
    }
