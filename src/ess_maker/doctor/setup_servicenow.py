"""ServiceNow Knowledge connector setup checklist for an Employee Self-Service agent.

Python port of ``Engine/Setup/ServiceNowKnowledgeSetup.cs`` (ESS Doctor plugin). Steps follow the Microsoft Learn
setup guides for ESS + ServiceNow and the connector's ServiceNow admin setup (service account tables,
sys_properties, HR scope, scripted REST API, identity sync, SSO), the Microsoft 365 admin center connection, Entra
identity mapping, and the Copilot Studio knowledge source. Every answer is optional: unanswered steps are
"unknown" and become follow-up questions.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

SERVICENOW_ADMIN = "ServiceNow admin"
SEARCH_ADMIN = "Microsoft 365 Search admin"
ENTRA_ADMIN = "Microsoft Entra admin"
MAKER = "ESS agent maker"

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
NOT_APPLICABLE = "n/a"

SETUP_DOC = "https://learn.microsoft.com/microsoft-365/copilot/connectors/servicenow-knowledge-admin-setup"
ESS_DOC = "https://learn.microsoft.com/copilot/employee-self-service/servicenow"
CHECKER_TOOL = "https://testconnectivity.microsoft.com/tests/CopilotServiceNowGraphConnectors/input"
CALLBACK = "https://gcs.office.com/v1.0/admin/oauth/callback"

ROLE_ORDER = [SERVICENOW_ADMIN, SEARCH_ADMIN, ENTRA_ADMIN, MAKER]

# StringBuilder.AppendLine uses Environment.NewLine; the plugin runs on Windows (Dataverse sandbox).
NEWLINE = "\n"

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


class _RawNumber:
    """A JSON number that keeps its source text (like JsonElement.GetRawText)."""

    __slots__ = ("raw",)

    def __init__(self, raw: str) -> None:
        self.raw = raw


class _JsonObject(list):
    """A parsed JSON object as ordered (name, value) pairs, so duplicates and source order survive."""


def _contains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def _n0(value: int) -> str:
    return f"{value:,}"


def _f0(value: float) -> str:
    # .NET Framework "F0": round to 15 significant digits, then half away from zero.
    d = Decimal(format(value, ".15g")).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return str(d) if d != 0 else "0"


def _result(status: str, detail: str | None) -> dict[str, Any]:
    return {"status": status, "detail": detail}


def _strip_comments_and_trailing_commas(text: str) -> str:
    """Mimics JsonCommentHandling.Skip and AllowTrailingCommas outside string literals."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("Answers isn't valid JSON: unterminated comment")
            out.append(" ")
            i = end + 2
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"'{name}' is an invalid start of a value.")


class Answers:
    """The maker's answers: a flat JSON object of booleans, numbers and short strings, matched case-insensitively."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    @classmethod
    def parse(cls, data: dict | str | None) -> Answers:
        a = cls()
        if data is None:
            return a
        if isinstance(data, dict):
            for k, v in data.items():
                a._values[str(k).lower()] = v
            return a
        if not isinstance(data, str):
            raise ValueError("Answers must be a JSON object.")
        if data.strip() == "":
            return a
        try:
            root = json.loads(
                _strip_comments_and_trailing_commas(data),
                parse_int=_RawNumber,
                parse_float=_RawNumber,
                parse_constant=_reject_constant,
                object_pairs_hook=_JsonObject,
            )
        except ValueError as ex:
            if str(ex).startswith("Answers isn't valid JSON: "):
                raise
            raise ValueError("Answers isn't valid JSON: " + str(ex)) from ex
        if not isinstance(root, _JsonObject):
            raise ValueError("Answers must be a JSON object.")
        for k, v in root:
            a._values[k.lower()] = v
        return a

    def _get(self, key: str) -> tuple[bool, Any]:
        k = key.lower()
        if k in self._values:
            return True, self._values[k]
        return False, None

    def bool(self, key: str) -> bool | None:
        found, v = self._get(key)
        if not found:
            return None
        if v is True or v is False:
            return v
        s = v.strip().lower() if isinstance(v, str) else ""
        if s in ("yes", "true", "y", "done", "pass", "passed"):
            return True
        if s in ("no", "false", "n", "none", "fail", "failed"):
            return False
        return None

    def text(self, key: str) -> str:
        found, v = self._get(key)
        if not found or v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, (list, tuple)) and not isinstance(v, _JsonObject):
            return ", ".join(x if isinstance(x, str) else _raw_text(x) for x in v)
        return _raw_text(v)

    def int(self, key: str) -> int | None:
        found, v = self._get(key)
        if not found:
            return None
        if isinstance(v, _RawNumber):
            raw = v.raw
            if raw.lstrip("-").isdigit():
                n = int(raw)
                return n if _INT32_MIN <= n <= _INT32_MAX else None
            return None
        if isinstance(v, int) and not isinstance(v, bool):
            return v if _INT32_MIN <= v <= _INT32_MAX else None
        if isinstance(v, str):
            s = v.replace(",", "").strip()
            body = s[1:] if s[:1] in "+-" else s
            if body and body.isascii() and body.isdigit():
                n = int(s)
                return n if _INT32_MIN <= n <= _INT32_MAX else None
        return None

    def yes_no(self, key: str, missing_list_means_fail: bool = False) -> dict[str, Any]:
        """Yes/no step: pass on yes, fail on no; with missing_list_means_fail, a list of names counts as missing items."""
        b = self.bool(key)
        if b is not None:
            return _result(PASS if b else FAIL, None)
        t = self.text(key)
        if missing_list_means_fail and len(t) > 0:
            return _result(FAIL, "Missing: " + t + ".")
        return _result(UNKNOWN, t if len(t) > 0 else None)


def _raw_text(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, _RawNumber):
        return v.raw
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, _JsonObject):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _raw_text(x) for k, x in v) + "}"
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + _raw_text(x) for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_raw_text(x) for x in v) + "]"
    return str(v)


@dataclass(frozen=True)
class SetupStep:
    """One checklist step: who owns it, the answer key it reads, and how to fix it."""

    id: str
    system: str
    role: str
    severity: str
    key: str
    title: str
    question: str
    fix: str
    silent: str | None
    """How the step fails without an error, or None when a failure is visible."""
    doc: str
    check: Callable[[Answers], dict[str, Any]]


def _check_sn04(a: Answers) -> dict[str, Any]:
    role = a.text("hrScopeRole")
    if a.bool("hrScope") is False or _contains(role, "no hr"):
        return _result(NOT_APPLICABLE, "No knowledge bases in the HR Core scope.")
    if len(role) == 0:
        return _result(UNKNOWN, None)
    if _contains(role, "content_reader") or _contains(role, "sn_hr_core.admin"):
        return _result(PASS, "Service account has " + role + ".")
    return _result(FAIL, "Service account role for HR scope: " + role + ".")


def _check_sn05(a: Answers) -> dict[str, Any]:
    v = a.bool("sncReadOnly")
    if v is None:
        return _result(UNKNOWN, None)
    return _result(FAIL, "snc_read_only is assigned.") if v is True else _result(PASS, None)


def _check_sn06(a: Answers) -> dict[str, Any]:
    exists = a.bool("oauthRegistry")
    redirect = a.text("oauthRedirectUrl")
    if exists is False:
        return _result(FAIL, "No OAuth application registry yet.")
    if len(redirect) > 0 and not _contains(redirect, "/admin/oauth/callback") and not _contains(redirect, "consent.azure-apim.net/redirect"):
        return _result(FAIL, "Redirect URL " + redirect + " doesn't match the connector callback; sign-in fails with 'Invalid redirect_url'.")
    refresh = a.int("refreshTokenLifespan")
    if refresh is not None and refresh < 8640000:
        return _result(FAIL, f"Refresh token lifespan {_n0(refresh)}s is shorter than the 100-day default; crawls need re-authorization sooner.")
    if exists is True:
        return _result(PASS, "Redirect URL " + redirect + "." if len(redirect) > 0 else None)
    return _result(UNKNOWN, None)


def _check_sn07(a: Answers) -> dict[str, Any]:
    adv = a.bool("advancedCriteria")
    if adv is False:
        return _result(NOT_APPLICABLE, "No advanced user criteria; the Simple flow is enough.")
    res = a.text("scriptedRestResources")
    if _contains(res, "V2"):
        return _result(PASS, "GetAllUserCriteriaV2 is in place.")
    if len(res) > 0:
        return _result(FAIL, "Scripted REST resources: " + res + ". GetAllUserCriteriaV2 is missing (GetAllUserCriteria is deprecated).")
    if adv is True:
        return _result(FAIL, "Advanced user criteria exist but no scripted REST API resources were reported.")
    return _result(UNKNOWN, None)


def _check_sn08(a: Answers) -> dict[str, Any]:
    if a.text("flow").lower() == "simple" or a.bool("advancedCriteria") is False:
        return _result(NOT_APPLICABLE, None)
    return a.yes_no("identitySync")


def _check_sn09(a: Answers) -> dict[str, Any]:
    if a.bool("sso") is False:
        return _result(NOT_APPLICABLE, None)
    if a.bool("sso") is True:
        return _result(FAIL, "SSO is on: authorize the connection with the service account in a private window.")
    return _result(UNKNOWN, None)


def _check_m365_01(a: Answers) -> dict[str, Any]:
    s = a.text("connectorState")
    if len(s) == 0:
        return _result(UNKNOWN, None)
    if _contains(s, "ready"):
        return _result(PASS, "State: " + s + ".")
    return _result(FAIL, "State: " + s + ".")


def _check_m365_02(a: Answers) -> dict[str, Any]:
    t = a.text("connectorAuth")
    if len(t) == 0:
        return _result(UNKNOWN, None)
    if _contains(t, "oauth"):
        return _result(PASS, None)
    return _result(FAIL, "Authentication: " + t + ". ESS deployments must use OAuth 2.0 with Entra user sign-in.")


def _check_m365_03(a: Answers) -> dict[str, Any]:
    flow = a.text("flow")
    if len(flow) == 0:
        return _result(UNKNOWN, None)
    hr_role = a.text("hrScopeRole")
    hr_scoped = a.bool("hrScope") is True or (len(hr_role) > 0 and not _contains(hr_role, "no hr"))
    needs_advanced = a.bool("advancedCriteria") is True or a.bool("usesUserCriteria") is True or hr_scoped
    if flow.lower() == "simple" and needs_advanced:
        return _result(FAIL, "Simple flow, but the instance uses user criteria or HR-scoped content.")
    return _result(PASS, "Flow: " + flow + ".")


def _check_m365_04(a: Answers) -> dict[str, Any]:
    indexed = a.int("indexedCount")
    expected = a.int("expectedCount")
    if indexed is None or expected is None:
        return _result(UNKNOWN, None)
    text = f"{_n0(indexed)} indexed of {_n0(expected)} published"
    if expected > 0 and indexed < expected * 0.95:
        return _result(FAIL, text + f" ({_f0(100.0 * indexed / expected)}%).")
    return _result(PASS, text + ".")


def _check_m365_05(a: Answers) -> dict[str, Any]:
    limited = a.bool("limitedAudience")
    if limited is False:
        return _result(PASS, "Rolled out to everyone.")
    included = a.bool("testersInAudience")
    if limited is None or included is None:
        return _result(UNKNOWN, None)
    return _result(PASS, None) if included is True else _result(FAIL, "Testers aren't in the limited audience.")


def _check_entra_01(a: Answers) -> dict[str, Any]:
    b = a.bool("identityMapping")
    if b is not None:
        return _result(PASS, None) if b else _result(FAIL, "ServiceNow email doesn't match Entra and no custom mapping is configured.")
    m = a.text("identityMapping")
    if len(m) == 0:
        return _result(UNKNOWN, None)
    if _contains(m, "mismatch"):
        return _result(FAIL, "Identity mapping: " + m + ".")
    return _result(PASS, "Identity mapping: " + m + ".")


STEPS: list[SetupStep] = [
    SetupStep(
        "SN-01", "ServiceNow", SERVICENOW_ADMIN, "high", "serviceAccountTables",
        "Service account can read the knowledge tables",
        "Has the connector's ServiceNow service account been granted read access to the required tables (kb_knowledge, kb_knowledge_base, the kb_uc_* tables, sys_user, sys_user_has_role, sys_user_group, sys_user_role, sys_db_object, sys_dictionary, sys_properties, user_criteria)? Answer yes, no, or list the tables that are missing.",
        "Grant read access to the missing tables through a role on the service account (Learn: Create service account and set up permissions). Then run the Copilot Connector Checker to confirm: " + CHECKER_TOOL,
        None, SETUP_DOC + "#create-service-account-and-set-up-permissions-to-index-items",
        lambda a: a.yes_no("serviceAccountTables", missing_list_means_fail=True),
    ),
    SetupStep(
        "SN-02", "ServiceNow", SERVICENOW_ADMIN, "high", "sysPropertiesReadable",
        "Service account can read the two knowledge sys_properties",
        "Can the service account read glide.knowman.apply_article_read_criteria and glide.knowman.block_access_with_no_user_criteria in sys_properties?",
        "Grant read on sys_properties (or the three narrow ACLs for those two properties' row, Name and Value) to the service account's role.",
        "If these can't be read the connector silently assumes the most restrictive settings, so articles without explicit user criteria don't appear. The admin center shows no error.",
        SETUP_DOC + "#set-up-hierarchical-permissions",
        lambda a: a.yes_no("sysPropertiesReadable"),
    ),
    SetupStep(
        "SN-03", "ServiceNow", SERVICENOW_ADMIN, "high", "userCriteriaReadable",
        "Service account can read user_criteria",
        "Can the service account read the user_criteria table? (Don't grant user_criteria_mtom_user, _group or _role; they aren't needed.)",
        "Grant the service account read access to user_criteria, then trigger a re-crawl of the connector.",
        "Without it, permission evaluation fails and restricted articles that employees can see in ServiceNow are missing in the ESS agent.",
        ESS_DOC + "#troubleshooting",
        lambda a: a.yes_no("userCriteriaReadable"),
    ),
    SetupStep(
        "SN-04", "ServiceNow", SERVICENOW_ADMIN, "high", "hrScopeRole",
        "HR Core knowledge bases: service account has sn_hr_core.content_reader or sn_hr_core.admin",
        "Do any knowledge bases live in the HR Core (sn_hr_core) scope, and if so does the service account have sn_hr_core.content_reader or sn_hr_core.admin? Answer with the role, 'none', or 'no HR scope'.",
        "Assign sn_hr_core.content_reader (least privilege) or sn_hr_core.admin to the service account, then re-crawl.",
        "OVERSHARING RISK: without one of these roles HR-scoped user criteria rows are silently filtered out and the connector may treat HR articles (compensation, benefits, disciplinary) as visible to everyone.",
        SETUP_DOC + "#additional-roles-for-hr-service-delivery-hrsd-content",
        _check_sn04,
    ),
    SetupStep(
        "SN-05", "ServiceNow", SERVICENOW_ADMIN, "medium", "sncReadOnly",
        "Service account doesn't have snc_read_only",
        "Is snc_read_only assigned to the service account?",
        "Remove snc_read_only from the service account; it must write oauth_credential to refresh tokens.",
        "Sign-in works at first, then token refresh fails and crawls stop.",
        SETUP_DOC + "#create-service-account-and-set-up-permissions-to-index-items",
        _check_sn05,
    ),
    SetupStep(
        "SN-06", "ServiceNow", SERVICENOW_ADMIN, "high", "oauthRegistry",
        "OAuth application registry for the connector",
        "Is there an active OAuth application registry for the connector, and what redirect URL and refresh token lifespan does it use?",
        "System OAuth > Application Registry > Create an OAuth API endpoint for external clients. Redirect URL " + CALLBACK + " (or the exact redirect_uri from the sign-in pop-up), refresh token lifespan 31,536,000, access token lifespan 43,200, Client type Integration as a Service.",
        None, ESS_DOC + "#task-1-create-an-oauth-application-registry",
        _check_sn06,
    ),
    SetupStep(
        "SN-07", "ServiceNow", SERVICENOW_ADMIN, "high", "advancedCriteria",
        "Advanced user criteria: scripted REST API with GetAllUserCriteriaV2",
        "Does GET <instance>/api/now/table/user_criteria?advanced=true&sysparm_limit=1 return any rows? If yes, which resources does the 'Microsoft Copilot' scripted REST API have?",
        "Create the 'Microsoft Copilot' scripted REST API (REST_Endpoint ACL for the crawl role) with the GetAllUserCriteriaV2 resource from the connector setup guide. GetAllUserCriteria (/user_criteria), which the ESS ServiceNow page still describes, is deprecated.",
        "Articles that use scripted user criteria can be indexed but invisible to employees unless the Advanced flow can evaluate them.",
        SETUP_DOC + "#set-up-rest-api",
        _check_sn07,
    ),
    SetupStep(
        "SN-08", "ServiceNow", SERVICENOW_ADMIN, "medium", "identitySync",
        "Incremental identity sync (Advanced flow): user_changes resource and auditing",
        "For the Advanced flow, does the scripted REST API have the user_changes resource, and is ServiceNow auditing (com.glide.audit) on for the user, role, group and user_criteria fields listed in the guide?",
        "Add the user_changes resource to the same scripted REST API, enable the Audit plugin and the listed field and delete auditing, and grant the service account read on sys_audit and sys_audit_delete.",
        "Role, group and user criteria changes don't reach Copilot until the next full crawl, so employees can keep or lose access for days.",
        SETUP_DOC + "#set-up-rest-api-for-incremental-identity-sync",
        _check_sn08,
    ),
    SetupStep(
        "SN-09", "ServiceNow", SERVICENOW_ADMIN, "low", "sso",
        "Connector sign-in with ServiceNow SSO",
        "Does the ServiceNow instance use single sign-on?",
        "Authorize the connection from a private window signed in to ServiceNow as the service account, with the Microsoft 365 admin center open in another tab.",
        "With SSO the OAuth pop-up can show 'Logout successfully' or bind the admin's own ServiceNow account instead of the service account.",
        SETUP_DOC + "#resolve-connector-setup-issues-with-servicenow-sso-configuration",
        _check_sn09,
    ),
    SetupStep(
        "M365-01", "Microsoft 365 admin center", SEARCH_ADMIN, "high", "connectorState",
        "ServiceNow Knowledge connection exists and is Ready",
        "In Microsoft 365 admin center > Settings > Search & intelligence > Data sources, what's the state of the ServiceNow Knowledge connection (Ready, Syncing, Failed, Draft, or not created)?",
        "Create the connection (Add connection > ServiceNow Knowledge) or open it and resolve the error shown, then wait for the first full crawl to finish.",
        None, ESS_DOC + "#microsoft-copilot-connector-configuration",
        _check_m365_01,
    ),
    SetupStep(
        "M365-02", "Microsoft 365 admin center", SEARCH_ADMIN, "high", "connectorAuth",
        "Connection uses OAuth 2.0 with Microsoft Entra user sign-in",
        "Which authentication type does the connection use: OAuth 2.0, Basic, or another?",
        "Recreate or edit the connection to use OAuth 2.0 with the client ID and secret from the ServiceNow OAuth registry.",
        None, ESS_DOC + "#servicenow-configuration",
        _check_m365_02,
    ),
    SetupStep(
        "M365-03", "Microsoft 365 admin center", SEARCH_ADMIN, "high", "flow",
        "Connection flow matches the instance (Simple vs Advanced)",
        "Was the connection set up with the Simple or the Advanced user criteria flow?",
        "Use the Advanced flow (with the scripted REST API namespace) when articles use user criteria, scripted conditions or HR profile rules.",
        "With the Simple flow on an instance that uses user criteria, articles are indexed but not visible to employees in ESS.",
        ESS_DOC + "#microsoft-copilot-connector-configuration",
        _check_m365_03,
    ),
    SetupStep(
        "M365-04", "Microsoft 365 admin center", SEARCH_ADMIN, "medium", "indexedCount",
        "Indexed item count matches published articles",
        "How many items has the connection indexed, and how many published articles does ServiceNow have (kb_knowledge with active=true^workflow_state=published)?",
        "If fewer items are indexed than published, check the query string, the service account's row and field ACLs, and the connection's error log, then re-crawl.",
        None, SETUP_DOC + "#identify-item-count-for-ingestion",
        _check_m365_04,
    ),
    SetupStep(
        "M365-05", "Microsoft 365 admin center", SEARCH_ADMIN, "medium", "limitedAudience",
        "Staged rollout includes the people testing ESS",
        "Is the connection rolled out to a limited audience, and if so are the makers and testers in it?",
        "Add the testers to the limited audience, or roll the connection out to everyone.",
        "Testers outside the limited audience get no ServiceNow results and the agent looks broken.",
        ESS_DOC + "#microsoft-copilot-connector-configuration",
        _check_m365_05,
    ),
    SetupStep(
        "ENTRA-01", "Microsoft Entra", ENTRA_ADMIN, "high", "identityMapping",
        "ServiceNow users map to Entra users",
        "Do ServiceNow users' email addresses match their Entra UPN or Mail attribute? If not, was a custom identity mapping configured?",
        "Make ServiceNow email match Entra UPN or Mail, or configure a custom mapping formula on the connection (Map your non-Entra ID identities).",
        "Unmapped users get no permission-trimmed results: restricted articles disappear for them even when they have access in ServiceNow.",
        SETUP_DOC + "#define-servicenow-attribute-mapping",
        _check_entra_01,
    ),
    SetupStep(
        "ENTRA-02", "Microsoft Entra", ENTRA_ADMIN, "medium", "oauthConsent",
        "OAuth authorization completed with Entra user sign-in",
        "Did the connection's Authorize step finish with a green check after Entra sign-in and the ServiceNow consent prompt?",
        "Authorize again with an account that can consent (Cloud Application Administrator or higher), using the ServiceNow service account in the ServiceNow prompt.",
        None, ESS_DOC + "#microsoft-copilot-connector-configuration",
        lambda a: a.yes_no("oauthConsent"),
    ),
    SetupStep(
        "CS-01", "Copilot Studio", MAKER, "high", "knowledgeAdded",
        "ESS agent has the ServiceNow connection as a knowledge source",
        "In Copilot Studio, does the ESS agent's Knowledge page list the ServiceNow connection (under 'Created by your admin')?",
        "Open the ESS agent > Knowledge > Add knowledge > ServiceNow, pick the admin-created connection and select Add. Add it only after the connection is Ready.",
        None, ESS_DOC + "#employee-self-service-agent-configuration",
        lambda a: a.yes_no("knowledgeAdded"),
    ),
    SetupStep(
        "CS-02", "Copilot Studio", MAKER, "medium", "testQuery",
        "A test question returns a ServiceNow article",
        "When you ask the ESS agent a question that a ServiceNow article answers, does it cite the ServiceNow article?",
        "If not, share the conversation ID from /share (or the developer mode card from Microsoft 365 Copilot) and ESS Doctor will show whether the knowledge search ran and what it returned.",
        None, ESS_DOC + "#employee-self-service-agent-configuration",
        lambda a: a.yes_no("testQuery"),
    ),
    SetupStep(
        "VERIFY-01", "ServiceNow", SERVICENOW_ADMIN, "medium", "connectorChecker",
        "Copilot Connector Checker passes",
        "Has the ServiceNow admin run the Copilot Connector Checker for this instance, and did it pass?",
        "Run " + CHECKER_TOOL + " with OAuth and fix every table or credential it reports.",
        None, SETUP_DOC + "#verify-service-account-permissions",
        lambda a: a.yes_no("connectorChecker"),
    ),
]


def _severity_rank(severity: str) -> int:
    return 0 if severity == "high" else 1 if severity == "medium" else 2


def _role_rank(role: str) -> int:
    return ROLE_ORDER.index(role) if role in ROLE_ORDER else -1


@dataclass
class _StepResult:
    step: SetupStep
    status: str
    detail: str | None


def _evaluate(step: SetupStep, a: Answers) -> _StepResult:
    r = step.check(a)
    detail = r["detail"]
    return _StepResult(step, r["status"], detail if isinstance(detail, str) else None)


def run(answers: dict | str | None) -> dict[str, Any]:
    """Runs the checklist on the maker's answers (a JSON object keyed by each step's answer key)."""
    a = Answers.parse(answers)
    results = [r for r in (_evaluate(s, a) for s in STEPS) if r.status != NOT_APPLICABLE]
    silent = [r for r in results if r.status != PASS and r.step.silent is not None]
    cards = [(role, [r for r in results if r.step.role == role and r.status != PASS]) for role in ROLE_ORDER]
    cards = [c for c in cards if len(c[1]) > 0]
    unknowns = [r for r in results if r.status == UNKNOWN]
    questions = [
        r.step.question
        for r in sorted(unknowns, key=lambda r: (_severity_rank(r.step.severity), _role_rank(r.step.role)))[:5]
    ]

    passed = sum(1 for r in results if r.status == PASS)
    failed = sum(1 for r in results if r.status == FAIL)
    unknown = sum(1 for r in results if r.status == UNKNOWN)
    summary = (
        f"ServiceNow Knowledge setup: {passed} of {len(results)} steps confirmed, {failed} need fixing, {unknown} to confirm"
        + (f"; {len(silent)} can fail silently." if len(silent) > 0 else ".")
    )

    return {
        "ok": True,
        "summary": summary,
        "markdown": _markdown(summary, results, silent, cards, questions),
        "counts": {"pass": passed, "fail": failed, "unknown": unknown, "total": len(results)},
        "steps": [
            {
                "id": r.step.id,
                "system": r.step.system,
                "role": r.step.role,
                "title": r.step.title,
                "status": r.status,
                "severity": r.step.severity,
                "detail": r.detail,
                "fix": None if r.status == PASS else r.step.fix,
                "silentRisk": None if r.status == PASS else r.step.silent,
                "question": r.step.question if r.status == UNKNOWN else None,
                "answerKey": r.step.key,
                "doc": r.step.doc,
            }
            for r in results
        ],
        "nextQuestions": questions,
    }


def _markdown(
    summary: str,
    results: list[_StepResult],
    silent: list[_StepResult],
    cards: list[tuple[str, list[_StepResult]]],
    questions: list[str],
) -> str:
    nl = NEWLINE
    md: list[str] = ["## ServiceNow Knowledge setup check", nl, nl, summary, nl, nl]
    if len(silent) > 0:
        md += ["### Can fail silently", nl, nl]
        for r in sorted(silent, key=lambda r: _severity_rank(r.step.severity)):
            md += [
                "- **", r.step.id, " ", r.step.title, "** (",
                "not set up" if r.status == FAIL else "not confirmed", "): ", r.step.silent or "", nl,
            ]
        md.append(nl)

    md += ["### Steps", nl, nl, "| Step | System | Status | Detail |", nl, "| --- | --- | --- | --- |", nl]
    for r in results:
        status = "✅ done" if r.status == PASS else "❌ fix" if r.status == FAIL else "❔ confirm"
        md += [
            "| ", r.step.id, " ", r.step.title, " | ", r.step.system, " | ", status, " | ",
            (r.detail or "").replace("|", "\\|"), " |", nl,
        ]

    md.append(nl)
    for role, items in cards:
        md += ["### For the ", role, nl, nl]
        for r in items:
            md += [
                "- [ ] **", r.step.title, "**: ", r.step.fix if r.status == FAIL else r.step.question,
                " ([doc](", r.step.doc, "))", nl,
            ]
        md.append(nl)

    if len(questions) > 0:
        md += ["### Next questions", nl, nl]
        for i, q in enumerate(questions):
            md += [str(i + 1), ". ", q, nl]

    return "".join(md)
