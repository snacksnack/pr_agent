"""Tests for the n8n execution-cost static check (RC1-112).

Pure, offline assertions on the deterministic check — no model, no network.
They lock in two things the ticket cares about: the rules fire on concretely
costly configurations (aggressive schedules/polling, unbounded loops,
sub-workflow fan-out, misconfigured execute-once), and — just as important —
unknown / sane / malformed structures produce NO finding (no false alarms).
"""
from __future__ import annotations

from app.agent.checks import n8n
from app.models import Finding


# --- helpers --------------------------------------------------------------

def _wf(*nodes: dict, connections: dict | None = None) -> dict:
    """Build a minimal n8n workflow export around the given nodes."""
    return {"nodes": list(nodes), "connections": connections or {}}


def _node(node_type: str, *, name: str = "", parameters: dict | None = None, **extra) -> dict:
    return {
        "name": name or node_type,
        "type": node_type,
        "parameters": parameters or {},
        **extra,
    }


def _categories(findings: list[Finding]) -> set[str]:
    return {f.category for f in findings}


# --- detection / version tolerance ----------------------------------------

def test_looks_like_n8n_workflow_accepts_real_export():
    assert n8n.looks_like_n8n_workflow({"nodes": [], "connections": {}})


def test_looks_like_n8n_workflow_rejects_unknown_shapes():
    assert not n8n.looks_like_n8n_workflow({"nodes": []})            # no connections
    assert not n8n.looks_like_n8n_workflow({"connections": {}})       # no nodes
    assert not n8n.looks_like_n8n_workflow({"nodes": {}, "connections": {}})  # nodes not a list
    assert not n8n.looks_like_n8n_workflow([])                        # not a dict
    assert not n8n.looks_like_n8n_workflow("nope")


def test_non_workflow_input_yields_no_findings():
    assert n8n.check_workflow({"hello": "world"}) == []
    assert n8n.check_workflow(None) == []
    assert n8n.check_workflow("nope") == []


def test_findings_are_finding_objects_tagged_n8n():
    wf = _wf(_node("n8n-nodes-base.cron",
                   parameters={"triggerTimes": {"item": [{"mode": "everyMinute"}]}}))
    findings = n8n.check_workflow(wf)
    assert findings and all(isinstance(f, Finding) for f in findings)
    assert _categories(findings) == {"n8n"}
    # The check leaves file/line for the caller (review.py) to anchor.
    assert all(f.file is None and f.line is None for f in findings)
    assert all(f.severity == "warning" for f in findings)


# --- clean workflows: no false alarms -------------------------------------

def test_benign_workflow_has_no_findings():
    wf = _wf(
        _node("n8n-nodes-base.manualTrigger"),
        _node("n8n-nodes-base.set"),
        _node("n8n-nodes-base.scheduleTrigger",
              parameters={"rule": {"interval": [{"field": "hours", "hoursInterval": 6}]}}),
    )
    assert n8n.check_workflow(wf) == []


def test_empty_workflow_has_no_findings():
    assert n8n.check_workflow(_wf()) == []


def test_unknown_node_shapes_are_skipped_not_raised():
    # Garbage nodes / params must never raise — they just yield nothing.
    wf = {"nodes": ["not-a-dict", {"type": 123}, {"no_type": True},
                    {"type": "n8n-nodes-base.cron", "parameters": "oops"}],
          "connections": {}}
    assert n8n.check_workflow(wf) == []


# --- aggressive cron trigger ----------------------------------------------

def test_cron_every_minute_flagged():
    wf = _wf(_node("n8n-nodes-base.cron", name="Poll",
                   parameters={"triggerTimes": {"item": [{"mode": "everyMinute"}]}}))
    findings = n8n.check_workflow(wf)
    assert len(findings) == 1
    assert "every minute" in findings[0].message.lower()
    assert "Poll" in findings[0].message


def test_cron_everyx_two_minutes_flagged_but_ten_is_not():
    hot = _wf(_node("n8n-nodes-base.cron",
                    parameters={"triggerTimes": {"item": [{"mode": "everyX", "value": 2, "unit": "minutes"}]}}))
    cool = _wf(_node("n8n-nodes-base.cron",
                     parameters={"triggerTimes": {"item": [{"mode": "everyX", "value": 10, "unit": "minutes"}]}}))
    assert len(n8n.check_workflow(hot)) == 1
    assert n8n.check_workflow(cool) == []


def test_cron_hourly_mode_not_flagged():
    wf = _wf(_node("n8n-nodes-base.cron",
                   parameters={"triggerTimes": {"item": [{"mode": "everyHour"}]}}))
    assert n8n.check_workflow(wf) == []


def test_cron_custom_expression_every_minute_flagged():
    wf = _wf(_node("n8n-nodes-base.cron",
                   parameters={"triggerTimes": {"item": [{"mode": "custom", "cronExpression": "* * * * *"}]}}))
    assert len(n8n.check_workflow(wf)) == 1


def test_cron_custom_expression_hourly_not_flagged():
    wf = _wf(_node("n8n-nodes-base.cron",
                   parameters={"triggerTimes": {"item": [{"mode": "custom", "cronExpression": "0 * * * *"}]}}))
    assert n8n.check_workflow(wf) == []


# --- scheduleTrigger (newer node) -----------------------------------------

def test_schedule_trigger_seconds_flagged():
    wf = _wf(_node("n8n-nodes-base.scheduleTrigger",
                   parameters={"rule": {"interval": [{"field": "seconds", "secondsInterval": 30}]}}))
    findings = n8n.check_workflow(wf)
    assert len(findings) == 1
    assert "30 second" in findings[0].message


def test_schedule_trigger_minutes_below_threshold_flagged():
    wf = _wf(_node("n8n-nodes-base.scheduleTrigger",
                   parameters={"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}}))
    assert len(n8n.check_workflow(wf)) == 1


def test_schedule_trigger_minutes_at_threshold_not_flagged():
    wf = _wf(_node("n8n-nodes-base.scheduleTrigger",
                   parameters={"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}}))
    assert n8n.check_workflow(wf) == []


def test_schedule_trigger_daily_not_flagged():
    wf = _wf(_node("n8n-nodes-base.scheduleTrigger",
                   parameters={"rule": {"interval": [{"field": "days", "daysInterval": 1}]}}))
    assert n8n.check_workflow(wf) == []


# --- interval node (older) ------------------------------------------------

def test_interval_node_seconds_flagged():
    wf = _wf(_node("n8n-nodes-base.interval",
                   parameters={"interval": 15, "unit": "seconds"}))
    assert len(n8n.check_workflow(wf)) == 1


def test_interval_node_minutes_above_threshold_not_flagged():
    wf = _wf(_node("n8n-nodes-base.interval",
                   parameters={"interval": 30, "unit": "minutes"}))
    assert n8n.check_workflow(wf) == []


# --- polling triggers (pollTimes) -----------------------------------------

def test_polling_every_minute_flagged():
    wf = _wf(_node("n8n-nodes-base.gmailTrigger",
                   parameters={"pollTimes": {"item": [{"mode": "everyMinute"}]}}))
    findings = n8n.check_workflow(wf)
    assert len(findings) == 1
    assert "poll" in findings[0].message.lower()


def test_polling_hourly_not_flagged():
    wf = _wf(_node("n8n-nodes-base.gmailTrigger",
                   parameters={"pollTimes": {"item": [{"mode": "everyHour"}]}}))
    assert n8n.check_workflow(wf) == []


# --- unbounded / misconfigured loop ---------------------------------------

def test_split_in_batches_zero_flagged():
    wf = _wf(_node("n8n-nodes-base.splitInBatches",
                   parameters={"batchSize": 0}))
    findings = n8n.check_workflow(wf)
    assert len(findings) == 1
    assert "batch size" in findings[0].message.lower()


def test_split_in_batches_positive_not_flagged():
    wf = _wf(_node("n8n-nodes-base.splitInBatches", parameters={"batchSize": 50}))
    assert n8n.check_workflow(wf) == []


def test_split_in_batches_absent_size_not_flagged():
    # Absent -> n8n applies its own default; we don't guess.
    wf = _wf(_node("n8n-nodes-base.splitInBatches", parameters={}))
    assert n8n.check_workflow(wf) == []


# --- sub-workflow fan-out --------------------------------------------------

def test_execute_workflow_without_execute_once_flagged():
    wf = _wf(_node("n8n-nodes-base.executeWorkflow", name="Run child"))
    findings = n8n.check_workflow(wf)
    assert len(findings) == 1
    assert "fans out" in findings[0].message.lower() or "per" in findings[0].message.lower()


def test_execute_workflow_node_level_execute_once_not_flagged():
    wf = _wf(_node("n8n-nodes-base.executeWorkflow", executeOnce=True))
    assert n8n.check_workflow(wf) == []


def test_execute_workflow_param_level_execute_once_not_flagged():
    wf = _wf(_node("n8n-nodes-base.executeWorkflow",
                   parameters={"executeOnce": True}))
    assert n8n.check_workflow(wf) == []


# --- multiple issues across a workflow ------------------------------------

def test_multiple_costly_nodes_each_flagged():
    wf = _wf(
        _node("n8n-nodes-base.cron", name="Cron",
              parameters={"triggerTimes": {"item": [{"mode": "everyMinute"}]}}),
        _node("n8n-nodes-base.splitInBatches", name="Loop", parameters={"batchSize": 0}),
        _node("n8n-nodes-base.executeWorkflow", name="Child"),
        _node("n8n-nodes-base.set", name="Edit"),  # benign
    )
    findings = n8n.check_workflow(wf)
    assert len(findings) == 3
    labels = " ".join(f.message for f in findings)
    assert "Cron" in labels and "Loop" in labels and "Child" in labels
