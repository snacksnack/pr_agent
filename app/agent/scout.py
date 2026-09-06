"""The scout: explore once, write a brief (RC1-390).

The single loop (:mod:`app.agent.reviewer`) explores the repository and
writes the review in one conversation. The multi-agent path separates the
two: this module is the exploring half, re-purposed to end in a
``submit_brief`` call instead of ``submit_review``. It is the only agent in
the multi-agent review with tools, which is what keeps exploration paid for
once rather than once per reviewer.

Same loop mechanics as the reviewer — same tools, same caps, same moving
cache marker, same forced final call when the budget runs out — because the
mechanics are not what changed. What changed is the output: a short,
factual brief that the three reviewers read as part of their shared prefix.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.prompts import SCOUT_CONTEXT_NOTE, SCOUT_INSTRUCTIONS, SUBMIT_BRIEF_TOOL
from app.agent.reviewer import (
    DEFAULT_MAX_TOKENS,
    ReviewError,
    _create,
    _get,
    _normalize_blocks,
    _tokens,
    _user_text,
    render_pr,
)
from app.agent.tools import TOOL_SCHEMAS, RepoTools
from app.models import Finding, PullRequest, TokenUsage

logger = logging.getLogger("app.agent.scout")

SCOUT_TOOLS = [*TOOL_SCHEMAS, SUBMIT_BRIEF_TOOL]
# The brief is context for three reviewers' cached prefix; a scout that
# wrote a page would cost every reviewer that page. Cut, not rejected.
MAX_BRIEF_CHARS = 2_500


@dataclass
class Brief:
    """What the scout found, and what finding it cost."""

    text: str
    tool_turns: int = 0
    files_read: int = 0
    truncated: bool = False
    usage: TokenUsage = TokenUsage()
    skipped: bool = False


def skipped_brief(reason: str) -> Brief:
    return Brief(text=f"(scout skipped: {reason})", skipped=True)


def explore(
    pull_request: PullRequest,
    repo_tools: RepoTools,
    *,
    client: Any,
    model: str,
    max_tool_turns: int,
    max_files_read: int,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    precomputed_findings: list[Finding] | None = None,
    context: str = "",
) -> Brief:
    """Run the exploration loop and return the brief.

    ``context`` (RC1-393) is what Python already established — the
    conventions file and the callers list — rendered; it goes in the seed
    ahead of the instructions, with a note telling the scout not to redo it.
    """
    parts = [*render_pr(pull_request, precomputed_findings)]
    if context:
        parts += ["", context]
    parts += ["", SCOUT_INSTRUCTIONS]
    if context:
        parts += ["", SCOUT_CONTEXT_NOTE]
    seed = "\n".join(parts)
    messages: list[dict] = [_user_text(seed)]
    files_read = 0
    turns = 0
    truncated = False
    usage = TokenUsage()

    for _ in range(max_tool_turns):
        turns += 1
        response = _create(
            client, model=model, messages=messages, max_tokens=max_tokens, tools=SCOUT_TOOLS
        )
        usage = usage + _tokens(response)
        blocks = _normalize_blocks(_get(response, "content"))
        messages.append({"role": "assistant", "content": blocks})

        tool_uses = [b for b in blocks if b["type"] == "tool_use"]
        if not tool_uses:
            break  # stopped without submitting -> force a brief below

        tool_results = []
        brief: str | None = None
        for tu in tool_uses:
            name, tool_input, tool_id = tu["name"], tu["input"], tu["id"]
            if name == SUBMIT_BRIEF_TOOL["name"]:
                brief = str(tool_input.get("brief") or "")
                output = "Brief recorded."
            elif name == "read_file" and files_read >= max_files_read:
                truncated = True
                output = "Error: file-read budget exhausted. Submit your brief now."
            else:
                if name == "read_file":
                    files_read += 1
                output = repo_tools.dispatch(name, tool_input)
            tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": output})
        messages.append({"role": "user", "content": tool_results})

        if brief is not None:
            return _brief(brief, turns, files_read, truncated, usage)
    else:
        truncated = True

    nudge = messages + [
        _user_text("You have used your exploration budget. Call submit_brief now.")
    ]
    response = _create(
        client,
        model=model,
        messages=nudge,
        max_tokens=max_tokens,
        tools=SCOUT_TOOLS,
        tool_choice={"type": "tool", "name": SUBMIT_BRIEF_TOOL["name"]},
    )
    usage = usage + _tokens(response)
    for block in _normalize_blocks(_get(response, "content")):
        if block["type"] == "tool_use" and block["name"] == SUBMIT_BRIEF_TOOL["name"]:
            text = str(block["input"].get("brief") or "")
            return _brief(text, turns, files_read, truncated, usage)
    raise ReviewError("scout did not submit a brief when forced")


def _brief(text: str, turns: int, files_read: int, truncated: bool, usage: TokenUsage) -> Brief:
    text = text.strip()
    if len(text) > MAX_BRIEF_CHARS:
        text = text[:MAX_BRIEF_CHARS].rstrip() + "\n... [brief cut at the length cap]"
    if not text:
        text = "(the scout submitted an empty brief)"
    logger.info(
        "scout_done turns=%d files_read=%d truncated=%s brief_chars=%d context=%d out=%d",
        turns,
        files_read,
        truncated,
        len(text),
        usage.context_tokens,
        usage.output_tokens,
    )
    return Brief(
        text=text, tool_turns=turns, files_read=files_read, truncated=truncated, usage=usage
    )
