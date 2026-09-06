"""Scout, routed reviewers, merge, verifier: the multi-agent review (RC1-390).

The single loop in :mod:`app.agent.reviewer` explores and judges in one
conversation. This path makes the review's graph explicit in plain Python:

    plan (router) -> scout -> [warm cache] -> reviewers (gather) -> merge -> verifier

* **Router** (:mod:`app.agent.router`) decides from the file list which
  reviewers run and on which dimensions. The model never routes.
* **Scout** (:mod:`app.agent.scout`) explores once, with tools, and writes a
  brief. Exploration is paid for once per review, not once per reviewer.
* **Reviewers** are three single calls, one per kind of evidence, fanned out
  with ``asyncio.gather`` so wall clock is the slowest of them rather than
  the sum. They have no tools. They share one prefix — system prompt, PR,
  brief — under one cache breakpoint, so each reads it at cache-read price
  and only its own short suffix (its rubric slice and instructions) is new.
* **Merge** is Python: a reviewer's finding outside its categories is
  discarded (another reviewer had that evidence), findings at the same file,
  line and category are folded into one, and the summary is assembled from
  the reviewers' one-sentence summaries, most serious first.
* **Verifier** (RC1-387) then runs as before, reading the same shared prefix.

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

``review_pull_request`` in :mod:`app.agent.reviewer` dispatches here when
``settings.review_multi_agent`` is on; with it off this module is never
imported, which is how "flag off is byte-identical" holds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent import scout as scouting
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
from app.agent.tools import RepoTools
from app.agent.verifier import VERIFY_TOOL, verify_findings
from app.config import settings
from app.models import Finding, PullRequest, ReviewResult, TokenUsage
from app.observability import annotate_span, stage_span

logger = logging.getLogger("app.agent.multi")

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
    pull_request: PullRequest, precomputed_findings: list[Finding] | None, brief: str
) -> str:
    """The PR as the reviewers and the verifier all see it, plus the brief.

    One string, one cache breakpoint. Everything that differs per call comes
    after it.
    """
    return "\n".join([*render_pr(pull_request, precomputed_findings), "", "Scout's brief:", brief])


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

def review_pull_request_multi(
    pull_request: PullRequest,
    repo_tools: RepoTools,
    *,
    client: Any | None = None,
    async_client: Any | None = None,
    model: str | None = None,
    max_files_read: int | None = None,
    scout_max_turns: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    precomputed_findings: list[Finding] | None = None,
    verify: bool = False,
    plan: ReviewPlan | None = None,
) -> ReviewResult:
    """Run the multi-agent review and return one :class:`ReviewResult`.

    ``client`` serves the scout and the verifier (sync); ``async_client``
    serves the warm call and the reviewers. Either may be a fake exposing
    ``messages.create``. Runs the fan-out on its own event loop, so call it
    from synchronous code — the CLI, the eval subject, or the webhook's
    background task, which Starlette runs in a worker thread.
    """
    model = model or settings.review_model
    max_files_read = max_files_read if max_files_read is not None else settings.max_files_read
    scout_max_turns = (
        scout_max_turns if scout_max_turns is not None else settings.review_scout_max_turns
    )
    if client is None:
        from anthropic import Anthropic  # imported lazily so tests don't need the SDK

        client = Anthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_S)
    if async_client is None:
        from anthropic import AsyncAnthropic

        async_client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_S)

    plan = plan or plan_review(pull_request, explorable=getattr(repo_tools, "explorable", True))
    logger.info(
        "plan scout=%s reviewers=%s reasons=%s", plan.scout, ",".join(plan.names), plan.reasons
    )

    latency: dict[str, float] = {}
    with stage_span("workflow", "pr_review"):
        started = time.perf_counter()
        if plan.scout:
            with stage_span("agent", "scout"):
                brief = scouting.explore(
                    pull_request,
                    repo_tools,
                    client=client,
                    model=model,
                    max_tool_turns=scout_max_turns,
                    max_files_read=max_files_read,
                    max_tokens=max_tokens,
                    precomputed_findings=precomputed_findings,
                )
        else:
            reason = plan.reasons[0] if plan.reasons else "nothing to explore"
            brief = scouting.skipped_brief(reason)

        latency["scout"] = _ms_since(started)

        prefix = build_shared_prefix(pull_request, precomputed_findings, brief.text)
        started = time.perf_counter()
        warm, outputs = asyncio.run(
            fan_out(async_client, plan.reviewers, model, prefix, max_tokens)
        )
        latency["fan_out"] = _ms_since(started)
        findings, off_scope, deduplicated = merge_findings(outputs)

        stage_usage = {"scout": brief.usage, "warm_cache": warm}
        for out in outputs:
            stage_usage[f"reviewer:{out.spec.name}"] = out.usage
        total = TokenUsage()
        for used in stage_usage.values():
            total = total + used

        result = ReviewResult(
            summary=compose_summary(outputs),
            findings=findings,
            model=model,
            tool_turns=brief.tool_turns,
            files_read=brief.files_read,
            truncated=brief.truncated,
            malformed_findings=sum(o.malformed for o in outputs),
            coerced_findings=sum(o.coerced for o in outputs),
            input_tokens=total.input_tokens,
            output_tokens=total.output_tokens,
            cache_creation_input_tokens=total.cache_creation_input_tokens,
            cache_read_input_tokens=total.cache_read_input_tokens,
            mode="multi",
            reviewers_run=plan.names,
            brief=brief.text,
            stage_usage=stage_usage,
            stage_latency_ms=latency,
            off_scope_findings=off_scope,
            deduplicated_findings=deduplicated,
            unusable_reviewer_calls=sum(1 for o in outputs if not o.usable),
        )
        logger.info(
            "multi_done reviewers=%s findings=%d off_scope=%d deduplicated=%d unusable=%d "
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
            metadata={"reviewers": plan.names, "scout": plan.scout, "reasons": list(plan.reasons)},
            metrics={"findings": len(findings), "off_scope": off_scope},
        )

        if verify and result.findings:
            started = time.perf_counter()
            with stage_span("agent", "verifier"):
                result = verify_findings(
                    pull_request,
                    result,
                    client=client,
                    shared_prefix=prefix,
                    tools=REVIEW_TOOLS,
                    tool_choice=TOOL_CHOICE_ANY,
                )
            latency["verifier"] = _ms_since(started)
        logger.info(
            "multi_latency %s",
            " ".join(f"{stage}={ms / 1000:.1f}s" for stage, ms in latency.items()),
        )
        return result


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000
