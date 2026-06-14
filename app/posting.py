"""Post a review to GitHub (RC1-117) with re-push dedup/supersede (RC1-118).

One review per push, kept tidy across pushes and webhook redeliveries:

- **Summary** — a single issue comment tagged with a hidden marker. On every
  push we find it by marker and *edit it in place* rather than stacking a new
  one (RC1-118 AC3: refresh/supersede rather than pile up). This is why the
  summary moved out of the review body since RC1-117.
- **Inline comments** — anchored to changed-hunk lines via the Reviews API, but
  only the ones we haven't already posted: identical comments on unchanged lines
  are skipped (AC2).
- **Verdict** — lives on a review object (Comment vs. Request changes, from
  :mod:`app.verdict`). Our prior *Request changes* reviews are dismissed before
  we (re-)assert the verdict, so a stale gate never lingers and they don't pile
  up. We only dismiss reviews carrying our own marker — never a human's.

Anchoring still only targets lines the diff actually contains (GitHub 422s the
whole review otherwise); :func:`commentable_lines` computes that set, and
:func:`post_review` retries the review without inline comments if GitHub still
rejects them, so the verdict and summary always land.
"""
from __future__ import annotations

import re
from typing import Any

from app.github import GitHubClient, GitHubError
from app.models import Finding, PullRequest, ReviewResult
from app.verdict import EVENT_REQUEST_CHANGES, decide_event, gating_findings

# Hidden markers let us recognize our own artifacts on later pushes without
# needing the bot's identity: HTML comments render invisibly on GitHub.
SUMMARY_MARKER = "<!-- pr-review-agent:summary -->"
REVIEW_MARKER = "<!-- pr-review-agent:review -->"

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


# --- finding -> markdown --------------------------------------------------

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


# --- payload pieces -------------------------------------------------------

def split_findings(pr: PullRequest, result: ReviewResult) -> tuple[list[dict], list[Finding]]:
    """Split findings into inline comments (anchorable) and summary overflow."""
    anchors = {f.filename: commentable_lines(f.patch) for f in pr.files}
    comments: list[dict] = []
    overflow: list[Finding] = []
    for f in result.sorted_findings:  # most serious first
        if f.file and f.line and f.line in anchors.get(f.file, set()):
            comments.append(
                {"path": f.file, "line": f.line, "side": "RIGHT", "body": _comment_body(f)}
            )
        else:
            overflow.append(f)
    return comments, overflow


def build_summary_body(
    summary: str,
    overflow: list[Finding],
    findings: list[Finding],
    block_on: list[str],
    event: str,
) -> str:
    """Compose the upserted summary comment (leads with the model's summary)."""
    parts: list[str] = [
        SUMMARY_MARKER,
        summary.strip() or "_No summary provided._",
    ]
    if overflow:
        parts.append(
            "### Findings not tied to a specific line\n"
            + "\n".join(_summary_line(f) for f in overflow)
        )
    parts.append(_verdict_note(findings, block_on, event))
    parts.append("<sub>🤖 Reviewed by the PR Review Agent.</sub>")
    return "\n\n".join(parts)


def _review_body(event: str) -> str:
    """Short body for the review object (the prose lives in the summary comment)."""
    note = (
        "A blocking finding is present — see the review summary comment for details."
        if event == EVENT_REQUEST_CHANGES
        else "See the review summary comment for the full write-up."
    )
    return f"{REVIEW_MARKER}\n{note}"


def find_marked(comments: list[dict], marker: str) -> dict | None:
    """Return the first comment whose body carries ``marker`` (ours), if any."""
    for c in comments:
        if marker in (c.get("body") or ""):
            return c
    return None


def filter_new_comments(comments: list[dict], existing: list[dict]) -> list[dict]:
    """Drop inline comments already present (same path + line + body)."""
    seen = {(c.get("path"), c.get("line"), c.get("body")) for c in existing}
    return [c for c in comments if (c["path"], c["line"], c["body"]) not in seen]


# --- orchestration --------------------------------------------------------

def _upsert_summary(client: GitHubClient, pr: PullRequest, body: str) -> str:
    existing = find_marked(client.list_issue_comments(pr.ref), SUMMARY_MARKER)
    if existing:
        client.update_issue_comment(pr.ref, existing["id"], body)
        return "updated"
    client.create_issue_comment(pr.ref, body)
    return "created"


def _dismiss_prior_change_requests(client: GitHubClient, pr: PullRequest) -> int:
    """Dismiss our own still-active 'Request changes' reviews (marker-scoped)."""
    dismissed = 0
    for rv in client.list_reviews(pr.ref):
        if rv.get("state") == "CHANGES_REQUESTED" and REVIEW_MARKER in (rv.get("body") or ""):
            client.dismiss_review(pr.ref, rv["id"], "Superseded by a newer review.")
            dismissed += 1
    return dismissed


def _create_review(
    client: GitHubClient, pr: PullRequest, event: str, comments: list[dict], commit_id: str | None
) -> dict:
    """Create the review, retrying without inline comments on a 422 anchor reject."""
    try:
        return client.create_review(
            pr.ref, body=_review_body(event), event=event,
            comments=comments or None, commit_id=commit_id,
        )
    except GitHubError as exc:
        if not comments or exc.status != 422:
            raise
        return client.create_review(
            pr.ref, body=_review_body(event), event=event, comments=None, commit_id=commit_id,
        )


def post_review(
    client: GitHubClient,
    pr: PullRequest,
    result: ReviewResult,
    *,
    block_on: list[str],
    commit_id: str | None = None,
) -> dict[str, Any]:
    """Post/refresh the review for ``pr``; return a summary of what changed.

    Upserts the single summary comment, posts only inline comments we haven't
    posted before, supersedes our prior Request-changes reviews, and creates a
    new review only when there's something new to say or a gate to (re-)assert.
    """
    comments, overflow = split_findings(pr, result)
    event = decide_event(result.findings, block_on)
    commit_id = commit_id or pr.head_sha or None

    summary_action = _upsert_summary(
        client, pr, build_summary_body(result.summary, overflow, result.findings, block_on, event)
    )
    new_comments = filter_new_comments(comments, client.list_review_comments(pr.ref))
    dismissed = _dismiss_prior_change_requests(client, pr)

    # A review is only worth posting when it carries new inline comments or a
    # blocking verdict to assert; otherwise the refreshed summary comment is it.
    review_id = None
    if new_comments or event == EVENT_REQUEST_CHANGES:
        review_id = _create_review(client, pr, event, new_comments, commit_id).get("id")

    return {
        "summary_action": summary_action,
        "new_comments": len(new_comments),
        "dismissed": dismissed,
        "review_id": review_id,
        "event": event,
    }
