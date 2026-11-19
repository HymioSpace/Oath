"""Check task boundary conditions against the actual provider-independent schema."""

import pytest
from jsonschema import Draft202012Validator, ValidationError

from core.tasks.schema import DEFINITIONS, schema, validate


def test_all_contracts_are_valid():
    for name in DEFINITIONS:
        Draft202012Validator.check_schema(schema(name))


@pytest.mark.parametrize(
    "decision,extra",
    [
        ("continue", {}),
        ("wait", {"wait": {"kind": "time", "reason": "retry", "at": "tomorrow"}}),
        ("complete", {}),
        ("blocked", {}),
    ],
)
def test_incomplete_reports_are_rejected(decision, extra):
    with pytest.raises(ValidationError):
        validate("Report", {"decision": decision, "checkpoint": {"progress": "saved"}, **extra})


def test_wait_and_complete_cannot_be_combined():
    with pytest.raises(ValidationError):
        validate(
            "Report",
            {
                "decision": "continue",
                "checkpoint": {"progress": "saved", "next_step": "research"},
                "wait": {"kind": "user", "reason": "choice", "question": "Which source?"},
            },
        )


def test_identity_cannot_be_supplied_by_model():
    with pytest.raises(ValidationError):
        validate("List", {"user_id": "another_account"})


def test_report_accepts_correlated_event_wait():
    validate(
        "Report",
        {
            "decision": "wait",
            "checkpoint": {"progress": "submitted"},
            "wait": {
                "kind": "event",
                "reason": "CI",
                "source": "github.ci",
                "key": "pr_47",
                "next_check_at": "2026-11-15T21:00:00+08:00",
            },
        },
    )


def test_model_tool_schema_has_explicit_fields_and_matches_server_contract():
    import json

    from core.tasks.schema import tool_schema
    from tests.test_tasks_e2e import specification

    exposed = tool_schema("Create")
    assert "$ref" not in json.dumps(exposed)
    assert exposed["properties"]["acceptance"]["type"] == "object"
    Draft202012Validator(exposed).validate(specification())
    bad = {**specification(), "acceptance": "criteria: send reminder"}
    with pytest.raises(ValidationError):
        Draft202012Validator(exposed).validate(bad)
    with pytest.raises(ValidationError):
        validate("Create", bad)
