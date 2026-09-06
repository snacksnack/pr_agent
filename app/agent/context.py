"""Deterministic repository context for the multi-agent review (RC1-393).

RC1-390 measured the scout as the cost of the multi-agent path: on the one
corpus case with a repository to explore it spent more than the whole single
loop, and most of what it spent was on questions whose answers are a
property of the repository, not of the PR. Two of those questions have
answers Python can fetch with no model turn at all:

* **What conventions does this repository follow?** Every repo in the
  estate carries a ``CLAUDE.md`` (some a ``CONTRIBUTING.md``) that says so in
  prose. :func:`conventions_file` reads the first one it finds and keeps the
  sections a reviewer needs — conventions, style, testing, layout — ahead
  of the rest, under a character cap, because the text lands in a prefix
  four model calls read.
* **Who calls what the change touched?** :func:`changed_symbols` reads the
  added and removed ``def``/``class``/constant lines out of the hunks, and
  :func:`callers` greps the repository for each, definition lines excluded,
  changed files first, capped. That is the repo-context reviewer's
  breaking-change question answered before any model runs.

:func:`build_repo_context` runs both and renders one block of text that
:mod:`app.agent.multi` puts in the shared prefix and the scout's seed. The
scout is then told to look only for what the block does not say. Nothing
here raises: a repository with no conventions file and a diff with no
symbols produce an empty context, and the review proceeds as it did before.

Both tool backends serve this module through the same two calls,
``read_text`` and ``grep``; the live path pays for them out of the
per-review API budget (RC1-364), and the files the grep fetches stay cached
for the scout.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools import ToolError, is_lockfile
from app.models import PullRequest

logger = logging.getLogger("app.agent.context")

# Where a repository states its conventions, in the order tried. The first
# one that reads is used.
CONVENTIONS_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
)
# The conventions text is read by the warm call at cache-write price and by
# every reviewer and the verifier at cache-read price; the cap is what keeps
# it cheaper than the model turns it replaces.
MAX_CONVENTIONS_CHARS = 6_000
# Headings that mark the sections a reviewer needs first. Matched
# case-insensitively as substrings, so "Conventions (match these)",
# "Code style" and "Testing" all count.
SECTION_KEYWORDS: tuple[str, ...] = (
    "convention",
    "style",
    "test",
    "layout",
    "structure",
    "pattern",
    "guideline",
    "idiom",
)

# Bounds on the callers search. Every symbol is one grep; on the live path a
# grep reads files through the API, so the symbol cap is also an API cap.
MAX_SYMBOLS = 12
MAX_CALLERS_PER_SYMBOL = 6
MAX_CALLER_ROWS = 40

_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)
# Added or removed definition lines. Python first; the JS form is there
# because the estate carries n8n and Node repositories too.
_PY_DEF = re.compile(r"^[+-]\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
_JS_DEF = re.compile(
    r"^[+-]\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
)
# A module-level constant or config key: column zero, SCREAMING_CASE.
_CONST = re.compile(r"^[+-]([A-Z][A-Z0-9_]{2,})\s*[:=]")
# Hits that are the definition itself, not a use of it.
_DEFINITION_HIT = re.compile(r"\b(?:def|class|function)\s+{name}\b|^\s*{name}\s*[:=]")


@dataclass
class RepoContext:
    """What Python found, and what it could not."""

    conventions_path: str | None = None
    conventions: str = ""
    conventions_truncated: bool = False
    symbols: list[str] = field(default_factory=list)
    symbols_unsearched: list[str] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    callers_truncated: bool = False
    # The tool backend refused (budget spent) part way through the search.
    search_stopped: bool = False

    @property
    def empty(self) -> bool:
        return not self.conventions and not self.symbols

    def render(self) -> str:
        """The block the reviewers and the scout read. Empty when there is
        nothing to say, so a review with no context has the RC1-390 prefix."""
        parts: list[str] = []
        if self.conventions:
            note = " (cut at the length cap)" if self.conventions_truncated else ""
            parts.append(
                f"Repository conventions, from {self.conventions_path}{note}:\n{self.conventions}"
            )
        if self.symbols:
            lines = [
                "Callers of what changed (found by grep, definition lines excluded; "
                f"symbols: {', '.join(self.symbols)}):"
            ]
            lines.extend(self.callers or ["(no references found outside the definitions)"])
            if self.unresolved and self.callers:
                lines.append(f"(no references found for: {', '.join(self.unresolved)})")
            if self.callers_truncated:
                lines.append(f"... [callers list capped at {MAX_CALLER_ROWS} rows]")
            if self.symbols_unsearched:
                lines.append(f"(not searched: {', '.join(self.symbols_unsearched)})")
            if self.search_stopped:
                lines.append("(the search stopped early: the repository read budget ran out)")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)


# --- the conventions file ------------------------------------------------------

def select_sections(text: str, limit: int = MAX_CONVENTIONS_CHARS) -> tuple[str, bool]:
    """Keep the sections a reviewer needs, first, under ``limit`` characters.

    The file is split at its markdown headings. Sections whose heading names
    a convention, style, testing, layout or pattern topic come first, in
    ``SECTION_KEYWORDS`` order, then the rest in file order, and the cap is
    filled section by section. A file with no headings is one section. Returns the text and
    whether anything was cut.
    """
    text = text.strip()
    if len(text) <= limit:
        return text, False

    marks = list(_HEADING.finditer(text))
    sections: list[tuple[str, str]] = []
    if not marks or marks[0].start() > 0:
        head = text[: marks[0].start()] if marks else text
        sections.append(("", head.strip()))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        sections.append((m.group(1), text[m.start() : end].strip()))

    def _priority(item: tuple[int, tuple[str, str]]) -> tuple[int, int]:
        index, (heading, _) = item
        lowered = heading.lower()
        ranks = [i for i, k in enumerate(SECTION_KEYWORDS) if k in lowered]
        rank = min(ranks) if ranks else len(SECTION_KEYWORDS)
        return rank, index

    # Conventions before testing before layout, then everything else in file
    # order: when the cap bites, the section a reviewer needs most survives.
    ordered = [s for _, s in sorted(enumerate(sections), key=_priority)]
    kept: list[str] = []
    used = 0
    cut = False
    for _, body in ordered:
        if not body:
            continue
        if used + len(body) + 2 <= limit:
            kept.append(body)
            used += len(body) + 2
            continue
        room = limit - used
        if room > 200:  # a fragment shorter than this is noise, not context
            kept.append(body[:room].rstrip() + "\n... [section cut]")
        cut = True
        break
    if len(kept) < sum(1 for _, b in ordered if b):
        cut = True
    return "\n\n".join(kept), cut


def conventions_file(tools: Any) -> tuple[str | None, str, bool]:
    """``(path, text, truncated)`` for the first conventions file that reads."""
    for path in CONVENTIONS_FILES:
        text = tools.read_text(path)
        if text and text.strip():
            body, cut = select_sections(text)
            return path, body, cut
    return None, "", False


# --- callers of what changed ---------------------------------------------------

def changed_symbols(pr: PullRequest) -> list[str]:
    """Names defined on an added or removed line of the diff, in diff order.

    A changed signature contributes its name once (the ``-`` and ``+`` lines
    both match). Dunder methods, ``test_*`` functions and anything defined
    in a test file are left out: nothing calls them.
    """
    seen: list[str] = []
    for f in pr.files:
        if not f.patch or is_lockfile(f.filename.rsplit("/", 1)[-1]):
            continue
        in_tests = "test" in f.filename.lower()
        for line in f.patch.splitlines():
            if line.startswith(("+++", "---")):
                continue
            m = _PY_DEF.match(line) or _JS_DEF.match(line) or _CONST.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in seen or len(name) < 3 or name.startswith("__"):
                continue
            if in_tests or name.startswith("test_"):
                continue
            seen.append(name)
    return seen


def callers(pr: PullRequest, tools: Any, symbols: list[str]) -> RepoContext:
    """Grep the repository for uses of each symbol, bounded, into a context.

    The grep is the same one the model gets, so the rows read the same:
    ``path:line: text``. Definition lines are dropped, since the diff already
    shows them; what is left is the callers, importers and references.
    """
    ctx = RepoContext(symbols=symbols[:MAX_SYMBOLS], symbols_unsearched=symbols[MAX_SYMBOLS:])
    for name in ctx.symbols:
        if ctx.callers_truncated or ctx.search_stopped:
            # Not searched is not the same as not found; the reviewers are
            # told which, so a missing caller is never read as no caller.
            ctx.symbols_unsearched.append(name)
            continue
        definition = re.compile(_DEFINITION_HIT.pattern.format(name=re.escape(name)))
        try:
            out = tools.grep(rf"\b{re.escape(name)}\b", max_results=MAX_CALLERS_PER_SYMBOL * 3)
        except ToolError as exc:
            logger.info("context_search_stopped symbol=%s reason=%s", name, exc)
            ctx.search_stopped = True
            ctx.symbols_unsearched.append(name)
            continue
        rows = [
            row
            for row in out.splitlines()
            if _is_hit(row) and not definition.search(row.split(": ", 1)[-1])
        ][:MAX_CALLERS_PER_SYMBOL]
        if not rows:
            ctx.unresolved.append(name)
            continue
        room = MAX_CALLER_ROWS - len(ctx.callers)
        if len(rows) > room:
            rows = rows[:room]
            ctx.callers_truncated = True
        ctx.callers.extend(rows)
    return ctx


def _is_hit(row: str) -> bool:
    """A ``path:line: text`` row, as opposed to a grep note or "(no matches)"."""
    return bool(re.match(r"^[^\s(].*?:\d+: ", row))


# --- the whole context ---------------------------------------------------------

def build_repo_context(pr: PullRequest, tools: Any) -> RepoContext:
    """Conventions file plus callers, for ``pr``, from ``tools``. Never raises."""
    symbols = changed_symbols(pr)
    ctx = callers(pr, tools, symbols) if symbols else RepoContext()
    ctx.conventions_path, ctx.conventions, ctx.conventions_truncated = conventions_file(tools)
    logger.info(
        "repo_context conventions=%s conventions_chars=%d symbols=%d callers=%d "
        "unresolved=%d stopped=%s",
        ctx.conventions_path,
        len(ctx.conventions),
        len(ctx.symbols),
        len(ctx.callers),
        len(ctx.unresolved),
        ctx.search_stopped,
    )
    return ctx
