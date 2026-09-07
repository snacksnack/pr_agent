"""The multi-agent review as a LangGraph graph (RC1-391 spike).

The same review as :mod:`app.agent.multi` — same router, same context, same
scout, same prompts, same requests byte for byte — with the orchestration
that module writes in plain ``asyncio`` expressed as a LangGraph
``StateGraph`` instead:

    START -> plan -> repo_context -> scout -> warm_cache
          -> [reviewer x N, one Send per planned reviewer]
          -> merge -> (verifier | END) -> END

What is the framework's here and what is not is the point of the spike, so
the seam is deliberate:

* **Nodes call the Anthropic SDK directly**, through the request builders
  and parsers :mod:`app.agent.multi` already has. The LangChain model
  wrapper (``langchain-anthropic``) would put an abstraction between the
  review and its ``cache_control`` breakpoints, and installing it pins
  ``anthropic`` to a different major than the one the app runs on. The
  graph is the thing under test; the calls are held constant.
* **State is the review record as it accumulates** — the plan, the
  context, the brief, the prefix, the reviewers' outputs, the result. The
  run's dependencies (the PR, the checkout, the clients, the knobs) are
  LangGraph's *runtime context*, not state: they do not change during a
  run and a checkpoint should not have to serialize a client.
* **The fan-out is ``Send``.** ``warm_cache`` ends with one ``Send`` per
  planned reviewer; LangGraph runs them as one superstep and ``merge`` runs
  in the next, after every reviewer has written to ``outputs``. The
  ordering the asyncio version gets from ``await`` — warm first, then the
  reviewers, then the merge — is the edge list here.
* **Reducers** replace the list comprehension: ``outputs`` is appended to
  by every reviewer; ``latency_ms`` is a dict merge.

``review_pull_request_graph`` has the signature of
``review_pull_request_multi`` and returns the same :class:`ReviewResult`,
so the eval, the CLI and the webhook cannot tell which ran; the record in
``docs/rc1-391-langgraph-spike.md`` is where the difference is measured.
This module is imported only when ``REVIEW_ORCHESTRATOR=langgraph``, so the
dependency is dev-only and flag-off is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import operator
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from app.agent import scout as scouting
from app.agent.context import RepoContext, build_repo_context
from app.agent.multi import (
    REVIEW_TOOLS,
    TOOL_CHOICE_ANY,
    ReviewerOutput,
    _run_reviewer,
    _warm_cache,
    build_shared_prefix,
    compose_summary,
    merge_findings,
)
from app.agent.prompts import ReviewerSpec
from app.agent.reviewer import DEFAULT_MAX_TOKENS, REQUEST_TIMEOUT_S
from app.agent.router import ReviewPlan, plan_review, scout_turns
from app.agent.scout import Brief
from app.agent.tools import RepoTools
from app.agent.verifier import verify_findings
from app.config import settings
from app.models import Finding, PullRequest, ReviewResult, TokenUsage
from app.observability import annotate_span, stage_span

logger = logging.getLogger("app.agent.graph")


# --- runtime context: what a run is given and never changes -------------------

@dataclass
class ReviewRun:
    """Everything a review needs that is not the review: the PR, the
    checkout, the two clients and the knobs. Passed as LangGraph's runtime
    ``context`` so nodes read it from ``runtime.context`` rather than
    carrying it in state."""

    pull_request: PullRequest
    repo_tools: RepoTools
    client: Any
    async_client: Any
    model: str
    max_files_read: int
    scout_max_turns: int
    scout_context_turns: int
    scout_complete_turns: int
    max_tokens: int = DEFAULT_MAX_TOKENS
    precomputed_findings: list[Finding] | None = None
    verify: bool = False
    repo_context: bool = True
    plan: ReviewPlan | None = None


# --- state: the review record as it accumulates -------------------------------

def _merge_dicts(left: dict[str, float], right: dict[str, float]) -> dict[str, float]:
    return {**left, **right}


class ReviewState(TypedDict, total=False):
    plan: ReviewPlan
    context: RepoContext
    context_text: str
    scout_turn_cap: int
    brief: Brief
    prefix: str
    warm: TokenUsage
    fan_out_started: float
    #: Appended to by every reviewer task in the fan-out superstep.
    outputs: Annotated[list[ReviewerOutput], operator.add]
    latency_ms: Annotated[dict[str, float], _merge_dicts]
    result: ReviewResult


class ReviewerTask(TypedDict):
    """What one ``Send`` hands a reviewer node."""

    spec: ReviewerSpec
    prefix: str


# --- nodes ------------------------------------------------------------------

def plan(state: ReviewState, runtime: Runtime[ReviewRun]) -> ReviewState:
    run = runtime.context
    chosen = run.plan or plan_review(
        run.pull_request, explorable=getattr(run.repo_tools, "explorable", True)
    )
    logger.info(
        "plan scout=%s reviewers=%s reasons=%s",
        chosen.scout,
        ",".join(chosen.names),
        chosen.reasons,
    )
    return {"plan": chosen}


def repo_context(state: ReviewState, runtime: Runtime[ReviewRun]) -> ReviewState:
    run = runtime.context
    started = time.perf_counter()
    context = RepoContext()
    if state["plan"].scout and run.repo_context:
        with stage_span("task", "repo_context"):
            context = build_repo_context(run.pull_request, run.repo_tools)
    cap = scout_turns(
        context,
        full=run.scout_max_turns,
        with_context=run.scout_context_turns,
        when_complete=run.scout_complete_turns,
    )
    logger.info(
        "context conventions=%s callers=%d unsearched=%d tests=%d untested=%d "
        "complete=%s scout_turns=%d",
        context.conventions_path,
        len(context.callers),
        len(context.symbols_unsearched),
        len(context.tests),
        len(context.untested),
        context.complete,
        cap,
    )
    return {
        "context": context,
        "context_text": context.render(),
        "scout_turn_cap": cap,
        "latency_ms": {"context": _ms_since(started)},
    }


def scout(state: ReviewState, runtime: Runtime[ReviewRun]) -> ReviewState:
    run = runtime.context
    chosen = state["plan"]
    started = time.perf_counter()
    if chosen.scout and state["scout_turn_cap"] == 0:
        brief = scouting.skipped_brief(
            "the conventions file, the callers of what changed and the tests "
            "touching the changed paths are above, gathered without a model "
            "turn; nothing left to explore"
        )
    elif chosen.scout:
        with stage_span("agent", "scout"):
            brief = scouting.explore(
                run.pull_request,
                run.repo_tools,
                client=run.client,
                model=run.model,
                max_tool_turns=state["scout_turn_cap"],
                max_files_read=run.max_files_read,
                max_tokens=run.max_tokens,
                precomputed_findings=run.precomputed_findings,
                context=state["context_text"],
            )
    else:
        reason = chosen.reasons[0] if chosen.reasons else "nothing to explore"
        brief = scouting.skipped_brief(reason)
    return {"brief": brief, "latency_ms": {"scout": _ms_since(started)}}


async def warm_cache(state: ReviewState, runtime: Runtime[ReviewRun]) -> ReviewState:
    run = runtime.context
    started = time.perf_counter()
    prefix = build_shared_prefix(
        run.pull_request, run.precomputed_findings, state["brief"].text, state["context_text"]
    )
    warm = await _warm_cache(run.async_client, run.model, prefix)
    return {"prefix": prefix, "warm": warm, "fan_out_started": started}


def fan_out(state: ReviewState) -> list[Send]:
    """The conditional edge out of ``warm_cache``: one reviewer task per
    planned reviewer, all in the next superstep."""
    return [
        Send("reviewer", ReviewerTask(spec=spec, prefix=state["prefix"]))
        for spec in state["plan"].reviewers
    ]


async def reviewer(task: ReviewerTask, runtime: Runtime[ReviewRun]) -> ReviewState:
    run = runtime.context
    out = await _run_reviewer(
        run.async_client, task["spec"], run.model, task["prefix"], run.max_tokens
    )
    return {"outputs": [out]}


def merge(state: ReviewState, runtime: Runtime[ReviewRun]) -> ReviewState:
    run = runtime.context
    chosen = state["plan"]
    # Reducer order is the order LangGraph applied the writes; the record's
    # order is the plan's, as in the asyncio version.
    order = {spec.name: i for i, spec in enumerate(chosen.reviewers)}
    outputs = sorted(state["outputs"], key=lambda o: order.get(o.spec.name, len(order)))
    latency = {"fan_out": _ms_since(state["fan_out_started"])}
    findings, off_scope, deduplicated = merge_findings(outputs)
    brief = state["brief"]
    context = state["context"]

    stage_usage = {"scout": brief.usage, "warm_cache": state["warm"]}
    for out in outputs:
        stage_usage[f"reviewer:{out.spec.name}"] = out.usage
    total = TokenUsage()
    for used in stage_usage.values():
        total = total + used

    result = ReviewResult(
        summary=compose_summary(outputs),
        findings=findings,
        model=run.model,
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
        reviewers_run=chosen.names,
        brief=brief.text,
        stage_usage=stage_usage,
        stage_latency_ms={**state.get("latency_ms", {}), **latency},
        off_scope_findings=off_scope,
        deduplicated_findings=deduplicated,
        unusable_reviewer_calls=sum(1 for o in outputs if not o.usable),
        conventions_file=context.conventions_path,
        callers_found=len(context.callers),
        tests_found=len(context.tests),
        context_complete=context.complete,
        scout_ran=not brief.skipped,
    )
    logger.info(
        "multi_done reviewers=%s findings=%d off_scope=%d deduplicated=%d unusable=%d "
        "context=%d out=%d",
        ",".join(chosen.names),
        len(findings),
        off_scope,
        deduplicated,
        result.unusable_reviewer_calls,
        total.context_tokens,
        total.output_tokens,
    )
    annotate_span(
        metadata={
            "reviewers": chosen.names,
            "scout": chosen.scout,
            "reasons": list(chosen.reasons),
            "conventions_file": context.conventions_path,
            "context_complete": context.complete,
            "orchestrator": "langgraph",
        },
        metrics={
            "findings": len(findings),
            "off_scope": off_scope,
            "callers_found": len(context.callers),
            "tests_found": len(context.tests),
            "scout_turn_cap": state["scout_turn_cap"],
        },
    )
    return {"result": result, "latency_ms": latency}


def needs_verifier(state: ReviewState, runtime: Runtime[ReviewRun]) -> str:
    """The conditional edge out of ``merge``."""
    if runtime.context.verify and state["result"].findings:
        return "verifier"
    return END


def verifier(state: ReviewState, runtime: Runtime[ReviewRun]) -> ReviewState:
    run = runtime.context
    started = time.perf_counter()
    with stage_span("agent", "verifier"):
        result = verify_findings(
            run.pull_request,
            state["result"],
            client=run.client,
            shared_prefix=state["prefix"],
            tools=REVIEW_TOOLS,
            tool_choice=TOOL_CHOICE_ANY,
            absence_rule=True,
        )
    latency = {"verifier": _ms_since(started)}
    result.stage_latency_ms = {**result.stage_latency_ms, **latency}
    return {"result": result, "latency_ms": latency}


# --- the graph --------------------------------------------------------------------

NODES = ("plan", "repo_context", "scout", "warm_cache", "reviewer", "merge", "verifier")


def build_graph(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    interrupt_before: list[str] | None = None,
) -> CompiledStateGraph:
    """Compile the review graph. ``checkpointer`` and ``interrupt_before``
    are the framework's pause-and-resume machinery; the review itself never
    needs them, and the spike measures what having them costs."""
    graph = StateGraph(ReviewState, context_schema=ReviewRun)
    graph.add_node("plan", plan)
    graph.add_node("repo_context", repo_context)
    graph.add_node("scout", scout)
    graph.add_node("warm_cache", warm_cache)
    graph.add_node("reviewer", reviewer)
    graph.add_node("merge", merge)
    graph.add_node("verifier", verifier)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "repo_context")
    graph.add_edge("repo_context", "scout")
    graph.add_edge("scout", "warm_cache")
    graph.add_conditional_edges("warm_cache", fan_out, ["reviewer"])
    graph.add_edge("reviewer", "merge")
    graph.add_conditional_edges("merge", needs_verifier, {"verifier": "verifier", END: END})
    graph.add_edge("verifier", END)
    return graph.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)


@lru_cache(maxsize=1)
def default_graph() -> CompiledStateGraph:
    """The checkpoint-free graph production would run, compiled once per
    process: compiling costs about 4 ms and a review does not change the
    graph, so the eval's sixteen cases and the webhook's reviews share one."""
    return build_graph()


def mermaid() -> str:
    """The graph as Mermaid, for the record."""
    return build_graph().get_graph().draw_mermaid()


# --- the review -----------------------------------------------------------------

def review_pull_request_graph(
    pull_request: PullRequest,
    repo_tools: RepoTools,
    *,
    client: Any | None = None,
    async_client: Any | None = None,
    model: str | None = None,
    max_files_read: int | None = None,
    scout_max_turns: int | None = None,
    scout_context_turns: int | None = None,
    scout_complete_turns: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    precomputed_findings: list[Finding] | None = None,
    verify: bool = False,
    plan: ReviewPlan | None = None,
    repo_context: bool = True,
    graph: CompiledStateGraph | None = None,
    config: dict | None = None,
) -> ReviewResult:
    """Run the multi-agent review through the LangGraph graph.

    Same signature and result as
    :func:`app.agent.multi.review_pull_request_multi`. ``graph`` and
    ``config`` let a caller pass a compiled graph with a checkpointer and
    the thread it should run on; the default is a fresh, checkpoint-free
    graph, which is what production would run.
    """
    model = model or settings.review_model
    if client is None:
        from anthropic import Anthropic  # imported lazily so tests don't need the SDK

        client = Anthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_S)
    owns_async_client = async_client is None
    if async_client is None:
        from anthropic import AsyncAnthropic

        async_client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_S)

    run = ReviewRun(
        pull_request=pull_request,
        repo_tools=repo_tools,
        client=client,
        async_client=async_client,
        model=model,
        max_files_read=(
            max_files_read if max_files_read is not None else settings.max_files_read
        ),
        scout_max_turns=(
            scout_max_turns if scout_max_turns is not None else settings.review_scout_max_turns
        ),
        scout_context_turns=(
            scout_context_turns
            if scout_context_turns is not None
            else settings.review_scout_context_turns
        ),
        scout_complete_turns=(
            scout_complete_turns
            if scout_complete_turns is not None
            else settings.review_scout_complete_turns
        ),
        max_tokens=max_tokens,
        precomputed_findings=precomputed_findings,
        verify=verify,
        repo_context=repo_context,
        plan=plan,
    )
    compiled = graph or default_graph()

    async def _invoke() -> dict:
        try:
            return await compiled.ainvoke({}, config=config, context=run)
        finally:
            if owns_async_client:
                await async_client.close()

    started = time.perf_counter()
    state = asyncio.run(_invoke())
    graph_ms = _ms_since(started)
    result: ReviewResult = state["result"]
    # The framework's own share of the wall clock: everything the stages
    # did not account for. Read it from the record, not the trace.
    accounted = sum(result.stage_latency_ms.values())
    result.stage_latency_ms = {**result.stage_latency_ms, "graph_overhead": graph_ms - accounted}
    logger.info(
        "graph_latency %s",
        " ".join(f"{stage}={ms / 1000:.2f}s" for stage, ms in result.stage_latency_ms.items()),
    )
    return result


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000
