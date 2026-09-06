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

import logging
import os
import re
import sys
import time
from contextlib import nullcontext
from typing import Any

import httpx

from app.models import ReviewResult
from app.pricing import ReviewCost, UnknownModelPrice, review_cost

try:  # documented optional-dep exception: ddtrace is absent in minimal envs
    from ddtrace.llmobs import LLMObs
except ImportError:  # pragma: no cover - exercised only without ddtrace
    LLMObs = None

logger = logging.getLogger("app.observability")

ML_APP = "pr-review-agent"

#: One point per review, never per call (RC1-395). Distributions, so a
#: dashboard can draw p50/p95 and a monitor can watch p95 over a day; the
#: RC1-377 monitor is cost per *call* and goes down when the multi-agent
#: path replaces one long loop with six short calls.
COST_METRIC = "pr_agent.review.cost_usd"
LATENCY_METRIC = "pr_agent.review.latency_s"
METRIC_TIMEOUT_S = 10


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


def stage_span(kind: str, name: str):
    """A context manager for one stage of a multi-agent review (RC1-390).

    ``kind`` is an LLM Observability span kind — ``workflow`` for the review
    as a whole, ``agent`` for the scout, each reviewer and the verifier,
    ``task`` for a step with no model of its own. The auto-instrumented
    Anthropic calls made inside become its children, which is what turns a
    review from N unrelated root spans into one tree. A no-op when tracing
    is off, so the review code never checks.
    """
    if LLMObs is None or not LLMObs.enabled:
        return nullcontext()
    starter = getattr(LLMObs, kind)
    return starter(name=name)


def annotate_span(**fields: Any) -> None:
    """Attach ``metadata``/``metrics``/``output_data`` to the active LLM
    Observability span; a no-op when tracing is off."""
    if LLMObs is None or not LLMObs.enabled:
        return
    try:
        LLMObs.annotate(**fields)
    except Exception as exc:  # noqa: BLE001 — decoration must never fail a review
        print(f"llmobs: annotate failed: {exc}", file=sys.stderr)


# --- cost per review (RC1-395) ------------------------------------------------

def annotate_review_cost(result: ReviewResult) -> ReviewCost | None:
    """Price a finished review and write the price onto the active workflow
    span: ``cost_usd``, one ``stage_cost_usd_<stage>`` per stage, and
    ``latency_s`` as metrics; the path, the scout and the conventions file
    as metadata. Called by the dispatcher while the span is open, so the
    numbers land on the trace's root rather than on any one call.

    Returns the cost, or ``None`` when the model has no price on file — the
    review is then logged as unpriced and the span gets no cost. Never
    raises: decoration must not fail a review that has already been paid for.
    """
    try:
        cost = review_cost(result)
    except UnknownModelPrice as exc:
        logger.warning("review_unpriced %s", exc)
        return None
    latency_s = result.latency_ms / 1000
    logger.info(
        "review_cost mode=%s cost_usd=%.4f latency_s=%.1f %s",
        result.mode,
        cost.total,
        latency_s,
        " ".join(f"{stage}={usd:.4f}" for stage, usd in cost.stages.items()),
    )
    metrics: dict[str, float] = {"cost_usd": float(cost.total), "latency_s": latency_s}
    for stage, usd in cost.stages.items():
        metrics[stage_metric_key(stage)] = float(usd)
    annotate_span(
        metadata={
            "mode": result.mode,
            "scout": _scout_tag(result),
            "scout_turns": result.tool_turns if result.mode == "multi" else None,
            "verified": result.verified,
            "conventions_file": result.conventions_file,
        },
        metrics=metrics,
    )
    return cost


def stage_metric_key(stage: str) -> str:
    """``reviewer:diff_local`` → ``stage_cost_usd_reviewer_diff_local``. LLM
    Observability drops a span whose metric key contains a dot (found on the
    first live run: ddtrace warns and rewrites it), so the stage names, which
    carry colons, are folded to word characters before they become keys."""
    return "stage_cost_usd_" + re.sub(r"[^A-Za-z0-9_]", "_", stage)


def _scout_tag(result: ReviewResult) -> str:
    if result.mode != "multi":
        return "none"
    return "ran" if result.scout_ran else "skipped"


def review_metric_tags(result: ReviewResult, *, repo: str) -> list[str]:
    """The tag set both metrics carry. Kept small on purpose — every distinct
    combination is a billable custom metric, five more once percentiles are
    on — and chosen so the dashboard can split by path, by repo and by model,
    which are the three things that change a review's price."""
    return [
        f"ml_app:{ML_APP}",
        f"repo:{repo}",
        f"mode:{result.mode}",
        f"scout:{_scout_tag(result)}",
        f"verified:{str(result.verified).lower()}",
        f"model:{result.model}",
    ]


def review_metric_points(
    result: ReviewResult, *, repo: str, at: int | None = None
) -> list[dict[str, Any]]:
    """The v1 distribution-points payload for one review. Pure — tests read
    this rather than a network. Empty when the model has no price: a gap
    means unmeasured, a zero would mean free."""
    try:
        cost = review_cost(result)
    except UnknownModelPrice:
        return []
    at = at or int(time.time())
    tags = review_metric_tags(result, repo=repo)
    return [
        {"metric": COST_METRIC, "points": [[at, [float(cost.total)]]], "tags": tags},
        {"metric": LATENCY_METRIC, "points": [[at, [result.latency_ms / 1000]]], "tags": tags},
    ]


def ship_review_metrics(result: ReviewResult, *, repo: str) -> bool:
    """Submit the review's cost and latency to Datadog, agentless, from the
    webhook. A no-op without ``DD_API_KEY``; any failure is logged and
    swallowed, since the review is already posted or about to be and a
    metric is not worth a retry. Returns whether a point was sent."""
    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return False
    series = review_metric_points(result, repo=repo)
    if not series:
        return False
    site = os.environ.get("DD_SITE", "datadoghq.com")
    try:
        resp = httpx.post(
            f"https://api.{site}/api/v1/distribution_points",
            json={"series": series},
            headers={"DD-API-KEY": api_key},
            timeout=METRIC_TIMEOUT_S,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a metric must never fail a review
        logger.warning("review_metrics_failed %s", exc)
        return False
    return True
