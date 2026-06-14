"""Tests for the verdict policy (RC1-117).

Locks in the RC1-114 carryover: gating is by finding *category* against
``block_on`` only — severity never gates on its own.
"""
from __future__ import annotations

from app.models import Finding
from app.verdict import (
    EVENT_COMMENT,
    EVENT_REQUEST_CHANGES,
    decide_event,
    gating_findings,
)

BLOCK_ON = ["leaked_secret"]


def test_no_findings_is_comment():
    assert decide_event([], BLOCK_ON) == EVENT_COMMENT


def test_leaked_secret_category_requests_changes():
    findings = [Finding("blocker", "leaked_secret", "AWS key in config.py")]
    assert decide_event(findings, BLOCK_ON) == EVENT_REQUEST_CHANGES
    assert gating_findings(findings, BLOCK_ON) == findings


def test_non_secret_blocker_stays_comment():
    # The whole point of the carryover: a blocker-severity, non-block_on finding
    # is advisory, not a gate.
    findings = [Finding("blocker", "security", "missing authz check")]
    assert decide_event(findings, BLOCK_ON) == EVENT_COMMENT
    assert gating_findings(findings, BLOCK_ON) == []


def test_block_on_is_configurable():
    findings = [Finding("warning", "security", "weak crypto")]
    assert decide_event(findings, ["security"]) == EVENT_REQUEST_CHANGES
    assert decide_event(findings, []) == EVENT_COMMENT  # empty = never block


def test_only_gating_findings_returned_among_many():
    findings = [
        Finding("nit", "pythonic", "use a comprehension"),
        Finding("blocker", "leaked_secret", "token committed"),
        Finding("warning", "tests", "no test for branch"),
    ]
    gating = gating_findings(findings, BLOCK_ON)
    assert [f.category for f in gating] == ["leaked_secret"]
