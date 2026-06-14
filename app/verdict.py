"""Verdict policy (RC1-117).

Maps a set of review findings to a GitHub review *event*. This is the single
source of truth for "does this review block the merge?", shared by the dry-run
CLI (RC1-113) and the live poster (:mod:`app.posting`).

Policy — **advisory by default**: the verdict is ``COMMENT`` unless a finding's
*category* is named in ``block_on`` (default ``["leaked_secret"]``), in which
case it escalates to ``REQUEST_CHANGES``. We gate on category alone, never on
``blocker`` severity: a non-secret correctness defect is reported prominently
but must not force "Request changes" (see docs/rc1-114-tuning.md — the RC1-114
carryover this implements). ``block_on`` is configurable via ``REVIEW_BLOCK_ON``.
"""
from __future__ import annotations

from collections.abc import Iterable

from app.models import Finding

# GitHub pull-request review events. We only ever use these two — the agent is
# advisory and never auto-approves.
EVENT_COMMENT = "COMMENT"
EVENT_REQUEST_CHANGES = "REQUEST_CHANGES"


def gating_findings(findings: Iterable[Finding], block_on: Iterable[str]) -> list[Finding]:
    """Findings that gate the merge: those whose category is in ``block_on``."""
    block = set(block_on)
    return [f for f in findings if f.category in block]


def decide_event(findings: Iterable[Finding], block_on: Iterable[str]) -> str:
    """``REQUEST_CHANGES`` if any finding gates, else ``COMMENT``."""
    findings = list(findings)
    return EVENT_REQUEST_CHANGES if gating_findings(findings, block_on) else EVENT_COMMENT
