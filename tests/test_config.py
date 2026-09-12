"""Tests for configuration loading (RC1-107)."""
from __future__ import annotations

import pytest

from app.config import Settings


def test_defaults_load():
    s = Settings(_env_file=None)
    assert s.review_model == "claude-sonnet-4-6"
    assert s.deep_review_model == "claude-opus-4-6"
    assert s.block_on == ["leaked_secret"]


def test_block_on_parses_csv_and_trims():
    s = Settings(_env_file=None, review_block_on="leaked_secret, sql_injection ,")
    assert s.block_on == ["leaked_secret", "sql_injection"]


def test_block_on_empty_means_advisory_only():
    s = Settings(_env_file=None, review_block_on="")
    assert s.block_on == []


def test_skip_authors_default_is_dependabot():
    s = Settings(_env_file=None)
    assert s.skip_authors == ["dependabot[bot]"]


def test_skip_authors_parses_csv_and_trims():
    s = Settings(_env_file=None, review_skip_authors="dependabot[bot], renovate[bot] ,")
    assert s.skip_authors == ["dependabot[bot]", "renovate[bot]"]


def test_skip_authors_empty_reviews_everyone():
    s = Settings(_env_file=None, review_skip_authors="")
    assert s.skip_authors == []


def test_limits_must_be_positive():
    with pytest.raises(ValueError):
        Settings(_env_file=None, remote_api_budget=0)


def test_verifier_is_off_by_default_and_parses_env_booleans():
    """RC1-387: the verifier is an experiment behind a flag until the ADR says otherwise."""
    assert Settings(_env_file=None).review_verify_findings is False
    assert Settings(_env_file=None, review_verify_findings="1").review_verify_findings is True
    assert Settings(_env_file=None, review_verify_model=None).review_verify_model is None


def test_retired_flags_in_the_environment_are_ignored(monkeypatch):
    """RC1-422: Fly still carries REVIEW_MULTI_AGENT=1 until the secret is
    unset, and an operator's .env may carry MAX_TOOL_TURNS; neither may
    break boot or resurface as a setting."""
    monkeypatch.setenv("REVIEW_MULTI_AGENT", "1")
    monkeypatch.setenv("MAX_TOOL_TURNS", "20")
    s = Settings(_env_file=None)
    assert not hasattr(s, "review_multi_agent") and not hasattr(s, "max_tool_turns")
    # RC1-427: the scout's settings went with it.
    monkeypatch.setenv("REVIEW_SCOUT_MAX_TURNS", "8")
    monkeypatch.setenv("MAX_FILES_READ", "40")
    s = Settings(_env_file=None)
    assert not hasattr(s, "review_scout_max_turns") and not hasattr(s, "max_files_read")
