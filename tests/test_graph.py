"""Tests for the LangGraph port of the multi-agent review (RC1-391 spike).

Offline, on the same scripted fakes as ``test_multi``. The load-bearing test
is parity: fed identical fakes, the graph and the asyncio version must make
the same requests in the same order and return the same record.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph, multi
from app.agent.prompts import DIFF_LOCAL
from app.agent.reviewer import review_pull_request
from app.agent.router import ReviewPlan
from app.agent.tools import RepoTools
from app.config import Settings
from app.models import ChangedFile, Finding, PRRef, PullRequest
from evals import subject
from tests.test_multi import (
    SCOUT,
    WARM,
    _async,
    _finding,
    _pr_changing_helper,
    _repo_with_conventions,
    _submit,
    _sync,
    _use,
)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "x.py").write_text("x = 1\n")
    return RepoTools(root)


@pytest.fixture()
def pr():
    return PullRequest(
        ref=PRRef("o", "r", 9),
        title="Change x",
        body="changes x",
        files=[ChangedFile("app/x.py", "modified", patch="@@ -1 +1 @@\n-x = 0\n+x = 1")],
    )


def _run(pr, repo, sync, async_client, **kw):
    kw.setdefault("model", "m")
    return graph.review_pull_request_graph(pr, repo, client=sync, async_client=async_client, **kw)


def _without_latency(result):
    return dataclasses.replace(result, stage_latency_ms={})


# --- parity with the asyncio version ---------------------------------------------

def _scripted(verify_verdicts=None):
    """One scripted world, built twice so each engine gets its own copy."""
    sync_script = [*SCOUT]
    if verify_verdicts is not None:
        sync_script.append([_use("verify_findings", verdicts=verify_verdicts)])
    reviewers = [
        _submit("A secret is committed.", [_finding("blocker", "leaked_secret", "key in code")]),
        _submit("No tests cover x.", [_finding("warning", "tests", "untested", line=9)]),
        _submit(
            "Docs drift.",
            [
                _finding("nit", "pr_drift", "d", file=None, line=None),
                _finding("warning", "tests", "off scope for me", line=9),
            ],
        ),
    ]
    return _sync(*sync_script), _async(WARM, *reviewers)


@pytest.mark.parametrize("verify", [False, True])
def test_graph_makes_the_same_requests_and_returns_the_same_record(pr, repo, verify):
    verdicts = [{"index": 0, "decision": "downgrade", "severity": "warning", "reason": "fixture"}]
    sync_a, async_a = _scripted(verdicts if verify else None)
    sync_g, async_g = _scripted(verdicts if verify else None)

    expected = multi.review_pull_request_multi(
        pr, repo, client=sync_a, async_client=async_a, model="m", verify=verify
    )
    actual = _run(pr, repo, sync_g, async_g, verify=verify)

    assert sync_g.messages.calls == sync_a.messages.calls, "scout and verifier requests"
    assert async_g.messages.calls == async_a.messages.calls, "warm call and reviewer requests"
    assert _without_latency(actual) == _without_latency(expected)
    assert actual.mode == "multi" and actual.verified is verify
    assert set(actual.stage_latency_ms) == set(expected.stage_latency_ms) | {"graph_overhead"}
    assert all(v >= 0 for k, v in actual.stage_latency_ms.items() if k != "graph_overhead")


def test_reviewer_order_is_the_plans_not_the_schedulers(pr, repo):
    """The reducer appends in the order LangGraph applies writes; the record
    is in plan order, so the summary reads the same as the asyncio version."""
    async_client = _async(
        WARM,
        _submit("first.", []),
        _submit("second.", []),
        _submit("third.", []),
    )
    result = _run(pr, repo, _sync(*SCOUT), async_client)
    assert result.reviewers_run == ["diff_local", "repo_context", "change_intent"]
    assert list(result.stage_usage) == [
        "scout", "warm_cache", "reviewer:diff_local", "reviewer:repo_context",
        "reviewer:change_intent",
    ]
    assert result.summary == "first. second. third."


def test_documentation_only_change_routes_around_the_scout(repo):
    docs = PullRequest(
        ref=PRRef("o", "r", 2),
        title="Docs",
        files=[ChangedFile("README.md", "modified", patch="+x")],
    )
    sync = _sync()
    async_client = _async(WARM, _submit("", []), _submit("", []))
    result = _run(docs, repo, sync, async_client)
    assert result.reviewers_run == ["diff_local", "change_intent"]
    assert sync.messages.calls == [] and result.brief.startswith("(scout skipped: documentation")


def test_an_explicit_plan_is_honoured(pr, repo):
    plan = ReviewPlan(scout=False, reviewers=(DIFF_LOCAL,), reasons=("test",))
    async_client = _async(WARM, _submit("only me", []))
    result = _run(pr, repo, _sync(), async_client, plan=plan)
    assert result.reviewers_run == ["diff_local"] and result.summary == "only me"


def test_complete_context_skips_the_scout(tmp_path):
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    result = _run(_pr_changing_helper(), _repo_with_conventions(tmp_path), _sync(), async_client)
    assert result.context_complete and not result.scout_ran
    assert result.conventions_file == "CLAUDE.md" and result.callers_found == 2


def test_precomputed_findings_reach_the_scout_and_the_prefix(pr, repo):
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("", []), _submit("", []), _submit("", []))
    _run(pr, repo, sync, async_client, precomputed_findings=[Finding("warning", "n8n", "hot cron")])
    assert "hot cron" in sync.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "hot cron" in async_client.messages.calls[0]["messages"][0]["content"][0]["text"]


# --- what the framework adds: the graph, checkpoints, interrupts -----------------

def test_the_graph_draws_itself():
    drawing = graph.mermaid()
    for node in graph.NODES:
        assert node in drawing
    assert "__start__" in drawing and "__end__" in drawing


def test_interrupt_before_the_verifier_then_resume(pr, repo):
    """The pause-and-resume the ticket names as a reason a framework earns
    its place: stop with the merged findings in hand, look at them, resume
    and the verifier runs on the same thread's state."""
    compiled = graph.build_graph(checkpointer=InMemorySaver(), interrupt_before=["verifier"])
    config = {"configurable": {"thread_id": "pr-9"}}
    sync = _sync(*SCOUT, [_use("verify_findings", verdicts=[{"index": 0, "decision": "drop"}])])
    async_client = _async(
        WARM,
        _submit("", [_finding("blocker", "leaked_secret", "test key")]),
        _submit("", []),
        _submit("", []),
    )

    paused = _run(pr, repo, sync, async_client, verify=True, graph=compiled, config=config)
    assert paused.verified is False and len(paused.findings) == 1
    assert len(sync.messages.calls) == 1, "the verifier has not run"
    snapshot = compiled.get_state(config)
    assert snapshot.next == ("verifier",)
    assert snapshot.values["result"].findings == paused.findings

    resumed = asyncio.run(
        compiled.ainvoke(
            None,
            config=config,
            context=graph.ReviewRun(
                pull_request=pr,
                repo_tools=repo,
                client=sync,
                async_client=async_client,
                model="m",
                max_files_read=5,
                scout_max_turns=8,
                scout_context_turns=3,
                scout_complete_turns=0,
                verify=True,
            ),
        )
    )
    assert len(sync.messages.calls) == 2
    assert resumed["result"].verified is True and resumed["result"].findings == []
    assert [f.category for f in resumed["result"].verifier_dropped] == ["leaked_secret"]
    history = list(compiled.get_state_history(config))
    assert len(history) >= len(graph.NODES), "one checkpoint per superstep, at least"


# --- the switch --------------------------------------------------------------------

def test_review_pull_request_dispatches_on_the_orchestrator_setting(pr, repo, monkeypatch):
    from app.agent import reviewer

    monkeypatch.setattr(
        reviewer,
        "settings",
        Settings(_env_file=None, review_multi_agent=True, review_orchestrator="langgraph"),
    )
    sync = _sync(*SCOUT)
    async_client = _async(WARM, _submit("via graph", []), _submit("", []), _submit("", []))
    result = review_pull_request(pr, repo, client=sync, async_client=async_client)
    assert result.mode == "multi" and result.summary == "via graph"
    assert "graph_overhead" in result.stage_latency_ms


def test_the_default_orchestrator_is_asyncio_and_unknown_names_are_rejected():
    assert Settings(_env_file=None).review_orchestrator == "asyncio"
    with pytest.raises(ValueError, match="asyncio, langgraph"):
        Settings(_env_file=None, review_orchestrator="crewai")


def test_prompt_version_names_the_orchestrator_when_it_is_not_asyncio(monkeypatch):
    monkeypatch.setattr(subject, "settings", Settings(_env_file=None, review_multi_agent=True))
    asyncio_version = subject.prompt_version()
    monkeypatch.setattr(
        subject,
        "settings",
        Settings(_env_file=None, review_multi_agent=True, review_orchestrator="langgraph"),
    )
    langgraph_version = subject.prompt_version()
    assert langgraph_version == asyncio_version + "+langgraph"
    monkeypatch.setattr(
        subject, "settings", Settings(_env_file=None, review_orchestrator="langgraph")
    )
    assert "+langgraph" not in subject.prompt_version(), "meaningless with multi off"
