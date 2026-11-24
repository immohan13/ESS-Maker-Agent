"""Parity tests for setup_servicenow, mirroring SetupCheckTests.cs."""

from __future__ import annotations

import pytest

from ess_maker.doctor.setup_servicenow import run


def _count(r: dict, key: str) -> int:
    return r["counts"][key]


def _step(r: dict, step_id: str) -> dict | None:
    return next((s for s in r["steps"] if s["id"] == step_id), None)


def test_no_answers_everything_to_confirm_and_asks_high_severity_first() -> None:
    r = run(None)

    assert _count(r, "pass") == 0
    assert _count(r, "fail") == 0
    questions = r["nextQuestions"]
    assert len(questions) == 5
    assert "read access to the required tables" in questions[0]


def test_hr_scope_without_hr_role_flags_oversharing_as_silent_risk() -> None:
    r = run('{"hrScope": true, "hrScopeRole": "none"}')

    step = _step(r, "SN-04")
    assert step["status"] == "fail"
    assert "OVERSHARING RISK" in r["markdown"]
    assert "### For the ServiceNow admin" in r["markdown"]


def test_simple_flow_with_advanced_criteria_fails_flow_and_requires_v2() -> None:
    r = run('{"advancedCriteria": "yes", "flow": "simple", "scriptedRestResources": ["GetAllUserCriteria"]}')

    assert _step(r, "M365-03")["status"] == "fail"
    assert _step(r, "SN-07")["status"] == "fail"
    assert "deprecated" in _step(r, "SN-07")["detail"]
    assert _step(r, "SN-08") is None, "Identity sync applies only to the Advanced flow."


def test_healthy_answers_pass_and_indexed_count_gap_fails() -> None:
    r = run(
        '{"serviceAccountTables":"yes","sysPropertiesReadable":true,"userCriteriaReadable":true,"hrScope":false,'
        '"sncReadOnly":false,"oauthRegistry":true,"oauthRedirectUrl":"https://gcs.office.com/v1.0/admin/oauth/callback",'
        '"advancedCriteria":false,"sso":false,"connectorState":"Ready","connectorAuth":"OAuth 2.0","flow":"Simple",'
        '"indexedCount":"1,200","expectedCount":2000,"limitedAudience":false,"identityMapping":"yes","oauthConsent":true,'
        '"knowledgeAdded":true,"testQuery":true,"connectorChecker":"passed"}'
    )

    assert _count(r, "fail") == 1
    assert _step(r, "M365-04")["status"] == "fail"
    assert "60%" in _step(r, "M365-04")["detail"]
    assert _count(r, "unknown") == 0


def test_hr_scope_role_answered_simple_flow_fails() -> None:
    r = run('{"hrScopeRole": "custom read role", "flow": "Simple"}')

    assert _step(r, "M365-03")["status"] == "fail"


def test_invalid_answers_raises_value_error() -> None:
    with pytest.raises(ValueError):
        run("[1,2]")
