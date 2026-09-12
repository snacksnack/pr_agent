"""Model-facing primitives shared by every stage of the review (RC1-110, RC1-422).

The review itself is :mod:`app.agent.pipeline`. What lives here is the part
of talking to the model that every stage shares: the PR rendered the way the
model reads it (:func:`render_pr`, one rendering for the reviewers and the
verifier), the cache marker and system block every request carries, the
token counts read off a response, and the ``submit_review`` payload read
into findings (:func:`parse_findings`).

RC1-110 built this as the single agentic loop — explore and judge in one
conversation. RC1-390 split that into a scout and the routed reviewers,
RC1-422 retired the loop, and RC1-427 retired the scout and with it the
tool-loop mechanics that lived here, so only the primitives remain. The
model client is injectable everywhere: any object exposing
``messages.create(...)``.
"""
from __future__ import annotations

from typing import Any

from app.agent.prompts import SYSTEM_PROMPT, format_precomputed_findings
from app.agent.repository import is_lockfile
from app.models import SEVERITY_ORDER, Finding, PullRequest, TokenUsage

# Max characters of inline diff to put in the prefix. The context block
# (conventions, callers, tests) carries what the reviewers need beyond it.
MAX_DIFF_CHARS = 50_000
DEFAULT_MAX_TOKENS = 4096
# Per-request ceiling for the SDK clients the pipeline builds itself (RC1-387).
# The SDK default is ten minutes with two retries, so one stalled response held
# a corpus case for half an hour; a review turn that has not answered in this
# long is not going to. Retries still apply on top of it.
REQUEST_TIMEOUT_S = 180

# Prompt caching (RC1-350). Two 5-minute-TTL breakpoints per request: one on
# the constant system block (shared across reviews), one on the review's
# shared prefix, so the warm call writes it and every reviewer and the
# verifier read it at ~0.1x input price.
CACHE_CONTROL = {"type": "ephemeral"}

SYSTEM_BLOCKS = [
    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": CACHE_CONTROL}
]


def _get(block: Any, key: str, default: Any = None) -> Any:
    """Read a field from a content block that may be a dict or an SDK object."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def render_pr(
    pr: PullRequest, precomputed_findings: list[Finding] | None = None
) -> list[str]:
    """The PR as the model sees it — metadata, description, bounded diff —
    without any stage's instructions. One rendering for the reviewers'
    shared prefix and the verifier (RC1-387), so every pass reads the same
    change the same way.

    When ``precomputed_findings`` are supplied (from deterministic static
    checks that ran first), they are listed as already-recorded so the model
    builds on them rather than duplicating them.
    """
    parts = [
        f"Pull request: {pr.slug}",
        f"Title: {pr.title}",
        f"Author: {pr.author or 'unknown'}",
        f"Base: {pr.base_ref} ({pr.base_sha[:7]})  Head: {pr.head_ref} ({pr.head_sha[:7]})",
        "",
        "Description:",
        (pr.body.strip() or "(no description provided)"),
        "",
        f"Changed files ({pr.changed_files_count}):",
    ]
    if pr.truncated_files:
        parts.append("(file list truncated — very large PR)")

    budget = MAX_DIFF_CHARS
    for f in pr.files:
        header = f"\n--- {f.filename} ({f.status}, +{f.additions}/-{f.deletions}) ---"
        parts.append(header)
        if is_lockfile(f.filename.rsplit("/", 1)[-1]):
            # RC1-365: a lock file's patch is generated noise that would eat
            # the diff budget; the header above already carries the +/- counts.
            parts.append("(generated lock file; patch omitted — review the manifest change)")
            continue
        if not f.patch:
            parts.append("(no inline patch — binary or too large; use read_file)")
            continue
        patch = f.patch
        if len(patch) > budget:
            patch = patch[:budget] + "\n... [diff truncated; use read_file for the rest]"
            budget = 0
        else:
            budget -= len(patch)
        parts.append(patch)
        if budget <= 0:
            parts.append("\n... [remaining diffs omitted; use the tools to inspect them]")
            break

    precomputed = format_precomputed_findings(precomputed_findings)
    if precomputed:
        parts.append(precomputed)
    return parts


def _tokens(response: Any) -> TokenUsage:
    """One response's token counts, zero when a fake omits them.

    All four fields: since RC1-350 most of the context is served from the
    prompt cache and reported as ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``, not ``input_tokens``.
    """
    usage = _get(response, "usage")
    return TokenUsage(
        input_tokens=_get(usage, "input_tokens", 0) or 0,
        output_tokens=_get(usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=_get(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=_get(usage, "cache_read_input_tokens", 0) or 0,
    )


def parse_findings(payload: dict) -> tuple[list[Finding], int, int]:
    """Read a ``submit_review`` payload into findings.

    Returns ``(findings, malformed, coerced)``: findings that could not be
    read are counted rather than silently skipped, and a severity outside
    blocker/warning/nit is coerced to warning and counted (RC1-387).
    """
    findings: list[Finding] = []
    malformed = 0
    coerced = 0
    for item in payload.get("findings") or []:
        if not isinstance(item, dict):
            malformed += 1
            continue
        severity = item.get("severity")
        message = item.get("message")
        if not severity or not message:
            malformed += 1  # skip malformed findings rather than crash, but count them
            continue
        if str(severity) not in SEVERITY_ORDER:
            # The enum in the tool schema guides the model; it does not bind it.
            coerced += 1
            severity = "warning"
        line = item.get("line")
        findings.append(
            Finding(
                severity=str(severity),
                category=str(item.get("category") or "general"),
                message=str(message),
                file=item.get("file"),
                line=int(line) if isinstance(line, (int, str)) and str(line).isdigit() else None,
                suggestion=item.get("suggestion"),
            )
        )
    return findings, malformed, coerced

