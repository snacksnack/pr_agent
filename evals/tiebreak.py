"""The verifier's category tie-break, measured (RC1-398).

Three questions, three modes, one scoring vocabulary:

* ``history`` — what the eval store already says. For every verified case-run
  of the planted corpus: which categories were filed on the planted defect,
  which survived the verifier, and whether the intended one was among them.
  Free.
* ``probe`` — the verifier alone. A boundary case's pair of findings (same
  defect, same line, two categories) is handed to ``verify_findings`` as the
  first pass, in both orders, several times; the survivor is the verifier's
  choice with nothing else in the way. Billed, a few tenths of a cent a call.
* ``pipeline`` — the whole multi-agent review over the same cases, so the
  probe's answer can be read against what the reviewers actually file and
  what reaches the author. Billed, a few cents a review.

The scoring is in this module and offline-testable; ``scripts/measure_tiebreak.py``
is the command line around it.
"""

from __future__ import annotations

import re
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent import verifier
from app.agent.context import RepoContext
from app.agent.pipeline import (
    REVIEW_TOOLS,
    TOOL_CHOICE_ANY,
    build_shared_prefix,
    review_pull_request,
)
from app.agent.tools import RepoTools
from app.config import settings
from app.models import Finding, ReviewResult
from app.pricing import cost_usd, review_cost
from evals import boundary, corpus
from evals.subject import materialise_checkout

_MESSAGE = re.compile(r"^\[(\w+)/(\w+)\] (.*)$", re.DOTALL)
#: A finding as ``format_findings_for_verification`` numbers it.
_NUMBERED = re.compile(r"^\[(\d+)\] \w+ / (\w+) — ", re.MULTILINE)

# --- shared vocabulary --------------------------------------------------------

#: What became of the intended category on one case-run.
SURVIVED = "survived"  # intended category among the kept findings
VERIFIER_DROPPED = "verifier-dropped"  # filed on the plant, dropped, another category kept
NEVER_FILED = "never-filed"  # the plant was found, but never under the intended category
NOT_FOUND = "not-found"  # nothing on the plant at all


def about(text: str, evidence: tuple[str, ...]) -> bool:
    """The corpus's generous evidence match, over any text."""
    haystack = text.lower()
    return any(token.lower() in haystack for token in evidence)


def outcome(intended: str, kept_on: list[str], dropped_on: list[str]) -> str:
    if intended in kept_on:
        return SURVIVED
    if intended in dropped_on:
        return VERIFIER_DROPPED
    if kept_on or dropped_on:
        return NEVER_FILED
    return NOT_FOUND


# --- history: the eval store --------------------------------------------------


def categories_on_plant(messages: list[str], evidence: tuple[str, ...]) -> list[str]:
    """Categories of the ``[severity/category] message`` rows about the plant.

    The store keeps kept findings as ``observations.messages`` and the
    verifier's drops as ``observations.verifier.dropped_messages``, both in
    this shape and both cut at 120 characters — enough for the evidence
    tokens on every case-run checked by hand, but a miss here reads as
    ``not-found``, never as a wrong survivor.
    """
    out: list[str] = []
    for row in messages:
        m = _MESSAGE.match(row)
        if m and about(m.group(3), evidence):
            out.append(m.group(2))
    return out


@dataclass(frozen=True)
class HistoryRow:
    run_id: str
    mode: str  # single | multi
    case_id: str
    intended: str
    kept_on: tuple[str, ...]
    dropped_on: tuple[str, ...]

    @property
    def outcome(self) -> str:
        return outcome(self.intended, list(self.kept_on), list(self.dropped_on))

    @property
    def survivors(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.kept_on)))


def history_rows(records: list[Any]) -> list[HistoryRow]:
    """One row per verified planted-case result across the given run records
    (``agent_evals.record.RunRecord``; anything with ``run_id`` and ``results``)."""
    rows: list[HistoryRow] = []
    for record in records:
        for result in record.results:
            planted = corpus.BY_ID.get(result.case_id)
            if planted is None or not planted.category:
                continue
            obs = result.observations or {}
            verified = obs.get("verifier") or {}
            if not verified.get("ran"):
                continue
            multi = (obs.get("multi") or {}).get("ran")
            rows.append(
                HistoryRow(
                    run_id=record.run_id,
                    mode="multi" if multi else "single",
                    case_id=planted.id,
                    intended=planted.category,
                    kept_on=tuple(categories_on_plant(obs.get("messages") or [], planted.evidence)),
                    dropped_on=tuple(
                        categories_on_plant(
                            verified.get("dropped_messages") or [], planted.evidence
                        )
                    ),
                )
            )
    return rows


def summarize_history(rows: list[HistoryRow]) -> dict[str, Any]:
    by_outcome = Counter(r.outcome for r in rows)
    dropped = [r for r in rows if r.outcome == VERIFIER_DROPPED]
    lost_to: Counter[tuple[str, str]] = Counter()
    for r in dropped:
        for winner in r.survivors:
            lost_to[(r.intended, winner)] += 1
    return {
        "case_runs": len(rows),
        "runs": len({r.run_id for r in rows}),
        "multi_runs": len({r.run_id for r in rows if r.mode == "multi"}),
        "by_outcome": dict(by_outcome),
        "verifier_dropped_by_case": dict(Counter(r.case_id for r in dropped)),
        "intended_lost_to": {f"{a} -> {b}": n for (a, b), n in lost_to.most_common()},
    }


# --- probe: the verifier alone ----------------------------------------------

INTENDED_FIRST = "intended-first"
RIVAL_FIRST = "rival-first"
ORDERS = (INTENDED_FIRST, RIVAL_FIRST)

#: What the verifier kept of the pair.
KEPT_INTENDED = "intended"
KEPT_RIVAL = "rival"
KEPT_BOTH = "both"
KEPT_NEITHER = "neither"


def kept_of_pair(case: boundary.BoundaryCase, kept: list[Finding]) -> str:
    cats = {f.category for f in kept}
    has_intended = case.intended in cats
    has_rival = case.rival in cats
    if has_intended and has_rival:
        return KEPT_BOTH
    if has_intended:
        return KEPT_INTENDED
    if has_rival:
        return KEPT_RIVAL
    return KEPT_NEITHER


class RecordingClient:
    """A sync client wrapper that keeps every response, so the verifier's
    verdicts — reasons included, which ``verify_findings`` logs but does not
    return — can be read back after the call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[dict[str, Any], Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        response = self._inner.messages.create(**kwargs)
        self.calls.append((kwargs, response))
        return response

    def verifier_call(self) -> tuple[dict[str, Any], Any] | None:
        """The last call that answered with verify_findings verdicts."""
        for call in reversed(self.calls):
            if verifier._verdicts(call[1]):
                return call
        return None

    def verdicts(self) -> list[dict]:
        call = self.verifier_call()
        return verifier._verdicts(call[1]) if call else []

    def numbered_categories(self) -> dict[int, str]:
        """``index -> category`` as the verifier's request numbered them —
        read back from the request text, since the kept/dropped split the
        result carries no longer says which index was which."""
        call = self.verifier_call()
        if call is None:
            return {}
        text = "\n".join(
            block.get("text", "")
            for message in call[0].get("messages", [])
            for block in (message.get("content") or [])
            if isinstance(block, dict)
        )
        return {int(m.group(1)): m.group(2) for m in _NUMBERED.finditer(text)}


def probe_prefix(case: boundary.BoundaryCase) -> str:
    """The shared prefix the verifier reads: the PR and the repository
    context Python would have built (the conventions page when the case
    needs one)."""
    pr = boundary.pull_request(case)
    context = ""
    if case.needs_conventions:
        context = RepoContext(
            conventions=boundary.CONVENTIONS_PAGE.strip(), conventions_path="CLAUDE.md"
        ).render()
    return build_shared_prefix(pr, None, context)


def probe_case(
    case: boundary.BoundaryCase,
    order: str,
    *,
    client: Any,
    model: str | None = None,
) -> dict[str, Any]:
    """One verifier call over the case's pair in the given order, with the
    shipped instructions. (RC1-398 measured a candidate wording here by
    swapping the sentence for the run; RC1-428 shipped it.)"""
    intended, rival = case.pair
    findings = [intended, rival] if order == INTENDED_FIRST else [rival, intended]
    model = model or settings.review_model
    first_pass = ReviewResult(findings=findings, model=model, mode="multi")
    recorder = RecordingClient(client)
    started = time.perf_counter()
    verified = verifier.verify_findings(
        first_pass,
        client=recorder,
        model=model,
        prefix=probe_prefix(case),
        tools=REVIEW_TOOLS,
        tool_choice=TOOL_CHOICE_ANY,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    by_index = {v.get("index"): v for v in recorder.verdicts() if isinstance(v.get("index"), int)}
    return {
        "case": case.id,
        "pair": list(case.categories),
        "order": order,
        "first": findings[0].category,
        "kept": kept_of_pair(case, verified.findings),
        "kept_categories": [f.category for f in verified.findings],
        "first_survived": findings[0].category in {f.category for f in verified.findings},
        "verdicts": [
            {
                "category": findings[i].category,
                "decision": by_index.get(i, {}).get("decision", "keep"),
                "reason": by_index.get(i, {}).get("reason"),
            }
            for i in range(len(findings))
        ],
        "cost_usd": float(cost_usd(model, verified.verifier_usage)),
        "latency_ms": round(latency_ms),
        "model": model,
    }


def summarize_probe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per_case: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["case"]].append(r)
    pair_hits: Counter[str] = Counter()
    pair_runs: Counter[str] = Counter()
    for case_id, case_rows in grouped.items():
        pair = " vs ".join(case_rows[0]["pair"])
        kept = Counter(r["kept"] for r in case_rows)
        by_order = {
            order: Counter(r["kept"] for r in case_rows if r["order"] == order) for order in ORDERS
        }
        intended_alone = kept[KEPT_INTENDED]
        per_case[case_id] = {
            "pair": pair,
            "runs": len(case_rows),
            "kept": dict(kept),
            "by_order": {o: dict(c) for o, c in by_order.items()},
            "agreement": intended_alone / len(case_rows),
            "cost_usd": sum(r["cost_usd"] for r in case_rows),
        }
        pair_hits[pair] += intended_alone
        pair_runs[pair] += len(case_rows)
    decided = [r for r in rows if r["kept"] in (KEPT_INTENDED, KEPT_RIVAL)]
    first_won = sum(1 for r in decided if r["first_survived"])
    return {
        "rows": len(rows),
        "cases": len(grouped),
        "kept": dict(Counter(r["kept"] for r in rows)),
        "agreement": sum(1 for r in rows if r["kept"] == KEPT_INTENDED) / len(rows) if rows else 0,
        "per_pair": {
            p: {"agreement": pair_hits[p] / pair_runs[p], "runs": pair_runs[p]} for p in pair_runs
        },
        "per_case": per_case,
        # Position: of the runs where exactly one of the pair survived, how
        # often it was the one listed first. 0.5 is no position effect.
        "first_listed_won": {
            "share": first_won / len(decided) if decided else None,
            "decided": len(decided),
        },
        "cost_usd": sum(r["cost_usd"] for r in rows),
    }


# --- pipeline: the whole multi-agent review -----------------------------------


def pipeline_case(case: boundary.BoundaryCase, *, client: Any) -> dict[str, Any]:
    """One multi-agent review with the verifier on, over a checkout holding
    only what the case lays down (the conventions page, when it needs one)."""
    pr = boundary.pull_request(case)
    recorder = RecordingClient(client)
    with tempfile.TemporaryDirectory(prefix=f"tiebreak-{case.id}-") as tmp:
        materialise_checkout(Path(tmp), None, boundary.repo_files(case))
        started = time.perf_counter()
        result = review_pull_request(
            pr, RepoTools(tmp), client=recorder, verify=True, repo_context=True
        )
        wall_s = time.perf_counter() - started
    kept_on = [
        f for f in result.findings if about(f"{f.message} {f.suggestion or ''}", case.evidence)
    ]
    dropped_on = [
        f
        for f in result.verifier_dropped
        if about(f"{f.message} {f.suggestion or ''}", case.evidence)
    ]
    kept_cats = [f.category for f in kept_on]
    dropped_cats = [f.category for f in dropped_on]
    a, _ = case.pair
    filed_cats = {f.category for f in [*kept_on, *dropped_on]}
    numbered = recorder.numbered_categories()
    reasons = [
        {"category": numbered.get(v.get("index"), "?"), "reason": v.get("reason")}
        for v in recorder.verdicts()
        if v.get("decision") == "drop"
    ]
    return {
        "case": case.id,
        "pair": list(case.categories),
        "outcome": outcome(case.intended, kept_cats, dropped_cats),
        "kept_on_plant": kept_cats,
        "dropped_on_plant": dropped_cats,
        "pair_both_filed": case.intended in filed_cats and case.rival in filed_cats,
        "pair_both_at_line": sum(
            1 for f in [*kept_on, *dropped_on] if f.file == a.file and f.line == a.line
        )
        >= 2,
        "findings": len(result.findings),
        "reviewers": list(result.reviewers_run),
        "context_complete": result.context_complete,
        "drop_reasons": reasons,
        "cost_usd": float(review_cost(result).total),
        "wall_s": round(wall_s, 1),
    }


def summarize_pipeline(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["case"]].append(r)
    return {
        "rows": len(rows),
        "by_outcome": dict(Counter(r["outcome"] for r in rows)),
        "pair_both_filed": sum(1 for r in rows if r["pair_both_filed"]),
        "per_case": {
            case_id: {
                "pair": " vs ".join(case_rows[0]["pair"]),
                "outcomes": dict(Counter(r["outcome"] for r in case_rows)),
                "both_filed": sum(1 for r in case_rows if r["pair_both_filed"]),
                "cost_usd": sum(r["cost_usd"] for r in case_rows),
            }
            for case_id, case_rows in grouped.items()
        },
        "cost_usd": sum(r["cost_usd"] for r in rows),
    }


@dataclass
class Tally:
    """A running total the CLI prints as it goes."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(r.get("cost_usd", 0.0) for r in self.rows)
