"""Tests for the review rubric, prompts, and output schema (RC1-111).

These are pure, offline assertions on the static prompt/schema definitions —
no model, no network. They lock in the contract the loop (RC1-110) and the
``Finding`` model depend on, and guard against the rubric silently dropping a
required review dimension.
"""
from __future__ import annotations

from app.agent import prompts
from app.agent.reviewer import ALL_TOOLS
from app.models import Finding, SEVERITY_ORDER


# --- finding schema contract ---------------------------------------------

def _finding_schema() -> dict:
    return prompts.SUBMIT_TOOL["input_schema"]["properties"]["findings"]["items"]


def test_format_precomputed_findings_empty_is_blank():
    assert prompts.format_precomputed_findings(None) == ""
    assert prompts.format_precomputed_findings([]) == ""


def test_format_precomputed_findings_lists_each_with_no_duplicate_instruction():
    out = prompts.format_precomputed_findings([
        Finding("warning", "n8n", "cron every minute", file="flow.json", line=8),
        Finding("nit", "docs", "a PR-level note"),
    ])
    assert "already recorded by automated checks" in out
    assert "do NOT repeat" in out
    assert "flow.json:8" in out and "[warning/n8n]" in out
    assert "(PR-level)" in out  # finding with no file falls back cleanly


def test_submit_tool_top_level_schema():
    schema = prompts.SUBMIT_TOOL["input_schema"]
    assert prompts.SUBMIT_TOOL["name"] == "submit_review"
    # Strict object with exactly summary + findings required.
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"summary", "findings"}
    assert schema["properties"]["findings"]["type"] == "array"


def test_finding_has_all_required_fields():
    item = _finding_schema()
    props = item["properties"]
    # The fields the acceptance criteria require each finding to carry.
    for field in ("severity", "file", "line", "message", "suggestion", "category"):
        assert field in props, f"finding schema missing '{field}'"
    # Strict items; severity/category/message are mandatory, anchor is optional.
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"severity", "category", "message"}


def test_severity_enum_matches_model_ordering():
    sev = _finding_schema()["properties"]["severity"]
    assert sev["enum"] == ["blocker", "warning", "nit"]
    # Schema enum and the model's triage ordering must not drift apart.
    assert set(sev["enum"]) == set(SEVERITY_ORDER)


def test_category_enum_matches_categories_constant():
    cat = _finding_schema()["properties"]["category"]
    assert cat["enum"] == list(prompts.CATEGORIES)
    # 'general' fallback must be valid, since the loop defaults to it.
    assert "general" in cat["enum"]


def test_line_is_integer_typed():
    assert _finding_schema()["properties"]["line"]["type"] == "integer"


# --- rubric / dimension coverage -----------------------------------------

def test_rubric_covers_every_acceptance_dimension():
    # Each AC dimension is represented by its category token in the rubric.
    for category in (
        "convention",
        "pythonic",
        "security",
        "tests",
        "dependencies",
        "error_handling",
        "breaking_change",
        "pr_drift",
        "infra_scalability",
        "n8n",
    ):
        assert category in prompts.REVIEW_RUBRIC, f"rubric omits '{category}'"


def test_system_prompt_embeds_rubric_and_severity_guidance():
    # The system prompt is what actually reaches the model, so the rubric and
    # calibration must be inside it, not just defined alongside.
    assert prompts.REVIEW_RUBRIC in prompts.SYSTEM_PROMPT
    assert prompts.SEVERITY_GUIDANCE in prompts.SYSTEM_PROMPT
    assert "advisory" in prompts.SYSTEM_PROMPT.lower()


def test_instructions_require_single_submission():
    text = prompts.INSTRUCTIONS.lower()
    assert "submit_review" in text
    assert "exactly once" in text


def test_categories_have_no_duplicates():
    assert len(prompts.CATEGORIES) == len(set(prompts.CATEGORIES))


# --- loop wiring still intact --------------------------------------------

def test_submit_tool_is_offered_to_the_model():
    names = {t["name"] for t in ALL_TOOLS}
    assert "submit_review" in names
    # The repo-exploration tools must still be present alongside it.
    assert {"read_file", "list_dir", "grep"} <= names
