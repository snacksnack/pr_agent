"""Tests for configuration loading (RC1-107)."""
from __future__ import annotations

import pytest

from app.config import Settings


def test_defaults_load():
    s = Settings(_env_file=None)
    assert s.review_model == "claude-sonnet-4-6"
    assert s.deep_review_model == "claude-opus-4-6"
    assert s.block_on == ["leaked_secret"]
    assert s.max_tool_turns > 0
    assert s.max_files_read > 0


def test_block_on_parses_csv_and_trims():
    s = Settings(_env_file=None, review_block_on="leaked_secret, sql_injection ,")
    assert s.block_on == ["leaked_secret", "sql_injection"]


def test_block_on_empty_means_advisory_only():
    s = Settings(_env_file=None, review_block_on="")
    assert s.block_on == []


def test_limits_must_be_positive():
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_tool_turns=0)
