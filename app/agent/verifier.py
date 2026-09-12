"""Verifier pass over the merged findings (RC1-387; a permanent stage since RC1-428).

A second, tool-less model call that re-reads every finding the reviewers
submitted against the same rendering of the diff, and returns a verdict per
finding: **keep**, **drop**, or **downgrade**. Python applies the verdicts
under three rules the model cannot override:

* a finding with no verdict is kept — nothing is dropped by omission;
* a downgrade only ever lowers severity — the verifier never raises one;
* nothing is added — the verifier's output schema has no room for a finding.

Deterministic findings (the n8n check) never pass through here: the
pipeline appends them after this pass (RC1-425), and they are not the
model's to second-guess.

Why a separate pass rather than a better prompt: the corpus (RC1-253) shows
recall at ceiling, and the number it cannot see is precision on live PRs. The
pattern that moves precision without touching recall is a reader with one job
— "does the diff support this claim" — and no incentive to look thorough.
It reads the reviewers' shared prefix from cache and adds only the numbered
findings and its instructions (RC1-390); the cost is measured, not assumed,
in the decision records (docs/rc1-387-verifier.md, docs/rc1-428-verifier-policy.md).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.prompts import SYSTEM_PROMPT
from app.config import settings
from app.models import Finding, TokenUsage

logger = logging.getLogger("app.agent.verifier")

DEFAULT_MAX_TOKENS = 2048


@dataclass(frozen=True)
class Verification:
    """What the pass did with the merged findings (RC1-429): the ones it
    kept (downgrades applied), the ones it dropped, and what the call cost.
    ``ran`` is False when there was nothing to verify and no call was made;
    ``kept`` is then the input unchanged and ``model`` is empty."""

    kept: tuple[Finding, ...]
    dropped: tuple[Finding, ...] = ()
    downgraded: int = 0
    usage: TokenUsage = TokenUsage()
    model: str = ""
    ran: bool = False

_SEVERITY_RANK = {"nit": 0, "warning": 1, "blocker": 2}
_ONE_STEP_DOWN = {"blocker": "warning", "warning": "nit"}

CACHE_CONTROL = {"type": "ephemeral"}
SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": CACHE_CONTROL}]

# RC1-394: the reviewers have no tools and read a bounded, grep-built
# context. RC1-393's run C showed them escalating "not found" into blockers
# on files that were simply not in the checkout; the context block is the
# thing that can be read that way. Part of the instructions since RC1-428.
ABSENCE_RULE = (
    "The repository context above was gathered by a bounded grep. Drop a "
    "finding whose only evidence is that something was not found there — a "
    "file, module, caller or symbol the context does not mention: absence "
    "from a bounded search is not evidence of absence. A changed path the "
    "tests list names as untested is a fair 'tests' finding at warning; it "
    "is not a blocker."
)

VERIFIER_INSTRUCTIONS = (
    "You are now verifying a first-pass review of this pull request, not "
    "writing one. Above is the change exactly as the first pass saw it, "
    "followed by the findings it submitted, numbered.\n"
    "\n"
    "For each finding decide one of:\n"
    "- keep: the diff (or the PR description) supports the claim as stated.\n"
    "- drop: the diff does not support it. This includes a pattern the rubric "
    "names as a defect that is deliberate here and explained in a comment or "
    "the description; a claim about code that is not in the diff; a test "
    "fixture or placeholder mistaken for a real credential; or a finding that "
    "restates a convention the change already follows.\n"
    "- downgrade: the issue is real but the severity overstates it. Give the "
    "lower severity.\n"
    "\n"
    "Rules: judge only what is in front of you — do not investigate further. "
    "Do not add findings. Do not raise a severity. When in doubt, keep. A real "
    "committed secret is always kept at blocker. When two findings point at "
    "the same line and the same defect — even if they describe it from "
    "different angles — keep exactly one: the one whose category is the "
    "rubric dimension that names the defect, at the higher of their "
    "severities, and drop the other. Their order in the list above means "
    "nothing: do not keep the earlier one because it came first, and do not "
    "keep both because their wording differs. Never drop a finding as "
    "redundant unless a kept finding states the same defect. "
    + ABSENCE_RULE
    + " Give a one-sentence reason for every drop and downgrade. Call "
    "verify_findings exactly once."
)

VERIFY_TOOL = {
    "name": "verify_findings",
    "description": (
        "Return a verdict for the numbered findings. Findings you do not "
        "mention are kept unchanged."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "The finding's number in the list above.",
                        },
                        "decision": {
                            "type": "string",
                            "enum": ["keep", "drop", "downgrade"],
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["warning", "nit"],
                            "description": "For downgrade only: the new, lower severity.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "One sentence. Required for drop and downgrade.",
                        },
                    },
                    "required": ["index", "decision"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


def format_findings_for_verification(findings: list[Finding]) -> str:
    lines = ["First-pass findings:"]
    for i, f in enumerate(findings):
        where = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "(PR-level)")
        lines.append(f"\n[{i}] {f.severity} / {f.category} — {where}")
        lines.append(f"    {f.message}")
        if f.suggestion:
            lines.append(f"    suggestion: {f.suggestion}")
    return "\n".join(lines)


def _get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _tokens(response: Any) -> TokenUsage:
    usage = _get(response, "usage")
    return TokenUsage(
        input_tokens=_get(usage, "input_tokens", 0) or 0,
        output_tokens=_get(usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=_get(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=_get(usage, "cache_read_input_tokens", 0) or 0,
    )


def _verdicts(response: Any) -> list[dict]:
    for block in _get(response, "content") or []:
        if _get(block, "type") == "tool_use" and _get(block, "name") == VERIFY_TOOL["name"]:
            payload = _get(block, "input") or {}
            verdicts = payload.get("verdicts") if isinstance(payload, dict) else None
            return [v for v in verdicts or [] if isinstance(v, dict)]
    return []


def apply_verdicts(
    findings: list[Finding], verdicts: list[dict]
) -> tuple[list[Finding], list[Finding], int]:
    """Apply the model's verdicts under the module's rules.

    Returns ``(kept, dropped, downgraded_count)``. Order is preserved. The
    first verdict for an index wins; indexes that do not exist are ignored;
    a downgrade that does not lower severity is treated as keep.
    """
    by_index: dict[int, dict] = {}
    for v in verdicts:
        index = v.get("index")
        if isinstance(index, int) and 0 <= index < len(findings) and index not in by_index:
            by_index[index] = v

    kept: list[Finding] = []
    dropped: list[Finding] = []
    downgraded = 0
    for i, finding in enumerate(findings):
        verdict = by_index.get(i)
        decision = (verdict or {}).get("decision", "keep")
        reason = (verdict or {}).get("reason") or "(no reason given)"
        if decision == "drop":
            logger.info(
                "verifier_drop file=%s category=%s severity=%s reason=%s",
                finding.file,
                finding.category,
                finding.severity,
                reason,
            )
            dropped.append(finding)
            continue
        if decision == "downgrade":
            new = str(verdict.get("severity") or _ONE_STEP_DOWN.get(finding.severity, ""))
            if _SEVERITY_RANK.get(new, 99) < _SEVERITY_RANK.get(finding.severity, -1):
                logger.info(
                    "verifier_downgrade file=%s category=%s %s->%s reason=%s",
                    finding.file,
                    finding.category,
                    finding.severity,
                    new,
                    reason,
                )
                finding = Finding(
                    severity=new,
                    category=finding.category,
                    message=finding.message,
                    file=finding.file,
                    line=finding.line,
                    suggestion=finding.suggestion,
                )
                downgraded += 1
        kept.append(finding)
    return kept, dropped, downgraded


def verify_findings(
    findings: list[Finding],
    *,
    client: Any,
    prefix: str,
    tools: list[dict],
    tool_choice: dict,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Verification:
    """Run the verifier over ``findings`` and return a :class:`Verification`.

    An empty list is returned unchanged with no model call: a clean review
    pays nothing here. ``model`` defaults to the review model in settings;
    the pipeline passes the one the reviewers ran on.

    ``prefix`` is the PR and the repository context exactly as the reviewers
    saw them, sent under the same cache breakpoint with their ``tools`` and
    ``tool_choice``, so this call reads their cached prefix instead of
    writing its own (RC1-390). The RC1-387 shape that rendered the PR itself
    went with the flag in RC1-428. Folding the pass's tokens into the
    review's totals is the pipeline's job (RC1-429), not this function's.
    """
    if not findings:
        return Verification(kept=tuple(findings))
    model = model or settings.review_model
    suffix = "\n".join(
        [format_findings_for_verification(findings), "", VERIFIER_INSTRUCTIONS]
    )
    content = [
        {"type": "text", "text": prefix, "cache_control": CACHE_CONTROL},
        {"type": "text", "text": suffix},
    ]
    response = client.messages.create(
        model=model,
        system=SYSTEM_BLOCKS,
        messages=[{"role": "user", "content": content}],
        tools=tools,
        tool_choice=tool_choice,
        max_tokens=max_tokens,
    )
    used = _tokens(response)
    kept, dropped, downgraded = apply_verdicts(findings, _verdicts(response))
    logger.info(
        "verifier_done findings=%d kept=%d dropped=%d downgraded=%d context=%d out=%d",
        len(findings),
        len(kept),
        len(dropped),
        downgraded,
        used.context_tokens,
        used.output_tokens,
    )
    return Verification(
        kept=tuple(kept),
        dropped=tuple(dropped),
        downgraded=downgraded,
        usage=used,
        model=model,
        ran=True,
    )
