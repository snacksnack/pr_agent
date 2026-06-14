"""Post a review to GitHub (RC1-117).

Turns a :class:`~app.models.ReviewResult` into a single GitHub review: a
top-level summary plus inline comments anchored to changed-hunk lines, with the
verdict (Comment vs. Request changes) from :mod:`app.verdict`.

Anchoring rule (the load-bearing bit): GitHub rejects the *entire* review with a
422 if any inline comment points at a line that isn't part of the diff. So we
parse each file's patch to learn exactly which lines are commentable and only
anchor findings there. Anything that can't anchor — PR-level findings, or a line
outside the diff — folds into the summary instead (acceptance criterion 3). As a
belt-and-braces guard, :func:`post_review` retries summary-only if GitHub still
422s, so a single bad anchor never costs the whole review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.github import GitHubClient, GitHubError
from app.models import Finding, PullRequest, ReviewResult
from app.verdict import EVENT_REQUEST_CHANGES, decide_event, gating_findings

_SEVERITY_LABEL = {"blocker": "🛑 blocker", "warning": "⚠️ warning", "nit": "💡 nit"}

# Header of a unified-diff hunk: @@ -old,n +new,n @@
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def commentable_lines(patch: str | None) -> set[int]:
    """RIGHT-side (new-file) line numbers a review comment can anchor to.

    Walks the unified diff and collects the new-file line numbers of added (``+``)
    and context (`` ``) lines — the lines GitHub treats as part of the hunk.
    Deleted (``-``) lines are LEFT-side only and excluded. Returns an empty set
    for a missing patch (binary / oversized diff), so nothing anchors there.
    """
    if not patch:
        return set()
    lines: set[int] = set()
    right: int | None = None
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            right = int(match.group(1)) if match else None
            continue
        if right is None:
            continue  # haven't seen a hunk header yet
        if raw.startswith("+"):
            lines.add(right)
            right += 1
        elif raw.startswith("-"):
            continue  # removed line: not on the new side
        elif raw.startswith(" ") or raw == "":
            lines.add(right)
            right += 1
        # anything else ("\ No newline at end of file") leaves `right` untouched
    return lines


@dataclass
class ReviewPayload:
    """The pieces of a single GitHub review, ready to POST."""

    body: str
    event: str
    comments: list[dict] = field(default_factory=list)


def _comment_body(finding: Finding) -> str:
    """Markdown for one inline comment: severity tag, message, optional fix."""
    label = _SEVERITY_LABEL.get(finding.severity, finding.severity)
    text = f"**{label}** · `{finding.category}`\n\n{finding.message.strip()}"
    if finding.suggestion:
        text += f"\n\n**Suggested fix:** {finding.suggestion.strip()}"
    return text


def _summary_line(finding: Finding) -> str:
    """One bullet for a finding that couldn't be anchored to a diff line."""
    label = _SEVERITY_LABEL.get(finding.severity, finding.severity)
    loc = ""
    if finding.file:
        loc = f"`{finding.file}`" + (f":{finding.line}" if finding.line else "")
        loc = f" ({loc})"
    line = f"- **{label}** · `{finding.category}`{loc} — {finding.message.strip()}"
    if finding.suggestion:
        line += f" _Fix:_ {finding.suggestion.strip()}"
    return line


def _verdict_note(findings: list[Finding], block_on: list[str], event: str) -> str:
    if event == EVENT_REQUEST_CHANGES:
        gating = gating_findings(findings, block_on)
        cats = ", ".join(sorted({f.category for f in gating}))
        return (
            f"**Verdict: Request changes** — {len(gating)} blocking finding(s) "
            f"in `{cats}` (block_on = {block_on})."
        )
    return "**Verdict: advisory** — no blocking findings; nothing here gates the merge."


def build_summary_body(
    summary: str,
    overflow: list[Finding],
    findings: list[Finding],
    block_on: list[str],
    event: str,
) -> str:
    """Compose the review's top-level body.

    Leads with the model's summary (already ordered most-serious-first), then
    lists any findings that couldn't anchor inline, then the verdict.
    """
    parts: list[str] = [summary.strip() or "_No summary provided._"]
    if overflow:
        parts.append(
            "### Findings not tied to a specific line\n"
            + "\n".join(_summary_line(f) for f in overflow)
        )
    parts.append(_verdict_note(findings, block_on, event))
    parts.append("<sub>🤖 Reviewed by the PR Review Agent.</sub>")
    return "\n\n".join(parts)


def build_review_payload(
    pr: PullRequest, result: ReviewResult, block_on: list[str]
) -> ReviewPayload:
    """Split findings into inline comments vs. summary, and pick the verdict."""
    anchors = {f.filename: commentable_lines(f.patch) for f in pr.files}
    comments: list[dict] = []
    overflow: list[Finding] = []
    for f in result.sorted_findings:  # most serious first
        if f.file and f.line and f.line in anchors.get(f.file, set()):
            comments.append(
                {
                    "path": f.file,
                    "line": f.line,
                    "side": "RIGHT",
                    "body": _comment_body(f),
                }
            )
        else:
            overflow.append(f)
    event = decide_event(result.findings, block_on)
    body = build_summary_body(result.summary, overflow, result.findings, block_on, event)
    return ReviewPayload(body=body, event=event, comments=comments)


def _fold_comments_into_body(payload: ReviewPayload, result: ReviewResult) -> str:
    """Fallback body when inline comments are rejected: list every finding."""
    listed = "\n".join(_summary_line(f) for f in result.sorted_findings)
    extra = (
        "### All findings\n" + listed
        if listed
        else "_No findings._"
    )
    return f"{payload.body}\n\n{extra}"


def post_review(
    client: GitHubClient,
    pr: PullRequest,
    result: ReviewResult,
    *,
    block_on: list[str],
    commit_id: str | None = None,
) -> dict:
    """Post the review for ``pr`` and return GitHub's review object.

    Pins the review to the head SHA so inline anchors resolve against the
    reviewed commit. If GitHub still rejects the inline comments (422 — e.g. an
    anchor edge case the patch parser missed), retry once summary-only with every
    finding folded into the body, so the review always lands.
    """
    payload = build_review_payload(pr, result, block_on)
    commit_id = commit_id or pr.head_sha or None
    try:
        return client.create_review(
            pr.ref,
            body=payload.body,
            event=payload.event,
            comments=payload.comments,
            commit_id=commit_id,
        )
    except GitHubError as exc:
        if not payload.comments or exc.status != 422:
            raise
        return client.create_review(
            pr.ref,
            body=_fold_comments_into_body(payload, result),
            event=payload.event,
            comments=None,
            commit_id=commit_id,
        )
