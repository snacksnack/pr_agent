"""Tests for the agentic review loop (RC1-110).

A scripted fake client stands in for the Anthropic SDK, so these run offline.
The repo tools run for real against a temp checkout.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent.reviewer import ReviewError, format_pr_for_review, review_pull_request
from app.agent.tools import RepoTools
from app.models import Finding, PRRef, PullRequest


# --- scripted fake Anthropic client --------------------------------------

class FakeMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []  # records kwargs of each create() call

    def create(self, **kwargs):
        # Snapshot kwargs: the loop mutates the messages list in place, so we
        # must deep-copy to capture the state at this call.
        self.calls.append(copy.deepcopy(kwargs))
        if not self._scripted:
            raise AssertionError("fake client ran out of scripted responses")
        content = self._scripted.pop(0)
        return SimpleNamespace(content=content, stop_reason="tool_use")


class FakeClient:
    def __init__(self, scripted):
        self.messages = FakeMessages(scripted)


def _text(t):
    return {"type": "text", "text": t}


def _use(tool_id, name, **inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _submit(tool_id, summary, findings):
    return {"type": "tool_use", "id": tool_id, "name": "submit_review",
            "input": {"summary": summary, "findings": findings}}


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def hello():\n    return 'hi'  # TODO\n")
    return RepoTools(root)


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


# --- explore then submit --------------------------------------------------

def test_explores_then_submits(repo, pr):
    scripted = [
        [_text("looking"), _use("t1", "grep", pattern="TODO")],
        [_use("t2", "read_file", path="src/app.py")],
        [_submit("t3", "One issue found", [
            {"severity": "warning", "category": "security", "message": "secret-ish",
             "file": "src/app.py", "line": 2, "suggestion": "use env"},
        ])],
    ]
    client = FakeClient(scripted)

    result = review_pull_request(pr, repo, client=client, max_tool_turns=10, max_files_read=10)

    assert result.summary == "One issue found"
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.severity == "warning" and f.file == "src/app.py" and f.line == 2
    assert result.tool_turns == 3
    assert result.files_read == 1
    assert result.truncated is False
    # The submit tool must have been offered to the model.
    tool_names = {t["name"] for t in client.messages.calls[0]["tools"]}
    assert "submit_review" in tool_names and "grep" in tool_names


def test_grep_tool_result_fed_back(repo, pr):
    # After the grep call, the next user message should carry a tool_result
    # whose content came from the real repo tools (mentions the match).
    scripted = [
        [_use("t1", "grep", pattern="TODO")],
        [_submit("t2", "done", [])],
    ]
    client = FakeClient(scripted)
    review_pull_request(pr, repo, client=client, max_tool_turns=10)

    second_call_messages = client.messages.calls[1]["messages"]
    tool_result_msg = second_call_messages[-1]
    block = tool_result_msg["content"][0]
    assert block["type"] == "tool_result"
    assert "src/app.py:2" in block["content"]


# --- file-read budget -----------------------------------------------------

def test_file_read_budget_enforced(repo, pr):
    scripted = [
        [_use("t1", "read_file", path="src/app.py")],
        [_use("t2", "read_file", path="src/app.py")],  # over budget
        [_submit("t3", "done", [])],
    ]
    client = FakeClient(scripted)

    result = review_pull_request(pr, repo, client=client, max_tool_turns=10, max_files_read=1)

    assert result.files_read == 1
    assert result.truncated is True
    # The second read should have been denied with an error tool_result.
    third_call_messages = client.messages.calls[2]["messages"]
    denied = third_call_messages[-1]["content"][0]["content"]
    assert "budget" in denied.lower()


# --- turn cap forces a final submission ----------------------------------

def test_turn_cap_forces_submission(repo, pr):
    scripted = [
        [_use("t1", "grep", pattern="TODO")],            # turn 1 (only turn allowed)
        [_submit("tf", "forced summary", [])],            # forced submit call
    ]
    client = FakeClient(scripted)

    result = review_pull_request(pr, repo, client=client, max_tool_turns=1)

    assert result.summary == "forced summary"
    assert result.truncated is True
    # The forced call must pin tool_choice to submit_review.
    assert client.messages.calls[-1]["tool_choice"] == {"type": "tool", "name": "submit_review"}


def test_no_tool_use_triggers_forced_submit(repo, pr):
    scripted = [
        [_text("I think this looks fine")],   # model forgot to submit
        [_submit("tf", "all good", [])],       # forced submission
    ]
    client = FakeClient(scripted)

    result = review_pull_request(pr, repo, client=client, max_tool_turns=10)
    assert result.summary == "all good"


def test_forced_submit_failure_raises(repo, pr):
    scripted = [
        [_text("nope")],
        [_text("still not submitting")],  # forced call returns no submit_review
    ]
    client = FakeClient(scripted)
    with pytest.raises(ReviewError):
        review_pull_request(pr, repo, client=client, max_tool_turns=10)


# --- precomputed (deterministic) findings as context ---------------------

def test_precomputed_findings_rendered_in_seed_prompt(repo, pr):
    pre = [Finding("warning", "n8n", "cron fires every minute", file="flow.json", line=8)]
    scripted = [[_submit("t1", "done", [])]]
    client = FakeClient(scripted)

    review_pull_request(pr, repo, client=client, max_tool_turns=5, precomputed_findings=pre)

    seed = client.messages.calls[0]["messages"][0]["content"]
    assert "already recorded by automated checks" in seed
    assert "do NOT repeat" in seed
    assert "cron fires every minute" in seed and "flow.json:8" in seed


def test_seed_prompt_has_no_precomputed_section_when_none(pr):
    assert "already recorded by automated checks" not in format_pr_for_review(pr)


# --- malformed findings are skipped, not fatal ---------------------------

def test_malformed_findings_are_skipped(repo, pr):
    scripted = [
        [_submit("t1", "mixed", [
            {"severity": "blocker", "category": "security", "message": "real one", "file": "a.py", "line": 3},
            {"category": "security"},                         # missing severity+message -> skip
            {"severity": "nit", "message": "no category ok", "line": "notanumber"},
        ])],
    ]
    client = FakeClient(scripted)
    result = review_pull_request(pr, repo, client=client, max_tool_turns=5)

    assert len(result.findings) == 2  # malformed one dropped
    assert result.has_blocking is True
    nit = [f for f in result.findings if f.severity == "nit"][0]
    assert nit.category == "general"   # defaulted
    assert nit.line is None             # non-numeric line dropped
