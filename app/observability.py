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
import sys

try:  # documented optional-dep exception: ddtrace is absent in minimal envs
    from ddtrace.llmobs import LLMObs
except ImportError:  # pragma: no cover - exercised only without ddtrace
    LLMObs = None


def enable_llm_obs(ml_app: str, *, service: str | None = None) -> bool:
    """Turn on tracing for this process, or quietly decline. Returns whether
    tracing is on; safe to call more than once.

    RC1-331 (mirrored from agent-evals v0.4.1): `LLMObs.enable()` patches
    ddtrace's entire LLM integration list with `raise_errors=True`, so a
    module-name collision or version mismatch would crash the webhook boot
    for the sake of its decoration. Only the anthropic integration is left
    on, and any failure to start tracing is a decline, not an error.
    """
    if LLMObs is None or not os.environ.get("DD_API_KEY"):
        return False
    _restrict_patching_to_anthropic()
    try:
        LLMObs.enable(
            ml_app=ml_app,
            agentless_enabled=True,
            site=os.environ.get("DD_SITE", "datadoghq.com"),
            service=service or ml_app,
        )
    except Exception as exc:
        print(f"llmobs: tracing disabled, enable() failed: {exc}", file=sys.stderr)
        return False
    return True


def _llm_integration_modules() -> tuple[str, ...]:
    """The module names `LLMObs.enable()` would patch; empty when unknown.

    Read from ddtrace's own constants — the same two lists its
    `_patch_integrations` concatenates — so the set tracks the installed
    version. Private imports, guarded: if they move in a future ddtrace we
    fall back to patching everything, and the try/except above still keeps
    the process alive.
    """
    try:
        from ddtrace.llmobs._constants import SUPPORTED_LLMOBS_INTEGRATIONS
        from ddtrace.llmobs._llmobs import _INTEGRATIONS_W_PROPAGATION_SUPPORT
    except ImportError:  # pragma: no cover - exercised only on a moved layout
        return ()
    modules = set(SUPPORTED_LLMOBS_INTEGRATIONS.values())
    modules |= set(_INTEGRATIONS_W_PROPAGATION_SUPPORT.values())
    return tuple(modules)


def _restrict_patching_to_anthropic() -> None:
    """Env-default every non-anthropic LLM integration off (RC1-331).

    setdefault, not setenv: an explicitly configured `DD_TRACE_<X>_ENABLED`
    in the environment still wins.
    """
    for module in _llm_integration_modules():
        if module == "anthropic":
            continue
        os.environ.setdefault(f"DD_TRACE_{module.upper().replace('-', '_')}_ENABLED", "false")
