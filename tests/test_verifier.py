"""Tests for the verifier pass (RC1-387). Offline: a scripted fake client."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agent import verifier
from app.agent.reviewer import review_pull_request
from app.agent.tools import RepoTools
from app.config import Settings
from app.models import Finding, PRRef, PullRequest, ReviewResult


class FakeMessages:
    def __init__(self, scripted, usages=None):
        self._scripted = list(scripted)
        self._usages = list(usages or [])
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if not self._scripted:
            raise AssertionError("fake client ran out of scripted responses")
        content = self._scripted.pop(0)
        usage = None
        if self._usages:
            used_in, used_out = self._usages.pop(0)
            usage = SimpleNamespace(input_tokens=used_in, output_tokens=used_out)
        return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage)


class FakeClient:
    def __init__(self, scripted, usages=None):
        self.messages = FakeMessages(scripted, usages)


def _verdicts(*verdicts):
    return [
        {
            "type": "tool_use",
            "id": "v1",
            "name": "verify_findings",
            "input": {"verdicts": list(verdicts)},
        }
    ]


def _submit(tool_id, summary, findings):
    return {"type": "tool_use", "id": tool_id, "name": "submit_review",
            "input": {"summary": summary, "findings": findings}}


def _finding(severity="warning", category="security", message="thing", file="a.py", line=1):
    return Finding(severity=severity, category=category, message=message, file=file, line=line)


@pytest.fixture()
def pr():
    return PullRequest(
        ref=PRRef("o", "r", 7), title="Add hello", body="adds a greeting",
        base_sha="b" * 12, head_sha="h" * 12, changed_files_count=1,
    )


def _result(*findings):
    return ReviewResult(
        summary="s", findings=list(findings), model="m", input_tokens=100, output_tokens=10
    )


# --- applying verdicts: the rules the model cannot override -----------------

def test_keep_drop_and_downgrade_are_applied_in_order():
    findings = [
        _finding(message="a"),
        _finding(message="b"),
        _finding(message="c", severity="blocker"),
    ]
    kept, dropped, downgraded = verifier.apply_verdicts(
        findings,
        [
            {"index": 0, "decision": "keep"},
            {"index": 1, "decision": "drop", "reason": "deliberate"},
            {"index": 2, "decision": "downgrade", "severity": "warning", "reason": "overstated"},
        ],
    )
    assert [f.message for f in kept] == ["a", "c"]
    assert [f.message for f in dropped] == ["b"]
    assert downgraded == 1
    assert kept[1].severity == "warning"


def test_a_finding_with_no_verdict_is_kept():
    """Nothing is dropped by omission."""
    kept, dropped, downgraded = verifier.apply_verdicts([_finding(), _finding()], [])
    assert len(kept) == 2 and not dropped and downgraded == 0


def test_a_downgrade_never_raises_severity():
    kept, _, downgraded = verifier.apply_verdicts(
        [_finding(severity="nit")], [{"index": 0, "decision": "downgrade", "severity": "warning"}]
    )
    assert kept[0].severity == "nit" and downgraded == 0


def test_a_downgrade_without_a_severity_steps_down_one():
    kept, _, downgraded = verifier.apply_verdicts(
        [_finding(severity="blocker")], [{"index": 0, "decision": "downgrade"}]
    )
    assert kept[0].severity == "warning" and downgraded == 1


def test_unknown_indexes_and_repeats_are_ignored():
    kept, dropped, _ = verifier.apply_verdicts(
        [_finding(message="only")],
        [
            {"index": 5, "decision": "drop"},
            {"index": "0", "decision": "drop"},
            {"index": 0, "decision": "keep"},
            {"index": 0, "decision": "drop"},  # second verdict for 0 loses
        ],
    )
    assert [f.message for f in kept] == ["only"] and not dropped


def test_a_downgrade_does_not_mutate_the_original_finding():
    original = _finding(severity="blocker")
    kept, _, _ = verifier.apply_verdicts([original], [{"index": 0, "decision": "downgrade"}])
    assert original.severity == "blocker" and kept[0].severity == "warning"


# --- the call ---------------------------------------------------------------

def test_verify_findings_returns_a_new_result_with_the_pass_metered(pr):
    client = FakeClient(
        [_verdicts({"index": 1, "decision": "drop", "reason": "fixture"})], usages=[(500, 50)]
    )
    before = _result(_finding(message="real"), _finding(message="decoy"))

    after = verifier.verify_findings(pr, before, client=client, model="verify-model")

    assert [f.message for f in after.findings] == ["real"]
    assert [f.message for f in after.verifier_dropped] == ["decoy"]
    assert after.verified is True
    assert (after.verifier_usage.input_tokens, after.verifier_usage.output_tokens) == (500, 50)
    assert (after.input_tokens, after.output_tokens) == (600, 60), "folded into the totals"
    assert before.findings[1].message == "decoy", "the input result is not mutated"
    assert after.model == "m", "the review's model, not the verifier's, names the result"


def test_the_request_shares_the_system_block_and_forces_the_tool(pr):
    client = FakeClient([_verdicts()])
    verifier.verify_findings(pr, _result(_finding()), client=client, model="verify-model")

    call = client.messages.calls[0]
    assert call["model"] == "verify-model"
    assert call["system"][0]["text"] == verifier.SYSTEM_PROMPT
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in call["tools"]] == ["verify_findings"]
    assert call["tool_choice"] == {"type": "tool", "name": "verify_findings"}
    text = call["messages"][0]["content"][0]["text"]
    assert "Pull request: o/r#7" in text and "[0] warning / security" in text
    assert text.rstrip().endswith("Call verify_findings exactly once.")


def test_a_result_with_no_findings_is_returned_unchanged_without_a_call(pr):
    client = FakeClient([])
    result = _result()
    assert verifier.verify_findings(pr, result, client=client) is result
    assert not client.messages.calls


def test_a_response_without_the_tool_keeps_everything(pr):
    client = FakeClient([[{"type": "text", "text": "I have nothing to say"}]])
    after = verifier.verify_findings(pr, _result(_finding(), _finding()), client=client)
    assert len(after.findings) == 2 and after.verified


def test_model_falls_back_to_the_review_model_then_settings(pr, monkeypatch):
    monkeypatch.setattr(
        verifier, "settings", Settings(_env_file=None, review_verify_model="from-settings")
    )
    client = FakeClient([_verdicts()])
    verifier.verify_findings(pr, _result(_finding()), client=client)
    assert client.messages.calls[0]["model"] == "from-settings"

    monkeypatch.setattr(verifier, "settings", Settings(_env_file=None))
    client = FakeClient([_verdicts()])
    verifier.verify_findings(pr, _result(_finding()), client=client)
    assert client.messages.calls[0]["model"] == "m", "the result's model when nothing is configured"


def test_cache_tokens_are_counted_on_the_verifier_call(pr):
    """RC1-387: the verifier's call is mostly a fresh cache write; pricing
    the uncached input alone would make it look nearly free."""
    client = FakeClient([_verdicts()])
    client.messages._usages = []

    def create(**kwargs):
        client.messages.calls.append(kwargs)
        return SimpleNamespace(
            content=_verdicts(),
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=3000,
                cache_read_input_tokens=200,
            ),
        )

    client.messages.create = create
    after = verifier.verify_findings(pr, _result(_finding()), client=client)
    assert after.verifier_usage.cache_creation_input_tokens == 3000
    assert after.verifier_usage.cache_read_input_tokens == 200
    assert after.verifier_usage.context_tokens == 3210
    assert after.cache_creation_input_tokens == 3000, "folded into the review's totals"


# --- wiring into the loop -----------------------------------------------------

@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return RepoTools(root)


def test_the_loop_runs_the_verifier_when_asked(repo, pr):
    scripted = [
        [_submit("t1", "two findings", [
            {"severity": "warning", "category": "security", "message": "real"},
            {"severity": "warning", "category": "error_handling", "message": "decoy"},
        ])],
        _verdicts({"index": 1, "decision": "drop", "reason": "deliberate"}),
    ]
    client = FakeClient(scripted, usages=[(100, 10), (40, 4)])

    result = review_pull_request(pr, repo, client=client, verify=True)

    assert [f.message for f in result.findings] == ["real"]
    assert result.verified and len(result.verifier_dropped) == 1
    assert (result.input_tokens, result.output_tokens) == (140, 14)
    assert len(client.messages.calls) == 2


def test_the_loop_skips_the_verifier_by_default(repo, pr, monkeypatch):
    import app.agent.reviewer as reviewer

    monkeypatch.setattr(reviewer, "settings", Settings(_env_file=None))
    client = FakeClient([[_submit("t1", "one", [
        {"severity": "warning", "category": "security", "message": "real"},
    ])]])

    result = review_pull_request(pr, repo, client=client)

    assert not result.verified and len(client.messages.calls) == 1


def test_the_loop_skips_the_verifier_on_a_clean_review(repo, pr):
    client = FakeClient([[_submit("t1", "clean", [])]])
    result = review_pull_request(pr, repo, client=client, verify=True)
    assert not result.verified and len(client.messages.calls) == 1


def test_the_forced_submission_path_is_verified_too(repo, pr):
    scripted = [
        [{"type": "text", "text": "hmm"}],  # no tool use -> forced submit
        [_submit("t2", "forced", [{"severity": "nit", "category": "docs", "message": "x"}])],
        _verdicts({"index": 0, "decision": "drop", "reason": "n/a"}),
    ]
    client = FakeClient(scripted)
    result = review_pull_request(pr, repo, client=client, max_tool_turns=1, verify=True)
    assert result.verified and not result.findings and len(result.verifier_dropped) == 1


def test_settings_flag_turns_the_verifier_on(repo, pr, monkeypatch):
    import app.agent.reviewer as reviewer

    monkeypatch.setattr(
        reviewer, "settings", Settings(_env_file=None, review_verify_findings=True)
    )
    client = FakeClient([
        [_submit("t1", "one", [
            {"severity": "warning", "category": "security", "message": "real"},
        ])],
        _verdicts(),
    ])
    result = review_pull_request(pr, repo, client=client)
    assert result.verified and len(client.messages.calls) == 2


# --- RC1-390: the shared-prefix mode -------------------------------------------

def test_default_request_is_the_rc1_387_shape(pr):
    """One text block with everything, one tool, forced tool choice."""
    client = FakeClient([_verdicts()])
    result = ReviewResult(findings=[_finding()], model="m")
    verifier.verify_findings(pr, result, client=client)
    call = client.messages.calls[0]
    assert [t["name"] for t in call["tools"]] == ["verify_findings"]
    assert call["tool_choice"] == {"type": "tool", "name": "verify_findings"}
    content = call["messages"][0]["content"]
    assert len(content) == 1 and content[0]["cache_control"] == {"type": "ephemeral"}
    assert "Pull request:" in content[0]["text"] and "First-pass findings:" in content[0]["text"]


def test_shared_prefix_mode_reads_the_prefix_verbatim_and_sends_the_shared_tools(pr):
    client = FakeClient([_verdicts()])
    result = ReviewResult(findings=[_finding()], model="m")
    tools = [{"name": "submit_review"}, verifier.VERIFY_TOOL]
    verifier.verify_findings(
        pr,
        result,
        client=client,
        shared_prefix="THE PREFIX",
        tools=tools,
        tool_choice={"type": "any"},
    )
    call = client.messages.calls[0]
    assert call["tools"] == tools and call["tool_choice"] == {"type": "any"}
    prefix, suffix = call["messages"][0]["content"]
    assert prefix == {"type": "text", "text": "THE PREFIX", "cache_control": {"type": "ephemeral"}}
    assert "cache_control" not in suffix
    assert suffix["text"].startswith("First-pass findings:")
    assert "Call verify_findings exactly once" in suffix["text"]


def test_wrong_tool_from_the_verifier_keeps_everything(pr):
    """Under tool_choice 'any' the model could call submit_review instead;
    that reads as no verdicts, so nothing is dropped."""
    client = FakeClient([[_submit("v1", "oops", [])]])
    result = ReviewResult(findings=[_finding(), _finding(message="two")], model="m")
    out = verifier.verify_findings(
        pr, result, client=client, shared_prefix="p", tools=[], tool_choice={"type": "any"}
    )
    assert out.verified is True and len(out.findings) == 2 and out.verifier_dropped == []
