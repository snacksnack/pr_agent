"""Datadog LLM Observability for the deployed webhook (RC1-322).

Mirrors the platform's `observability.py`, for the same reason it exists
there: `agent_evals.llmobs` is the dev-side helper (the evals CLI uses its
per-case spans), but it is pinned by *git* ref, and the runtime image is
`python:3.12-slim` with no git — the webhook cannot import it in production
without shipping the whole harness. `ddtrace` comes from PyPI, so the
runtime carries only this enable call. Agentless on purpose (no local
Datadog agent daemon on Fly); a no-op without `DD_API_KEY`, so tests and
uninstrumented machines run identical code.
"""

from __future__ import annotations

import os

try:  # documented optional-dep exception: ddtrace is absent in minimal envs
    from ddtrace.llmobs import LLMObs
except ImportError:  # pragma: no cover - exercised only without ddtrace
    LLMObs = None


def enable_llm_obs(ml_app: str, *, service: str | None = None) -> bool:
    """Turn on tracing for this process, or quietly decline. Returns whether
    tracing is on; safe to call more than once."""
    if LLMObs is None or not os.environ.get("DD_API_KEY"):
        return False
    LLMObs.enable(
        ml_app=ml_app,
        agentless_enabled=True,
        site=os.environ.get("DD_SITE", "datadoghq.com"),
        service=service or ml_app,
    )
    return True
