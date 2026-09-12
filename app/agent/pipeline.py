"""The review pipeline: context, routed reviewers, merge, verifier (RC1-390, RC1-422).

One production pipeline (RC1-422 retired the single loop that explored and
judged in one conversation; RC1-427 retired the scout that explored for the
reviewers). The review's graph is explicit in plain Python:

    plan (router) -> context (Python) -> warm cache -> reviewers (gather)
        -> merge -> verifier

* **Context** (:mod:`app.agent.context`, RC1-393, RC1-394) is the
  repository's own conventions file, a grep for callers of what the diff
  changed, and the tests touching the changed paths, put in the shared
  prefix by Python with no model turn. It is the review's whole
  exploration: a property of the repository, done once and cheaply. The
  scout that used to explore on top of it was measured in RC1-427 (no
  defect found that the review otherwise missed, noise added wherever it
  ran) and retired, so the review is one prefix write plus four cached
  reads.
* **Router** (:mod:`app.agent.router`) decides from the file list which
  reviewers run and on which dimensions, and whether there is a repository
  to gather context from. The model never routes.
* **Reviewers** are three single calls, one per kind of evidence, fanned out
  with ``asyncio.gather`` so wall clock is the slowest of them rather than
  the sum. They have no tools. They share one prefix — system prompt, PR,
  context — under one cache breakpoint, so each reads it at cache-read price
  and only its own short suffix (its rubric slice and instructions) is new.
* **Merge** is Python: a reviewer's finding outside its categories is
  discarded (another reviewer had that evidence), findings at the same file,
  line and category are folded into one, and the summary is assembled from
  the reviewers' one-sentence summaries, most serious first.
* **Verifier** (RC1-387) then re-reads the merged findings against the same
  shared prefix and may drop or downgrade them. It is a stage, not a switch
  (RC1-428): it runs on every review that has findings and makes no call on
  one that has none.

Two details of the cache are load-bearing and are measured, not assumed:

1. The API only serves a cache entry once the request that wrote it has
   begun responding, so three reviewers launched together would each write
   the prefix and none would read it. A one-token **warm-cache** call writes
   it first; the reviewers then read it. The write is paid once either way;
   the warm call adds one output token and a round-trip.
2. A different ``tool_choice`` invalidates the cached *messages*, so the
   warm call, the reviewers and the verifier all send the same tool list and
   ``tool_choice: any``, and each is told which tool to call. A reviewer that
   calls the wrong tool is counted as unusable (zero findings), a verifier
   that does is read as "keep everything" — both safe, both visible.

:func:`review_pull_request` is the one entry point: the webhook, the dry-run
CLI, the eval corpus and the measurement scripts all call it, and it opens
the one ``pr_review`` workflow span the review's cost and latency land on
(RC1-395). The model-facing primitives it shares with the verifier — PR
rendering, cache markers, response parsing — live in
:mod:`app.agent.reviewer`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent.context import RepoContext, build_repo_context
from app.agent.local_repository import LocalRepository
from app.agent.prompts import SUBMIT_TOOL, SYSTEM_PROMPT, ReviewerSpec, reviewer_instructions
from app.agent.reviewer import (
    CACHE_CONTROL,
    DEFAULT_MAX_TOKENS,
    REQUEST_TIMEOUT_S,
    _get,
    _tokens,
    parse_findings,
    render_pr,
)
from app.agent.router import ReviewPlan, plan_review
from app.agent.verifier import VERIFY_TOOL, verify_findings
from app.config import settings
from app.models import Finding, PullRequest, ReviewResult, TokenUsage
from app.observability import (
    annotate_review_cost,
    annotate_review_identity,
    annotate_span,
    stage_span,
)

logger = logging.getLogger("app.agent.pipeline")

# Identical on every call that shares the prefix — see the module docstring.
REVIEW_TOOLS = [SUBMIT_TOOL, VERIFY_TOOL]
TOOL_CHOICE_ANY = {"type": "any"}
SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": CACHE_CONTROL}]
WARM_MAX_TOKENS = 1


@dataclass
class ReviewerOutput:
    """One reviewer's answer, before the merge."""

    spec: ReviewerSpec
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    usage: TokenUsage = TokenUsage()
    malformed: int = 0
    coerced: int = 0
    # False when the call came back without a readable submit_review.
    usable: bool = True


# --- the shared prefix ------------------------------------------------------

def build_shared_prefix(
    pull_request: PullRequest,
    precomputed_findings: list[Finding] | None,
    context: str = "",
) -> str:
    """The PR as the reviewers and the verifier all see it, then the
    repository context Python gathered (RC1-393; empty when there was none).

    One string, one cache breakpoint. Everything that differs per call comes
    after it.
    """
    parts = [*render_pr(pull_request, precomputed_findings)]
    if context:
        parts += ["", context]
    return "\n".join(parts)


def _request(model: str, prefix: str, suffix: str, max_tokens: int) -> dict[str, Any]:
    content: list[dict] = [{"type": "text", "text": prefix, "cache_control": CACHE_CONTROL}]
    if suffix:
        content.append({"type": "text", "text": suffix})
    return {
        "model": model,
        "system": SYSTEM_BLOCKS,
        "messages": [{"role": "user", "content": content}],
        "tools": REVIEW_TOOLS,
        "tool_choice": TOOL_CHOICE_ANY,
        "max_tokens": max_tokens,
    }


# --- the fan-out --------------------------------------------------------------

def _submission(response: Any) -> dict | None:
    for block in _get(response, "content") or []:
        if _get(block, "type") == "tool_use" and _get(block, "name") == SUBMIT_TOOL["name"]:
            payload = _get(block, "input") or {}
            return payload if isinstance(payload, dict) else None
    return None


async def _warm_cache(async_client: Any, model: str, prefix: str) -> TokenUsage:
    with stage_span("task", "warm_cache"):
        response = await async_client.messages.create(
            **_request(model, prefix, "", WARM_MAX_TOKENS)
        )
        usage = _tokens(response)
        logger.info(
            "warm_cache cache_write=%d cache_read=%d",
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
        )
        return usage


async def _run_reviewer(
    async_client: Any, spec: ReviewerSpec, model: str, prefix: str, max_tokens: int
) -> ReviewerOutput:
    with stage_span("agent", f"reviewer.{spec.name}"):
        response = await async_client.messages.create(
            **_request(model, prefix, reviewer_instructions(spec), max_tokens)
        )
        usage = _tokens(response)
        payload = _submission(response)
        if payload is None:
            logger.warning("reviewer_unusable name=%s: no submit_review in the response", spec.name)
            return ReviewerOutput(spec, usage=usage, usable=False)
        findings, malformed, coerced = parse_findings(payload)
        logger.info(
            "reviewer_done name=%s findings=%d cache_read=%d cache_write=%d uncached=%d out=%d",
            spec.name,
            len(findings),
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
            usage.input_tokens,
            usage.output_tokens,
        )
        annotate_span(
            metrics={
                "findings": len(findings),
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            },
            metadata={"categories": list(spec.categories)},
        )
        return ReviewerOutput(
            spec,
            summary=str(payload.get("summary") or ""),
            findings=findings,
            usage=usage,
            malformed=malformed,
            coerced=coerced,
        )


async def fan_out(
    async_client: Any,
    reviewers: tuple[ReviewerSpec, ...],
    model: str,
    prefix: str,
    max_tokens: int,
) -> tuple[TokenUsage, list[ReviewerOutput]]:
    """Warm the shared prefix, then run every reviewer concurrently."""
    warm = await _warm_cache(async_client, model, prefix)
    outputs = await asyncio.gather(
        *(_run_reviewer(async_client, spec, model, prefix, max_tokens) for spec in reviewers)
    )
    return warm, list(outputs)


async def _fan_out_then_close(
    async_client: Any,
    reviewers: tuple[ReviewerSpec, ...],
    model: str,
    prefix: str,
    max_tokens: int,
    *,
    close: bool,
) -> tuple[TokenUsage, list[ReviewerOutput]]:
    """``fan_out``, then close the client this review built, on the loop its
    connections were opened on. Seen in the RC1-394 corpus run: a client
    left to the garbage collector schedules its close on the loop
    ``asyncio.run`` has already torn down — "Event loop is closed", once per
    review, and a connection held until then. An injected client is the
    caller's to close."""
    try:
        return await fan_out(async_client, reviewers, model, prefix, max_tokens)
    finally:
        if close:
            await async_client.close()


# --- the merge ----------------------------------------------------------------

def merge_findings(outputs: list[ReviewerOutput]) -> tuple[list[Finding], int, int]:
    """Combine the reviewers' findings under rules the model cannot override.

    Returns ``(findings, off_scope, deduplicated)``. A finding outside its
    reviewer's categories is discarded; two findings at the same file, line
    and category become one, keeping the more severe. PR-level findings (no
    file or line) are never folded. Order follows the plan, then submission.
    """
    merged: list[Finding] = []
    index: dict[tuple[str, int, str], int] = {}
    off_scope = 0
    deduplicated = 0
    for out in outputs:
        for f in out.findings:
            if f.category not in out.spec.allowed:
                off_scope += 1
                logger.info(
                    "merge_off_scope reviewer=%s category=%s file=%s message=%s",
                    out.spec.name,
                    f.category,
                    f.file,
                    f.message[:120],
                )
                continue
            key = (f.file, f.line, f.category) if f.file and f.line else None
            if key is not None and key in index:
                deduplicated += 1
                at = index[key]
                if f.severity_rank < merged[at].severity_rank:
                    merged[at] = f
                continue
            if key is not None:
                index[key] = len(merged)
            merged.append(f)
    return merged, off_scope, deduplicated


def compose_summary(outputs: list[ReviewerOutput]) -> str:
    """The reviewers' one-sentence summaries, the one holding the most
    serious finding first, plan order on ties."""
    ordered = sorted(
        outputs, key=lambda o: min((f.severity_rank for f in o.findings), default=99)
    )
    sentences = [o.summary.strip() for o in ordered if o.summary.strip()]
    if sentences:
        return " ".join(sentences)
    names = ", ".join(o.spec.name for o in outputs)
    return f"No issues found by the {names} reviewers."


# --- the review -----------------------------------------------------------------

def review_pull_request(
    pull_request: PullRequest,
    repo_tools: LocalRepository,
    *,
    client: Any | None = None,
    async_client: Any | None = None,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    precomputed_findings: list[Finding] | None = None,
    plan: ReviewPlan | None = None,
    repo_context: bool = True,
) -> ReviewResult:
    """Run the review and return one :class:`ReviewResult`.

    ``precomputed_findings`` are findings from deterministic static checks
    that ran first; they are shown to the model as already-recorded (so it
    doesn't duplicate them) but are NOT merged here — the caller owns
    merging them into the final result, keeping this function's output the
    model's own. ``repo_context`` (RC1-393) is whether Python puts the conventions file,
    callers and tests in the shared prefix; the eval turns it off to measure
    it, nothing else does.

    ``client`` serves the verifier (sync) and is built only when there is
    something to verify; ``async_client`` serves the warm call and the reviewers. Either
    may be a fake exposing ``messages.create``. Runs the fan-out on its own
    event loop, so call it from synchronous code — the CLI, the eval
    subject, or the webhook's background task, which Starlette runs in a
    worker thread.

    The whole review runs inside one ``pr_review`` workflow span and is
    priced while that span is open (RC1-395), so the trace carries the
    review's cost and latency as metrics on its root.
    """
    started_review = time.perf_counter()
    with stage_span("workflow", "pr_review"):
        annotate_review_identity(pull_request)
        result = _review(
            pull_request,
            repo_tools,
            client=client,
            async_client=async_client,
            model=model,
            max_tokens=max_tokens,
            precomputed_findings=precomputed_findings,
            plan=plan,
            repo_context=repo_context,
        )
        result.latency_ms = (time.perf_counter() - started_review) * 1000
        annotate_review_cost(result)
    return result


def _review(
    pull_request: PullRequest,
    repo_tools: LocalRepository,
    *,
    client: Any | None,
    async_client: Any | None,
    model: str | None,
    max_tokens: int,
    precomputed_findings: list[Finding] | None,
    plan: ReviewPlan | None,
    repo_context: bool,
) -> ReviewResult:
    model = model or settings.review_model
    owns_async_client = async_client is None
    if async_client is None:
        from anthropic import AsyncAnthropic

        async_client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_S)

    plan = plan or plan_review(pull_request, explorable=getattr(repo_tools, "explorable", True))
    logger.info(
        "plan context=%s reviewers=%s reasons=%s",
        plan.context,
        ",".join(plan.names),
        plan.reasons,
    )

    latency: dict[str, float] = {}
    # RC1-393: the deterministic context is the review's exploration; a
    # docs-only change or an empty checkout pays for none of it.
    started = time.perf_counter()
    context = RepoContext()
    if plan.context and repo_context:
        with stage_span("task", "repo_context"):
            context = build_repo_context(pull_request, repo_tools)
    context_text = context.render()
    latency["context"] = _ms_since(started)
    logger.info(
        "context conventions=%s callers=%d unsearched=%d tests=%d untested=%d complete=%s",
        context.conventions_path,
        len(context.callers),
        len(context.symbols_unsearched),
        len(context.tests),
        len(context.untested),
        context.complete,
    )

    prefix = build_shared_prefix(pull_request, precomputed_findings, context_text)
    started = time.perf_counter()
    warm, outputs = asyncio.run(
        _fan_out_then_close(
            async_client, plan.reviewers, model, prefix, max_tokens, close=owns_async_client
        )
    )
    latency["fan_out"] = _ms_since(started)
    findings, off_scope, deduplicated = merge_findings(outputs)

    stage_usage: dict[str, TokenUsage] = {"warm_cache": warm}
    for out in outputs:
        stage_usage[f"reviewer:{out.spec.name}"] = out.usage
    total = TokenUsage()
    for used in stage_usage.values():
        total = total + used

    result = ReviewResult(
        summary=compose_summary(outputs),
        findings=findings,
        model=model,
        malformed_findings=sum(o.malformed for o in outputs),
        coerced_findings=sum(o.coerced for o in outputs),
        input_tokens=total.input_tokens,
        output_tokens=total.output_tokens,
        cache_creation_input_tokens=total.cache_creation_input_tokens,
        cache_read_input_tokens=total.cache_read_input_tokens,
        mode="multi",
        reviewers_run=plan.names,
        stage_usage=stage_usage,
        stage_latency_ms=latency,
        off_scope_findings=off_scope,
        deduplicated_findings=deduplicated,
        unusable_reviewer_calls=sum(1 for o in outputs if not o.usable),
        conventions_file=context.conventions_path,
        callers_found=len(context.callers),
        tests_found=len(context.tests),
        context_complete=context.complete,
    )
    logger.info(
        "review_done reviewers=%s findings=%d off_scope=%d deduplicated=%d unusable=%d "
        "context=%d out=%d",
        ",".join(plan.names),
        len(findings),
        off_scope,
        deduplicated,
        result.unusable_reviewer_calls,
        total.context_tokens,
        total.output_tokens,
    )
    annotate_span(
        metadata={
            "reviewers": plan.names,
            "context": plan.context,
            "reasons": list(plan.reasons),
            "conventions_file": context.conventions_path,
            "context_complete": context.complete,
        },
        metrics={
            "findings": len(findings),
            "off_scope": off_scope,
            "callers_found": len(context.callers),
            "tests_found": len(context.tests),
        },
    )

    if result.findings:
        if client is None:
            from anthropic import Anthropic  # imported lazily so tests don't need the SDK

            client = Anthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_S)
        started = time.perf_counter()
        with stage_span("agent", "verifier"):
            result = verify_findings(
                result,
                client=client,
                prefix=prefix,
                tools=REVIEW_TOOLS,
                tool_choice=TOOL_CHOICE_ANY,
            )
        latency["verifier"] = _ms_since(started)
    logger.info(
        "review_latency %s",
        " ".join(f"{stage}={ms / 1000:.1f}s" for stage, ms in latency.items()),
    )
    return result


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000
