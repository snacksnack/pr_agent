"""Tests for the verifier pass (RC1-387). Offline: a scripted fake client."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from app.agent import verifier
from app.config import Settings
from app.models import Finding, ReviewResult


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


def _result(*findings):
    return ReviewResult(
        summary="s", findings=list(findings), model="m", input_tokens=100, output_tokens=10
    )


SHARED_TOOLS = [{"name": "submit_review"}, verifier.VERIFY_TOOL]
TOOL_CHOICE_ANY = {"type": "any"}


def _verify(result, client, **kwargs):
    """The call as the pipeline makes it: the reviewers' prefix, tools and
    tool choice (RC1-390); the one shape there is since RC1-428."""
    return verifier.verify_findings(
        result,
        client=client,
        prefix=kwargs.pop("prefix", "THE PREFIX"),
        tools=SHARED_TOOLS,
        tool_choice=TOOL_CHOICE_ANY,
        **kwargs,
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

def test_verify_findings_returns_a_new_result_with_the_pass_metered():
    client = FakeClient(
        [_verdicts({"index": 1, "decision": "drop", "reason": "fixture"})], usages=[(500, 50)]
    )
    before = _result(_finding(message="real"), _finding(message="decoy"))

    after = _verify(before, client, model="verify-model")

    assert [f.message for f in after.findings] == ["real"]
    assert [f.message for f in after.verifier_dropped] == ["decoy"]
    assert after.verified is True
    assert (after.verifier_usage.input_tokens, after.verifier_usage.output_tokens) == (500, 50)
    assert (after.input_tokens, after.output_tokens) == (600, 60), "folded into the totals"
    assert before.findings[1].message == "decoy", "the input result is not mutated"
    assert after.model == "m", "the review's model, not the verifier's, names the result"


def test_the_request_reads_the_reviewers_prefix_and_sends_their_tools():
    client = FakeClient([_verdicts()])
    _verify(_result(_finding()), client, model="verify-model")

    call = client.messages.calls[0]
    assert call["model"] == "verify-model"
    assert call["system"][0]["text"] == verifier.SYSTEM_PROMPT
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["tools"] == SHARED_TOOLS and call["tool_choice"] == TOOL_CHOICE_ANY
    prefix, suffix = call["messages"][0]["content"]
    assert prefix == {"type": "text", "text": "THE PREFIX", "cache_control": {"type": "ephemeral"}}
    assert "cache_control" not in suffix
    assert suffix["text"].startswith("First-pass findings:")
    assert "[0] warning / security" in suffix["text"]
    assert suffix["text"].rstrip().endswith("Call verify_findings exactly once.")


def test_a_result_with_no_findings_is_returned_unchanged_without_a_call():
    client = FakeClient([])
    result = _result()
    assert _verify(result, client) is result
    assert not client.messages.calls


def test_a_response_without_the_tool_keeps_everything():
    client = FakeClient([[{"type": "text", "text": "I have nothing to say"}]])
    after = _verify(_result(_finding(), _finding()), client)
    assert len(after.findings) == 2 and after.verified


def test_model_falls_back_to_the_review_model_then_settings(monkeypatch):
    client = FakeClient([_verdicts()])
    _verify(_result(_finding()), client)
    assert client.messages.calls[0]["model"] == "m", "the review's model"

    monkeypatch.setattr(
        verifier, "settings", Settings(_env_file=None, review_model="from-settings")
    )
    client = FakeClient([_verdicts()])
    _verify(ReviewResult(findings=[_finding()]), client)
    assert client.messages.calls[0]["model"] == "from-settings"


def test_cache_tokens_are_counted_on_the_verifier_call():
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
    after = _verify(_result(_finding()), client)
    assert after.verifier_usage.cache_creation_input_tokens == 3000
    assert after.verifier_usage.cache_read_input_tokens == 200
    assert after.verifier_usage.context_tokens == 3210
    assert after.cache_creation_input_tokens == 3000, "folded into the review's totals"


# --- wiring into the loop -----------------------------------------------------

# --- the instructions -----------------------------------------------------

def test_the_instructions_carry_the_absence_rule_and_the_duplicate_fold():
    """RC1-394's absence rule and RC1-398's duplicate-fold sentence are part
    of the one instruction text since RC1-428; nothing is appended per call."""
    client = FakeClient([_verdicts()])
    _verify(_result(_finding()), client)
    text = client.messages.calls[0]["messages"][0]["content"][1]["text"]
    assert text.endswith(verifier.VERIFIER_INSTRUCTIONS)
    assert verifier.ABSENCE_RULE in verifier.VERIFIER_INSTRUCTIONS
    assert "absence from a bounded search is not evidence" in verifier.ABSENCE_RULE
    assert "not a blocker" in verifier.ABSENCE_RULE
    assert "Their order in the list above means nothing" in verifier.VERIFIER_INSTRUCTIONS
    assert "do not keep both because their wording differs" in verifier.VERIFIER_INSTRUCTIONS


def test_the_verifier_carries_every_other_field_of_the_result_through():
    # RC1-425: the pipeline assembles the final result after this pass, so a
    # field the verifier does not touch must survive it (dataclasses.replace).
    result = _result(_finding())
    result.conventions_file, result.callers_found, result.latency_ms = "CLAUDE.md", 3, 12.5
    result.stage_latency_ms = {"context": 1.0}
    client = FakeClient([_verdicts()])
    out = _verify(result, client)
    assert out.verified is True
    assert (out.conventions_file, out.callers_found, out.latency_ms) == ("CLAUDE.md", 3, 12.5)
    assert out.stage_latency_ms == {"context": 1.0}
