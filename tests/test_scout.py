"""Tests for the scout (RC1-390). Offline: a scripted fake client."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent import scout
from app.agent.reviewer import ReviewError
from app.agent.tools import RepoTools
from app.models import Finding, PRRef, PullRequest


class FakeMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._scripted:
            raise AssertionError("fake client ran out of scripted responses")
        return SimpleNamespace(
            content=self._scripted.pop(0),
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=100,
                cache_read_input_tokens=50,
            ),
        )


def _client(scripted):
    return SimpleNamespace(messages=FakeMessages(scripted))


def _use(tool_id, name, **inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def hello():\n    return 'hi'  # TODO\n")
    return RepoTools(root)


@pytest.fixture()
def pr():
    return PullRequest(ref=PRRef("o", "r", 7), title="Add hello", body="adds a greeting")


def _explore(pr, repo, client, **kw):
    kw.setdefault("max_tool_turns", 5)
    kw.setdefault("max_files_read", 5)
    return scout.explore(pr, repo, client=client, model="m", **kw)


def test_explores_then_submits_a_brief(pr, repo):
    client = _client(
        [
            [_use("t1", "grep", pattern="TODO")],
            [_use("t2", "read_file", path="src/app.py")],
            [_use("t3", "submit_brief", brief="  Uses a TODO marker.  ")],
        ]
    )
    brief = _explore(pr, repo, client)

    assert brief.text == "Uses a TODO marker."
    assert brief.tool_turns == 3 and brief.files_read == 1 and brief.truncated is False
    assert brief.skipped is False
    assert brief.usage.cache_read_input_tokens == 150  # summed across turns
    # The grep's result was fed back before the brief was written.
    fed_back = client.messages.calls[1]["messages"][-1]["content"][0]
    assert fed_back["type"] == "tool_result" and "src/app.py:2" in fed_back["content"]


def test_scout_offers_repo_tools_and_submit_brief_but_not_submit_review(pr, repo):
    client = _client([[_use("t1", "submit_brief", brief="b")]])
    _explore(pr, repo, client)
    names = {t["name"] for t in client.messages.calls[0]["tools"]}
    assert names == {"read_file", "list_dir", "grep", "submit_brief"}
    seed = client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "Do not write findings" in seed
    assert "Pull request: o/r#7" in seed


def test_precomputed_findings_are_in_the_seed(pr, repo):
    client = _client([[_use("t1", "submit_brief", brief="b")]])
    _explore(pr, repo, client, precomputed_findings=[Finding("warning", "n8n", "hot cron")])
    seed = client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "hot cron" in seed


def test_exhausted_turn_budget_forces_a_brief(pr, repo):
    client = _client(
        [
            [_use("t1", "grep", pattern="a")],
            [_use("t2", "grep", pattern="b")],
            [_use("t3", "submit_brief", brief="forced")],
        ]
    )
    brief = _explore(pr, repo, client, max_tool_turns=2)

    assert brief.text == "forced" and brief.truncated is True and brief.tool_turns == 2
    forced_call = client.messages.calls[-1]
    assert forced_call["tool_choice"] == {"type": "tool", "name": "submit_brief"}
    assert "Call submit_brief now" in forced_call["messages"][-1]["content"][0]["text"]


def test_model_stopping_without_a_tool_call_is_forced(pr, repo):
    client = _client(
        [
            [{"type": "text", "text": "I looked around."}],
            [_use("t3", "submit_brief", brief="forced")],
        ]
    )
    brief = _explore(pr, repo, client)
    assert brief.text == "forced"


def test_file_read_budget_is_enforced(pr, repo):
    client = _client(
        [
            [_use("t1", "read_file", path="src/app.py")],
            [_use("t2", "read_file", path="src/app.py")],
            [_use("t3", "submit_brief", brief="b")],
        ]
    )
    brief = _explore(pr, repo, client, max_files_read=1)
    assert brief.truncated is True and brief.files_read == 1
    refused = client.messages.calls[2]["messages"][-1]["content"][0]["content"]
    assert "budget exhausted" in refused


def test_forced_call_without_a_brief_is_an_error(pr, repo):
    client = _client([[{"type": "text", "text": "no"}], [{"type": "text", "text": "still no"}]])
    with pytest.raises(ReviewError):
        _explore(pr, repo, client)


def test_long_brief_is_cut_and_empty_brief_is_named(pr, repo):
    long = _explore(pr, repo, _client([[_use("t", "submit_brief", brief="x" * 5000)]]))
    assert len(long.text) < 5000 and long.text.endswith("[brief cut at the length cap]")
    empty = _explore(pr, repo, _client([[_use("t", "submit_brief", brief="   ")]]))
    assert "empty brief" in empty.text


def test_skipped_brief_says_why():
    brief = scout.skipped_brief("documentation-only change")
    assert brief.skipped is True and brief.text.startswith("(scout skipped: documentation-only")
    assert brief.usage.context_tokens == 0
