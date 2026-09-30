"""RC1-474: the experiment-only Cohere adapter, offline.

The adapter's one job is to be indistinguishable from the Anthropic client
at the pipeline's seam: accept the exact request the pipeline builds, answer
in the shape the pipeline reads. These tests hold both directions with a
mocked transport, plus the three behaviours that are deliberately not
Anthropic-like — the warm-cache no-op, the trial-key pacing kept out of the
latency figures, and the honest non-price for a model with no list price.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agent.pipeline import _submission
from app.agent.prompts import SUBMIT_TOOL
from app.models import TokenUsage
from evals import cohere, subject


def _chat_response(tool_name: str = "submit_review", arguments: str | None = None) -> dict:
    return {
        "id": "resp-1",
        "finish_reason": "TOOL_CALL",
        "message": {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": arguments
                        if arguments is not None
                        else json.dumps({"summary": "Looks fine.", "findings": []}),
                    },
                }
            ],
        },
        "usage": {
            "billed_units": {"input_tokens": 1200, "output_tokens": 80},
            "tokens": {"input_tokens": 1300, "output_tokens": 90},
        },
    }


def _client(handler, min_interval_s: float = 0.0) -> cohere.AsyncCohereReviewClient:
    return cohere.AsyncCohereReviewClient(
        "test-key",
        min_interval_s=min_interval_s,
        transport=httpx.MockTransport(handler),
    )


def _pipeline_request() -> dict:
    """The request exactly as the pipeline sends it to any client."""
    return {
        "model": "command-a-plus-05-2026",
        "system": [
            {"type": "text", "text": "You are a reviewer.", "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "PREFIX", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "SUFFIX"},
                ],
            }
        ],
        "tools": [SUBMIT_TOOL],
        "tool_choice": {"type": "any"},
        "max_tokens": 4096,
    }


def test_warm_cache_request_is_answered_locally():
    """Cohere has no prompt cache to warm: the pipeline's one-token warm
    call must not spend a rate-limited round trip."""

    def handler(request):  # pragma: no cover - the point is it never runs
        raise AssertionError("the warm call must not reach the API")

    client = _client(handler)

    async def go():
        kwargs = _pipeline_request() | {"max_tokens": 1}
        response = await client.messages.create(**kwargs)
        await client.close()
        return response

    response = asyncio.run(go())
    assert response["usage"]["input_tokens"] == 0
    assert response["usage"]["output_tokens"] == 0
    assert response["content"] == []
    assert client.warm_noops == 1
    assert client.api_latency_ms == []


def test_request_is_translated_to_cohere_v2():
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response())

    client = _client(handler)

    async def go():
        response = await client.messages.create(**_pipeline_request())
        await client.close()
        return response

    asyncio.run(go())
    body = seen[0]
    assert body["model"] == "command-a-plus-05-2026"
    assert body["messages"][0] == {"role": "system", "content": "You are a reviewer."}
    assert body["messages"][1]["role"] == "user"
    assert body["messages"][1]["content"] == "PREFIX\nSUFFIX"
    assert "cache_control" not in json.dumps(body), "cache markers mean nothing here"
    (tool,) = body["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == SUBMIT_TOOL["name"]
    assert tool["function"]["parameters"] == SUBMIT_TOOL["input_schema"]
    assert body["tool_choice"] == "REQUIRED"
    assert body["max_tokens"] == 4096


def test_response_reads_as_an_anthropic_response():
    """The pipeline's own submission reader must find the tool call, and the
    usage must be the billed counts — the comparison is a billing one."""

    def handler(request):
        return httpx.Response(200, json=_chat_response())

    client = _client(handler)

    async def go():
        response = await client.messages.create(**_pipeline_request())
        await client.close()
        return response

    response = asyncio.run(go())
    payload = _submission(response)
    assert payload == {"summary": "Looks fine.", "findings": []}
    assert response["usage"]["input_tokens"] == 1200
    assert response["usage"]["output_tokens"] == 80
    assert response["usage"]["cache_read_input_tokens"] == 0
    assert len(client.api_latency_ms) == 1


def test_unparseable_tool_arguments_become_no_submission():
    """A garbled tool call is the pipeline's existing "unusable reviewer"
    path, not a crash."""

    def handler(request):
        return httpx.Response(200, json=_chat_response(arguments="{not json"))

    client = _client(handler)

    async def go():
        response = await client.messages.create(**_pipeline_request())
        await client.close()
        return response

    response = asyncio.run(go())
    assert _submission(response) is None


def test_429_is_retried_and_never_counted_as_latency():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return httpx.Response(200, json=_chat_response())

    client = _client(handler)

    async def go():
        response = await client.messages.create(**_pipeline_request())
        await client.close()
        return response

    response = asyncio.run(go())
    assert calls["n"] == 2
    assert _submission(response) is not None
    assert len(client.api_latency_ms) == 1, "the 429 round trip is pacing, not model latency"


def test_pacing_wait_is_recorded_apart_from_latency():
    def handler(request):
        return httpx.Response(200, json=_chat_response())

    client = _client(handler, min_interval_s=0.05)

    async def go():
        await client.messages.create(**_pipeline_request())
        await client.messages.create(**_pipeline_request())
        await client.close()

    asyncio.run(go())
    assert client.pacing_wait_ms > 0, "the second call must wait out the interval"
    assert len(client.api_latency_ms) == 2


def test_flagship_command_has_no_invented_price():
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cohere.cost_usd("command-a-plus-05-2026", usage) is None
    with pytest.raises(KeyError):
        cohere.cost_usd("command-made-up", usage)


def test_subject_routes_command_models_to_the_experiment_table():
    usage = TokenUsage(input_tokens=1000, output_tokens=100)
    assert subject._cost_usd("command-a-plus-05-2026", usage) is None
    assert subject._cost_repr("command-a-plus-05-2026", usage) == "unpriced"
    assert subject._cost_usd("claude-sonnet-4-6", usage) is not None


def test_preflight_wants_the_cohere_key_for_a_command_model(monkeypatch):
    monkeypatch.setattr(subject.settings, "review_model", "command-a-plus-05-2026")
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
        subject.preflight()
    monkeypatch.setenv("COHERE_API_KEY", "trial")
    subject.preflight()  # the Anthropic key is not required on this arm
