"""RC1-474: an experiment-only Cohere client for the reviewer seat.

The pipeline talks to "any object whose ``messages.create`` is a coroutine"
and reads the response through duck-typed accessors, so a second vendor is
an adapter, not a pipeline change: this class accepts the exact request the
pipeline builds for Anthropic (system blocks, text-block content with cache
markers, ``tools`` in Anthropic's schema, ``tool_choice: any``) and answers
with an Anthropic-shaped dict (``content`` holding ``tool_use`` blocks,
``usage`` holding token counts). Nothing in ``app/`` knows it exists — the
eval subject builds it when the review model is a Command model, and the
production roster cannot reach it.

Three deliberate differences from the Anthropic client:

* **The warm-cache call is answered locally.** Cohere v2 has no
  caller-controlled prompt cache, so the pipeline's one-token cache-warming
  request (``max_tokens == 1``, RC1-390's premise) would be a full-price,
  rate-limited round trip that warms nothing. The adapter returns an empty
  response with zero usage instead; every Command-arm case therefore reads
  as "cold cache" in the run summary, which is the truth of the platform,
  not a failure of the run.
* **Requests are paced, and the pacing is not timed as latency.** The trial
  key allows 10 requests/minute — measured via 429s in RC1-473; the
  documented ~20/minute is wrong — so request starts are spaced
  ``min_interval_s`` apart under a lock. The pacing sleep is recorded in
  ``pacing_wait_ms`` and the API round trip in ``api_latency_ms``
  separately, because a latency figure that includes a deliberate sleep is
  the RC1-475 measurement mistake.
* **Prices live here, not in ``agent_evals.pricing``.** The harness table is
  a snapshot of the published first-party Anthropic price list; Cohere's is
  this experiment's concern. ``command-a-plus-05-2026`` has NO published
  per-token price (checked cohere.com/pricing on 2026-09-29 — the flagship
  is a contact-sales tier), so :func:`cost_usd` returns ``None`` for it and
  the run records tokens without an invented dollar figure. A guessed price
  in the store would be worse than a missing one.

The key is read from ``COHERE_API_KEY`` in the environment — not
``app.config.settings``, which is production surface this experiment stays
out of (the ticket's AC: roster untouched, experiment config only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger("evals.cohere")

COHERE_CHAT_URL = "https://api.cohere.com/v2/chat"

#: Trial-key pacing: 10 requests/minute measured (RC1-473), so one request
#: start every 6.5 s keeps a margin under the window.
TRIAL_MIN_INTERVAL_S = 6.5

#: Same per-request ceiling the pipeline's Anthropic clients get.
REQUEST_TIMEOUT_S = 180

#: Attempts per request; 429s sleep and retry, anything else raises.
MAX_ATTEMPTS = 5

#: The pipeline's warm-cache request is exactly one output token (RC1-390).
WARM_MAX_TOKENS = 1

#: Cohere models this experiment may run, mapped to (input, output) USD per
#: million tokens — or ``None`` when the vendor publishes no per-token price.
#: Checked against cohere.com/pricing on 2026-09-29: the current Command
#: generation is absent from the published table (legacy Command R+ 08-2024
#: is the newest generative model with a listed price), so the flagship arm
#: is honestly unpriceable at list.
PRICES: dict[str, tuple[str, str] | None] = {
    "command-a-plus-05-2026": None,
    "command-a-reasoning-08-2025": None,
}


def is_cohere_model(model: str) -> bool:
    """Whether ``model`` is served by Cohere's chat API (the Command family)."""
    return model.startswith("command")


def cost_usd(model: str, usage: Any) -> Decimal | None:
    """Price a Cohere call, or ``None`` when no published price exists.

    ``None`` — not zero: the eval record's convention is that an unknown
    price must never quietly read as free (agent_evals raises for its own
    table; this experiment's table returns the explicit "no list price").
    """
    if model not in PRICES:
        raise KeyError(
            f"no Cohere price entry for {model!r}; add it to evals.cohere.PRICES "
            "rather than letting the run record claim zero cost"
        )
    price = PRICES[model]
    if price is None:
        return None
    input_per_mtok, output_per_mtok = (Decimal(price[0]), Decimal(price[1]))
    return (
        Decimal(usage.input_tokens) * input_per_mtok
        + Decimal(usage.output_tokens) * output_per_mtok
    ) / Decimal("1000000")


def api_key_from_env() -> str | None:
    """The trial key. Environment, not ``settings`` — see the module docstring."""
    return os.environ.get("COHERE_API_KEY") or None


# --- request/response translation -------------------------------------------


def _text(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("text") or "")
    return str(getattr(block, "text", "") or "")


def _cohere_messages(system: Any, messages: list[dict]) -> list[dict]:
    """Anthropic system blocks + text-block messages, as Cohere chat messages.

    Cache markers are dropped (nothing to mark), block lists are joined —
    the pipeline only ever sends text blocks (RC1-427 removed the tools that
    produced anything else).
    """
    out: list[dict] = []
    system_text = "\n\n".join(filter(None, (_text(b) for b in system or [])))
    if system_text:
        out.append({"role": "system", "content": system_text})
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(filter(None, (_text(b) for b in content)))
        out.append({"role": message.get("role", "user"), "content": content or ""})
    return out


def _cohere_tools(tools: list[dict] | None) -> list[dict]:
    """Anthropic tool declarations as Cohere v2 function tools. The schema
    key moves (``input_schema`` -> ``function.parameters``); the JSON Schema
    inside is common ground."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools or []
    ]


def _anthropic_shape(payload: dict) -> dict:
    """A Cohere v2 chat response as the dict the pipeline reads.

    Tool calls become ``tool_use`` blocks with parsed ``input``; a call whose
    arguments do not parse is skipped, which the pipeline already counts (an
    unusable reviewer, a keep-everything verifier) rather than crashes on.
    ``billed_units`` is the usage source — it is what the account is charged
    for, and the comparison is a billing comparison.
    """
    message = payload.get("message") or {}
    content: list[dict] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            logger.warning(
                "cohere_tool_call_unparseable name=%s", function.get("name")
            )
            continue
        content.append(
            {"type": "tool_use", "name": function.get("name"), "input": arguments}
        )
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            content.append({"type": "text", "text": block.get("text", "")})
    billed = (payload.get("usage") or {}).get("billed_units") or {}
    return {
        "content": content,
        "stop_reason": payload.get("finish_reason"),
        "usage": {
            "input_tokens": int(billed.get("input_tokens") or 0),
            "output_tokens": int(billed.get("output_tokens") or 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


_WARM_RESPONSE = {
    "content": [],
    "stop_reason": "warm_cache_noop",
    "usage": {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    },
}


# --- the client ---------------------------------------------------------------


class _Messages:
    def __init__(self, client: AsyncCohereReviewClient) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> dict:
        return await self._client._create(**kwargs)


class AsyncCohereReviewClient:
    """The pipeline's client contract, over Cohere v2 chat.

    ``transport`` and ``min_interval_s`` exist for the offline tests; the
    real runs take the defaults. The caller closes it (the pipeline's rule
    for injected clients), and the latency/pacing counters survive the close
    so the subject can report them.
    """

    def __init__(
        self,
        api_key: str,
        *,
        min_interval_s: float = TRIAL_MIN_INTERVAL_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT_S,
            transport=transport,
        )
        self._min_interval_s = min_interval_s
        self._pace_lock = asyncio.Lock()
        self._last_start = 0.0
        self.messages = _Messages(self)
        #: Round-trip times of real API calls, pacing excluded (RC1-475).
        self.api_latency_ms: list[float] = []
        #: Total time spent waiting on the trial-key pacing, not the API.
        self.pacing_wait_ms: float = 0.0
        self.warm_noops = 0

    async def close(self) -> None:
        await self._http.aclose()

    async def _create(
        self,
        *,
        model: str,
        system: Any = None,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int = 4096,
        **_ignored: Any,
    ) -> dict:
        if max_tokens == WARM_MAX_TOKENS:
            # The pipeline warming a cache this platform does not have.
            self.warm_noops += 1
            return dict(_WARM_RESPONSE)
        body: dict[str, Any] = {
            "model": model,
            "messages": _cohere_messages(system, messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = _cohere_tools(tools)
            # The pipeline's {"type": "any"} (a tool call is required) has no
            # translation: the flagship rejects Cohere's own ``tool_choice``
            # outright ("tool_choice is not supported for this model",
            # measured 2026-09-29), so it is not sent and the prompt's "call
            # submit_review exactly once" is the only forcing. The pipeline
            # already treats a missing tool call as an unusable reviewer or a
            # keep-everything verifier, so an answer in prose is counted, not
            # crashed on — and how often that happens is part of the result.
        return _anthropic_shape(await self._post(body))

    async def _post(self, body: dict) -> dict:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            async with self._pace_lock:
                wait = self._last_start + self._min_interval_s - time.monotonic()
                if wait > 0:
                    self.pacing_wait_ms += wait * 1000
                    await asyncio.sleep(wait)
                self._last_start = time.monotonic()
            started = time.perf_counter()
            response = await self._http.post(COHERE_CHAT_URL, json=body)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if response.status_code == 429 and attempt < MAX_ATTEMPTS:
                # Rate-limited calls are pacing, not model latency: a 429
                # counted as an arm's latency (or worse, as a lost case)
                # would charge the trial key's window to the model (AC4).
                delay = float(response.headers.get("retry-after") or 0) or (
                    self._min_interval_s * attempt
                )
                logger.warning(
                    "cohere_429 attempt=%d retry_in=%.1fs", attempt, delay
                )
                self.pacing_wait_ms += delay * 1000
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            self.api_latency_ms.append(elapsed_ms)
            return response.json()
        raise RuntimeError("unreachable: the loop returns or raises")
