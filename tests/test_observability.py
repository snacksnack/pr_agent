"""The contract is the no-op: the webhook app factory calls enable_llm_obs
unconditionally, so an untraced environment (tests, CI, a laptop without
DD_API_KEY) must go through it without side effects."""

import os
import re
from decimal import Decimal

import httpx

from app import observability
from app.models import ReviewResult, TokenUsage


class FakeLLMObs:
    def __init__(self):
        self.enabled_with = None

    def enable(self, **kwargs):
        self.enabled_with = kwargs


def test_declines_without_api_key(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.setattr(observability, "LLMObs", FakeLLMObs())
    assert observability.enable_llm_obs("pr-review-agent") is False


def test_declines_without_ddtrace(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setattr(observability, "LLMObs", None)
    assert observability.enable_llm_obs("pr-review-agent") is False


def test_enables_agentless_with_key(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.delenv("DD_SITE", raising=False)
    fake = FakeLLMObs()
    monkeypatch.setattr(observability, "LLMObs", fake)
    assert observability.enable_llm_obs("pr-review-agent", service="webhook") is True
    assert fake.enabled_with["ml_app"] == "pr-review-agent"
    assert fake.enabled_with["agentless_enabled"] is True
    assert fake.enabled_with["service"] == "webhook"
    assert fake.enabled_with["site"] == "datadoghq.com"


def test_declines_instead_of_raising_when_patching_crashes(monkeypatch, capsys):
    """RC1-331: an integration patch failure must not crash the webhook boot."""
    monkeypatch.setenv("DD_API_KEY", "k")
    # Keep the env-defaulting out of this test: no lingering DD_TRACE_* vars.
    monkeypatch.setattr(observability, "_llm_integration_modules", tuple)

    class CrashingLLMObs(FakeLLMObs):
        def enable(self, **kwargs):
            raise ModuleNotFoundError("No module named 'mcp.shared.session'")

    monkeypatch.setattr(observability, "LLMObs", CrashingLLMObs())
    assert observability.enable_llm_obs("pr-review-agent") is False
    assert "mcp.shared.session" in capsys.readouterr().err


def test_non_anthropic_integrations_are_defaulted_off(monkeypatch):
    """RC1-331: the estate is Anthropic-only; nothing else gets patched."""
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setattr(
        observability,
        "_llm_integration_modules",
        lambda: ("anthropic", "openai_agents", "google-genai"),
    )
    monkeypatch.delenv("DD_TRACE_ANTHROPIC_ENABLED", raising=False)
    monkeypatch.delenv("DD_TRACE_OPENAI_AGENTS_ENABLED", raising=False)
    # An explicit environment setting must survive the defaulting.
    monkeypatch.setenv("DD_TRACE_GOOGLE_GENAI_ENABLED", "true")
    monkeypatch.setattr(observability, "LLMObs", FakeLLMObs())

    assert observability.enable_llm_obs("pr-review-agent") is True
    assert "DD_TRACE_ANTHROPIC_ENABLED" not in os.environ
    assert os.environ["DD_TRACE_OPENAI_AGENTS_ENABLED"] == "false"
    assert os.environ["DD_TRACE_GOOGLE_GENAI_ENABLED"] == "true"


# --- cost per review (RC1-395) ------------------------------------------------


class RecordingLLMObs(FakeLLMObs):
    enabled = True

    def __init__(self):
        super().__init__()
        self.annotations = []

    def annotate(self, **fields):
        self.annotations.append(fields)


def _never_post(*args, **kwargs):
    raise AssertionError("posted")


def _result(**kw):
    kw.setdefault("model", "claude-sonnet-4-6")
    kw.setdefault("output_tokens", 1000)
    kw.setdefault("cache_read_input_tokens", 20000)
    kw.setdefault("latency_ms", 12500.0)
    return ReviewResult(**kw)


def test_annotate_puts_cost_stages_and_latency_on_the_span(monkeypatch):
    fake = RecordingLLMObs()
    monkeypatch.setattr(observability, "LLMObs", fake)
    verifier = TokenUsage(output_tokens=100, cache_read_input_tokens=9000)
    result = _result(verified=True, verifier_usage=verifier, verifier_model="claude-sonnet-4-6")

    cost = observability.annotate_review_cost(result)

    assert cost is not None and cost.total > 0
    [annotation] = fake.annotations
    metrics = annotation["metrics"]
    assert metrics["cost_usd"] == float(cost.total)
    assert metrics["latency_s"] == 12.5
    assert metrics["stage_cost_usd_loop"] == float(cost.stages["loop"])
    assert metrics["stage_cost_usd_verifier"] == float(cost.stages["verifier"])
    assert all(isinstance(v, float) for v in metrics.values()), "LLMObs rejects Decimal"
    meta = annotation["metadata"]
    assert meta["mode"] == "multi" and meta["scout"] == "skipped" and meta["verified"] is True


def test_annotate_multi_carries_the_scout_and_its_turns(monkeypatch):
    fake = RecordingLLMObs()
    monkeypatch.setattr(observability, "LLMObs", fake)
    result = _result(
        mode="multi", scout_ran=True, tool_turns=3, conventions_file="CLAUDE.md",
        stage_usage={
            "scout": TokenUsage(output_tokens=50),
            "reviewer:diff_local": TokenUsage(output_tokens=50),
        },
    )
    observability.annotate_review_cost(result)
    [annotation] = fake.annotations
    assert annotation["metadata"]["scout"] == "ran"
    assert annotation["metadata"]["scout_turns"] == 3
    assert annotation["metadata"]["conventions_file"] == "CLAUDE.md"
    # LLM Obs drops a span whose metric key has a dot; stage names carry colons.
    assert "stage_cost_usd_scout" in annotation["metrics"]
    assert "stage_cost_usd_reviewer_diff_local" in annotation["metrics"]
    assert all(re.fullmatch(r"\w+", k) for k in annotation["metrics"])


def test_annotate_carries_whether_the_context_was_complete(monkeypatch):
    fake = RecordingLLMObs()
    monkeypatch.setattr(observability, "LLMObs", fake)
    observability.annotate_review_cost(_result(mode="multi", context_complete=True))
    assert fake.annotations[0]["metadata"]["context_complete"] is True


def test_review_identity_tags_the_span_with_repo_pr_and_head_sha(monkeypatch):
    """RC1-394: the PR behind a review is on the span, never on the metric —
    tags on a span are free, tags on the metric are billable per value."""
    from app.models import PRRef, PullRequest

    fake = RecordingLLMObs()
    monkeypatch.setattr(observability, "LLMObs", fake)
    pr = PullRequest(ref=PRRef("snacksnack", "pr_agent", 41), head_sha="abc123")
    observability.annotate_review_identity(pr)
    [annotation] = fake.annotations
    assert annotation["tags"] == {"repo": "snacksnack/pr_agent", "pr": "41", "head_sha": "abc123"}
    assert all(isinstance(v, str) for v in annotation["tags"].values())

    monkeypatch.setattr(observability, "LLMObs", None)
    observability.annotate_review_identity(pr)  # a no-op, never raises


def test_unpriced_model_is_logged_and_not_annotated(monkeypatch, caplog):
    fake = RecordingLLMObs()
    monkeypatch.setattr(observability, "LLMObs", fake)
    with caplog.at_level("WARNING", logger="app.observability"):
        assert observability.annotate_review_cost(_result(model="m")) is None
    assert fake.annotations == []
    assert "review_unpriced" in caplog.text


def test_annotate_is_a_no_op_when_tracing_is_off(monkeypatch):
    monkeypatch.setattr(observability, "LLMObs", None)
    cost = observability.annotate_review_cost(_result())
    assert cost is not None, "pricing still happens; only the span is skipped"


def test_metric_points_are_one_per_review_with_the_tag_set():
    result = _result(mode="multi", scout_ran=False, verified=True)
    points = observability.review_metric_points(result, repo="o/r", at=1700000000)
    assert [p["metric"] for p in points] == [
        "pr_agent.review.cost_usd", "pr_agent.review.latency_s"
    ]
    cost, latency = points
    assert cost["points"] == [[1700000000, [float(observability.review_cost(result).total)]]]
    assert latency["points"] == [[1700000000, [12.5]]]
    assert cost["tags"] == latency["tags"] == [
        "ml_app:pr-review-agent",
        "repo:o/r",
        "mode:multi",
        "scout:skipped",
        "verified:true",
        "model:claude-sonnet-4-6",
    ]


def test_metric_points_are_empty_for_an_unpriced_model():
    """A gap means unmeasured; a zero would mean free."""
    assert observability.review_metric_points(_result(model="m"), repo="o/r") == []


def test_ship_declines_without_api_key(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "post", _never_post)
    assert observability.ship_review_metrics(_result(), repo="o/r") is False


def test_ship_posts_distribution_points_agentless(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setenv("DD_SITE", "datadoghq.eu")
    sent = {}

    def fake_post(url, *, json, headers, timeout):
        sent.update(url=url, json=json, headers=headers, timeout=timeout)
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert observability.ship_review_metrics(_result(), repo="o/r") is True
    assert sent["url"] == "https://api.datadoghq.eu/api/v1/distribution_points"
    assert sent["headers"] == {"DD-API-KEY": "k"}
    assert [s["metric"] for s in sent["json"]["series"]] == [
        "pr_agent.review.cost_usd", "pr_agent.review.latency_s"
    ]
    assert Decimal(str(sent["json"]["series"][0]["points"][0][1][0])) > 0


def test_ship_swallows_a_failed_post(monkeypatch, caplog):
    monkeypatch.setenv("DD_API_KEY", "k")

    def failing_post(url, **kw):
        return httpx.Response(403, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", failing_post)
    with caplog.at_level("WARNING", logger="app.observability"):
        assert observability.ship_review_metrics(_result(), repo="o/r") is False
    assert "review_metrics_failed" in caplog.text


def test_ship_sends_nothing_for_an_unpriced_model(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", _never_post)
    assert observability.ship_review_metrics(_result(model="m"), repo="o/r") is False
