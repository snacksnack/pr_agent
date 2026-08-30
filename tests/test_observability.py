"""The contract is the no-op: the webhook app factory calls enable_llm_obs
unconditionally, so an untraced environment (tests, CI, a laptop without
DD_API_KEY) must go through it without side effects."""

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
