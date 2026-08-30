"""The contract is the no-op: the webhook app factory calls enable_llm_obs
unconditionally, so an untraced environment (tests, CI, a laptop without
DD_API_KEY) must go through it without side effects."""

import os

from app import observability


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
