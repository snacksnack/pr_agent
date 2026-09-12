"""Tests for the model-facing primitives every stage shares (RC1-110, RC1-422).

The single loop these once drove was retired in RC1-422; what is left is the
PR rendering, the cache-marked request, the response normalization and the
``submit_review`` parsing that the scout, the reviewers and the verifier all
use. Offline: a scripted fake stands in for the Anthropic SDK.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent import reviewer
from app.agent.reviewer import parse_findings, render_pr
from app.models import ChangedFile, Finding, PRRef, PullRequest

# --- scripted fake Anthropic client --------------------------------------

class FakeMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []  # records kwargs of each create() call

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._scripted:
            raise AssertionError("fake client ran out of scripted responses")
        return SimpleNamespace(content=self._scripted.pop(0), stop_reason="tool_use", usage=None)


class FakeClient:
    def __init__(self, scripted):
        self.messages = FakeMessages(scripted)


def _use(tool_id, name, **inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


TOOLS = [{"name": "grep", "input_schema": {"type": "object"}}]


@pytest.fixture()
def pr():
    return PullRequest(
        ref=PRRef("o", "r", 7),
        title="Add hello",
        body="adds a greeting",
        base_sha="b" * 12,
        head_sha="h" * 12,
        changed_files_count=1,
    )


# --- render_pr: the one rendering every stage reads ------------------------

def test_render_pr_carries_the_metadata_and_the_description(pr):
    text = "\n".join(render_pr(pr))
    assert "Pull request: o/r#7" in text
    assert "Title: Add hello" in text
    assert "adds a greeting" in text
    assert "Changed files (1)" in text


def test_precomputed_findings_are_rendered_as_already_recorded(pr):
    pre = [Finding("warning", "n8n", "cron fires every minute", file="flow.json", line=8)]
    text = "\n".join(render_pr(pr, pre))
    assert "already recorded by automated checks" in text
    assert "do NOT repeat" in text
    assert "cron fires every minute" in text and "flow.json:8" in text


def test_no_precomputed_section_when_there_are_none(pr):
    assert "already recorded by automated checks" not in "\n".join(render_pr(pr))


def test_rendered_diff_omits_lock_file_patches_but_keeps_their_header():
    """RC1-365: the +/- counts stay, the registry-URL wall goes."""
    pr = PullRequest(
        ref=PRRef("o", "r", 1),
        title="bump",
        files=[
            ChangedFile(
                filename="package-lock.json", status="modified", additions=43, deletions=43,
                patch='@@ -1 +1 @@\n-"resolved": "https://registry.npmjs.org/a"\n+"resolved": "https://registry.npmjs.org/b"',
            ),
            ChangedFile(
                filename="package.json", status="modified", additions=1, deletions=1,
                patch='@@ -1 +1 @@\n-"a": "1"\n+"a": "2"',
            ),
        ],
    )
    seed = "\n".join(render_pr(pr))
    assert "--- package-lock.json (modified, +43/-43) ---" in seed
    assert "generated lock file; patch omitted" in seed
    assert "registry.npmjs.org" not in seed
    assert '+"a": "2"' in seed


def test_rendered_diff_is_bounded(monkeypatch):
    monkeypatch.setattr(reviewer, "MAX_DIFF_CHARS", 20)
    pr = PullRequest(
        ref=PRRef("o", "r", 1),
        title="big",
        files=[
            ChangedFile(filename="a.py", status="modified", patch="+" + "a" * 50),
            ChangedFile(filename="b.py", status="modified", patch="+" + "b" * 50),
        ],
    )
    seed = "\n".join(render_pr(pr))
    assert "[diff truncated; use read_file for the rest]" in seed
    assert "[remaining diffs omitted" in seed
    assert "b" * 50 not in seed


# --- the cache-marked request (RC1-350) --------------------------------------

def test_create_carries_exactly_two_cache_breakpoints():
    # One on the constant tools+system prefix, one riding the latest turn —
    # and never more, or old markers would eat the API's four-breakpoint cap
    # as the scout's conversation grows.
    client = FakeClient([[_use("t1", "grep", pattern="x")]])
    messages = [
        reviewer._user_text("seed"),
        {"role": "assistant", "content": [_use("t0", "grep", pattern="y")]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "hit"}],
        },
    ]
    reviewer._create(client, model="m", messages=messages, max_tokens=10, tools=TOOLS)

    [call] = client.messages.calls
    assert call["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert call["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    marked = [
        block
        for message in call["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]
    assert len(marked) == 1
    assert call["tools"] == TOOLS and "tool_choice" not in call


def test_the_moving_marker_is_applied_to_a_copy_not_the_history():
    messages = [reviewer._user_text("seed")]
    marked = reviewer._with_cache_marker(messages)
    assert "cache_control" in marked[-1]["content"][-1]
    assert "cache_control" not in messages[-1]["content"][-1]


def test_a_string_content_message_is_left_unmarked():
    messages = [{"role": "user", "content": "plain"}]
    assert reviewer._with_cache_marker(messages) is messages


def test_create_passes_tool_choice_through_when_given():
    client = FakeClient([[]])
    reviewer._create(
        client, model="m", messages=[reviewer._user_text("s")], max_tokens=1, tools=TOOLS,
        tool_choice={"type": "tool", "name": "grep"},
    )
    assert client.messages.calls[0]["tool_choice"] == {"type": "tool", "name": "grep"}


# --- responses: normalization and token counts ---------------------------------

def test_normalize_blocks_reads_dicts_and_sdk_objects_alike():
    content = [
        {"type": "text", "text": "hi"},
        SimpleNamespace(type="tool_use", id="t1", name="grep", input=None),
        SimpleNamespace(type="thinking", thinking="…"),  # dropped: not replayable
    ]
    assert reviewer._normalize_blocks(content) == [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t1", "name": "grep", "input": {}},
    ]


def test_tokens_reads_all_four_counts_and_zero_when_missing():
    """RC1-387: since RC1-350 most of the context is cache reads, which the
    API reports outside `input_tokens`; a review's cost needs all four."""
    usage = SimpleNamespace(
        input_tokens=12,
        output_tokens=6,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=2000,
    )
    used = reviewer._tokens(SimpleNamespace(usage=usage))
    assert (used.input_tokens, used.output_tokens) == (12, 6)
    assert used.cache_read_input_tokens == 2000 and used.context_tokens == 2012
    assert reviewer._tokens(SimpleNamespace(usage=None)).context_tokens == 0


# --- parse_findings: malformed findings are counted, not fatal -----------------

def test_malformed_findings_are_counted_not_just_skipped():
    """RC1-387: a silent skip left three zero-finding corpus misses unexplainable."""
    findings, malformed, coerced = parse_findings({"findings": [
        {"severity": "warning", "category": "docs", "message": "fine"},
        {"severity": "warning"},              # no message
        {"category": "docs", "message": "m"},  # no severity
        "not a dict",
    ]})
    assert len(findings) == 1 and malformed == 3 and coerced == 0


def test_an_unknown_severity_is_coerced_to_warning_and_counted():
    """RC1-387: the schema enum does not bind the model; one live review came
    back with severity 'breaking_change' and would have been posted as such."""
    findings, _, coerced = parse_findings({"findings": [
        {"severity": "breaking_change", "category": "breaking_change", "message": "m"},
        {"severity": "nit", "category": "docs", "message": "n"},
    ]})
    assert [f.severity for f in findings] == ["warning", "nit"] and coerced == 1


def test_missing_category_and_non_numeric_line_are_defaulted():
    findings, malformed, _ = parse_findings({"findings": [
        {
            "severity": "blocker", "category": "security", "message": "real",
            "file": "a.py", "line": 3,
        },
        {"category": "security"},  # missing severity+message -> skip
        {"severity": "nit", "message": "no category ok", "line": "notanumber"},
    ]})
    assert len(findings) == 2 and malformed == 1
    nit = [f for f in findings if f.severity == "nit"][0]
    assert nit.category == "general" and nit.line is None
    assert findings[0].line == 3


def test_an_empty_or_absent_findings_list_parses_to_nothing():
    assert parse_findings({}) == ([], 0, 0)
    assert parse_findings({"findings": None}) == ([], 0, 0)
