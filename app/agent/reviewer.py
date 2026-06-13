"""Agentic review loop (RC1-110).

Drives a multi-turn conversation with the model: seed it with the PR diff and
metadata, expose the repo-exploration tools (RC1-109) plus a ``submit_review``
tool, and let it investigate across turns until it submits structured findings.

Guardrails cap both the number of model turns and the number of files read so a
single review can't run away on cost or time. If the model exhausts its budget
without submitting, we force a final ``submit_review`` call so the caller always
gets a structured :class:`ReviewResult`.

The model client is injectable: pass any object exposing
``messages.create(...)`` (the real ``anthropic.Anthropic`` client, or a scripted
fake in tests). The review rubric, system prompt, procedural instructions, and
the ``submit_review`` output schema live in :mod:`app.agent.prompts` (RC1-111);
this module composes them into the loop.
"""
from __future__ import annotations

from typing import Any

from app.agent.prompts import INSTRUCTIONS, SUBMIT_TOOL, SYSTEM_PROMPT
from app.agent.tools import TOOL_SCHEMAS, RepoTools
from app.config import settings
from app.models import Finding, PullRequest, ReviewResult

# Max characters of inline diff to put in the seed prompt; the agent can read
# full files via tools if it needs more than this.
MAX_DIFF_CHARS = 50_000
DEFAULT_MAX_TOKENS = 4096

ALL_TOOLS = [*TOOL_SCHEMAS, SUBMIT_TOOL]


class ReviewError(RuntimeError):
    """Raised when the loop cannot produce a structured review."""


def _get(block: Any, key: str, default: Any = None) -> Any:
    """Read a field from a content block that may be a dict or an SDK object."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _normalize_blocks(content: Any) -> list[dict]:
    """Normalize model response content into plain dict blocks we can replay."""
    blocks: list[dict] = []
    for block in content or []:
        btype = _get(block, "type")
        if btype == "text":
            blocks.append({"type": "text", "text": _get(block, "text", "")})
        elif btype == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": _get(block, "id"),
                    "name": _get(block, "name"),
                    "input": _get(block, "input") or {},
                }
            )
    return blocks


def format_pr_for_review(pr: PullRequest) -> str:
    """Render the PR metadata + diff into the seed user message."""
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

    parts.extend(["", INSTRUCTIONS])
    return "\n".join(parts)


def _create(client: Any, *, model: str, messages: list, max_tokens: int, tool_choice: dict | None = None):
    kwargs: dict[str, Any] = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "messages": messages,
        "tools": ALL_TOOLS,
        "max_tokens": max_tokens,
    }
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    return client.messages.create(**kwargs)


def _result_from_submission(
    payload: dict, *, model: str, tool_turns: int, files_read: int, truncated: bool
) -> ReviewResult:
    findings: list[Finding] = []
    for item in payload.get("findings") or []:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        message = item.get("message")
        if not severity or not message:
            continue  # skip malformed findings rather than crash
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
    return ReviewResult(
        summary=str(payload.get("summary") or ""),
        findings=findings,
        model=model,
        tool_turns=tool_turns,
        files_read=files_read,
        truncated=truncated,
    )


def review_pull_request(
    pull_request: PullRequest,
    repo_tools: RepoTools,
    *,
    client: Any | None = None,
    model: str | None = None,
    max_tool_turns: int | None = None,
    max_files_read: int | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ReviewResult:
    """Run the agentic review loop over a PR and return structured findings."""
    model = model or settings.review_model
    max_tool_turns = max_tool_turns if max_tool_turns is not None else settings.max_tool_turns
    max_files_read = max_files_read if max_files_read is not None else settings.max_files_read

    if client is None:
        from anthropic import Anthropic  # imported lazily so tests don't need the SDK

        client = Anthropic(api_key=settings.anthropic_api_key)

    messages: list[dict] = [
        {"role": "user", "content": format_pr_for_review(pull_request)}
    ]
    files_read = 0
    turns = 0
    truncated = False

    for _ in range(max_tool_turns):
        turns += 1
        response = _create(client, model=model, messages=messages, max_tokens=max_tokens)
        blocks = _normalize_blocks(_get(response, "content"))
        messages.append({"role": "assistant", "content": blocks})

        tool_uses = [b for b in blocks if b["type"] == "tool_use"]
        if not tool_uses:
            break  # model stopped without submitting -> force a submission below

        tool_results = []
        submission: dict | None = None
        for tu in tool_uses:
            name, tool_input, tool_id = tu["name"], tu["input"], tu["id"]
            if name == "submit_review":
                submission = tool_input
                output = "Review recorded."
            elif name == "read_file" and files_read >= max_files_read:
                truncated = True
                output = "Error: file-read budget exhausted. Submit your review now."
            else:
                if name == "read_file":
                    files_read += 1
                output = repo_tools.dispatch(name, tool_input)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": tool_id, "content": output}
            )

        messages.append({"role": "user", "content": tool_results})

        if submission is not None:
            return _result_from_submission(
                submission, model=model, tool_turns=turns, files_read=files_read, truncated=truncated
            )
    else:
        # Ran the full turn budget without submitting.
        truncated = True

    # Force a final structured submission.
    submission = _force_submit(client, messages, model=model, max_tokens=max_tokens)
    return _result_from_submission(
        submission, model=model, tool_turns=turns, files_read=files_read, truncated=truncated
    )


def _force_submit(client: Any, messages: list, *, model: str, max_tokens: int) -> dict:
    """Make one final call that must call submit_review, and return its input."""
    nudge = messages + [
        {
            "role": "user",
            "content": "You have used your review budget. Call submit_review now "
            "with your final findings.",
        }
    ]
    response = _create(
        client,
        model=model,
        messages=nudge,
        max_tokens=max_tokens,
        tool_choice={"type": "tool", "name": "submit_review"},
    )
    for block in _normalize_blocks(_get(response, "content")):
        if block["type"] == "tool_use" and block["name"] == "submit_review":
            return block["input"]
    raise ReviewError("model did not submit a review when forced")
