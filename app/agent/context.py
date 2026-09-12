"""Deterministic repository context for the multi-agent review (RC1-393).

RC1-390 measured a model-driven scout as the cost of the multi-agent path:
on the one corpus case with a repository to explore it spent more than the
whole single loop, and most of what it spent was on questions whose answers
are a property of the repository, not of the PR. Those questions have
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
* **Which tests touch the changed paths?** (RC1-394) For each changed
  source file, :func:`tests_for` finds the test file named for it
  (``tests/test_<module>.py``, ``<module>_test.py``, ``<module>.test.*``)
  in the repository's file list, then greps the test tree for the module's
  name; the hits the callers grep made inside test files are moved here
  too. With it answered the context is complete on all three kinds of
  evidence (``complete`` is reported on the span and the metric).

:func:`build_repo_context` runs all three and renders one block of text
that :mod:`app.agent.pipeline` puts in the shared prefix. Since RC1-427 this
is the review's whole exploration: the scout that used to explore on top of
it was measured and retired. Nothing here raises: a repository with no
conventions file and a diff with no symbols produce an empty context, and
the reviewers work from the diff.

Both tool backends serve this module through the same three calls,
``read_text``, ``grep`` and ``paths``; the live path pays for them out of
the per-review API budget (RC1-364).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.local_repository import RepositoryError, is_lockfile
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

# Bounds on the tests search (RC1-394): one grep per changed source file per
# test root, rows capped per file and in total like the callers list.
MAX_SOURCE_FILES = 12
MAX_TESTS_PER_FILE = 6
MAX_TEST_ROWS = 30
MAX_TEST_ROOTS = 3
# Directory names that hold tests, by the conventions the estate uses
# (``tests/`` in the Python repos, ``__tests__`` in the Node ones).
TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})
# Files that are tests by name, wherever they sit.
_TEST_FILE = re.compile(r"^(?:test_.+\.py|.+_test\.py|.+\.(?:test|spec)\.\w+|conftest\.py)$")
# Files a test would never reference by module name.
_NOT_SOURCE_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

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
    # RC1-394: the tests search. ``tests_searched`` is whether it reached an
    # answer — a list of test files and rows, or the fact that there are
    # none — as opposed to not running or being cut off by the read budget.
    source_files: list[str] = field(default_factory=list)
    source_files_unsearched: list[str] = field(default_factory=list)
    test_roots: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    untested: list[str] = field(default_factory=list)
    # RC1-428: changed files that have tests, none of which fit under the
    # row cap. Told apart from ``untested`` because the reviewers read the
    # list literally: RC1-427's reference PR #39 drew three false "no tests"
    # warnings from files whose rows the cap had refused.
    tests_unlisted: list[str] = field(default_factory=list)
    tests_truncated: bool = False
    tests_searched: bool = False
    tests_stopped: bool = False

    @property
    def empty(self) -> bool:
        return not self.conventions and not self.symbols and not self.source_files

    @property
    def complete(self) -> bool:
        """Every kind of evidence is answered here:
        the conventions file was found, and neither search was cut off.
        Reported as ``context_complete`` on the result (RC1-394)."""
        return bool(self.conventions) and not self.search_stopped and self.tests_searched

    def render(self) -> str:
        """The block the reviewers read. Empty when there is
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
        if self.source_files or self.source_files_unsearched:
            parts.append("\n".join(self._render_tests()))
        return "\n\n".join(parts)

    def _render_tests(self) -> list[str]:
        where = (
            "under " + ", ".join(f"{r}/" for r in self.test_roots)
            if self.test_roots
            else "by file name, no test directory found"
        )
        sources = ", ".join(self.source_files + self.source_files_unsearched)
        lines = [
            f"Tests touching the changed paths (found by grep {where}; "
            f"changed source files: {sources}):"
        ]
        if self.tests_stopped and not self.tests:
            lines.append("(the tests search stopped early: the repository read budget ran out)")
            if self.source_files_unsearched:
                lines.append(f"(not searched: {', '.join(self.source_files_unsearched)})")
            return lines
        if self.tests:
            lines.extend(self.tests)
        elif self.untested and not self.source_files_unsearched and not self.test_roots:
            lines.append("(no test files found in the repository)")
        else:
            lines.append("(no test references the changed paths)")
        if self.untested and self.tests:
            lines.append(f"(no test references: {', '.join(self.untested)})")
        if self.tests_truncated:
            lines.append(f"... [tests list capped at {MAX_TEST_ROWS} rows]")
        if self.tests_unlisted:
            lines.append(
                "(tests exist but did not fit under the cap for: "
                f"{', '.join(self.tests_unlisted)})"
            )
        if self.source_files_unsearched:
            lines.append(f"(not searched: {', '.join(self.source_files_unsearched)})")
        if self.tests_stopped:
            lines.append("(the tests search stopped early: the repository read budget ran out)")
        return lines


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
        except RepositoryError as exc:
            logger.info("context_search_stopped symbol=%s reason=%s", name, exc)
            ctx.search_stopped = True
            ctx.symbols_unsearched.append(name)
            continue
        hits = [
            row
            for row in out.splitlines()
            if _is_hit(row) and not definition.search(row.split(": ", 1)[-1])
        ]
        # RC1-394: a hit inside a test file is a test touching the change,
        # not a caller; it goes to the tests section, under that cap.
        for row in hits:
            if is_test_path(row.split(":", 1)[0]) and row not in ctx.tests:
                if len(ctx.tests) < MAX_TEST_ROWS:
                    ctx.tests.append(row)
                else:
                    ctx.tests_truncated = True
        rows = [row for row in hits if not is_test_path(row.split(":", 1)[0])]
        rows = rows[:MAX_CALLERS_PER_SYMBOL]
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


# --- tests for the changed paths (RC1-394) ----------------------------------------

def is_test_path(path: str) -> bool:
    """Whether ``path`` is a test file: it sits under a test directory, or
    its name says so (``test_x.py``, ``x_test.py``, ``x.test.ts``,
    ``conftest.py``)."""
    parts = path.replace("\\", "/").split("/")
    return any(p in TEST_DIR_NAMES for p in parts[:-1]) or bool(_TEST_FILE.match(parts[-1]))


def changed_source_files(pr: PullRequest) -> list[str]:
    """The changed files a test could reference: not tests themselves, not
    lock files, not prose. Diff order."""
    out: list[str] = []
    for f in pr.files:
        name = f.filename.rsplit("/", 1)[-1]
        if is_test_path(f.filename) or is_lockfile(name):
            continue
        if f.filename.lower().endswith(_NOT_SOURCE_SUFFIXES):
            continue
        if f.filename not in out:
            out.append(f.filename)
    return out


def module_stem(path: str) -> str:
    """The name a test would import or mention: ``app/agent/context.py`` →
    ``context``; a package's ``__init__.py`` is named for its directory."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    stem = name.split(".", 1)[0] if "." in name else name
    if stem == "__init__" and len(parts) > 1:
        return parts[-2]
    return stem


def test_roots(paths: list[str]) -> list[str]:
    """The directories to grep for tests, most test files first, capped:
    the shortest path prefix ending in a test directory name, for every
    test file that sits under one. A repository whose tests are named but
    not gathered in a directory has no roots; the search then covers the
    whole tree and filters by name."""
    counts: dict[str, int] = {}
    for p in paths:
        parts = p.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in TEST_DIR_NAMES:
                root = "/".join(parts[: i + 1])
                counts[root] = counts.get(root, 0) + 1
                break
    ordered = sorted(counts, key=lambda r: (-counts[r], r))
    return ordered[:MAX_TEST_ROOTS]


def named_test_files(source: str, paths: list[str]) -> list[str]:
    """Test files named for ``source`` by the usual conventions."""
    stem = re.escape(module_stem(source))
    pattern = re.compile(rf"^(?:test_{stem}\.\w+|{stem}_test\.\w+|{stem}\.(?:test|spec)\.\w+)$")
    return sorted(p for p in paths if is_test_path(p) and pattern.match(p.rsplit("/", 1)[-1]))


def tests_for(pr: PullRequest, tools: Any, ctx: RepoContext) -> RepoContext:
    """Fill the tests section of ``ctx``: the test files named for each
    changed source file, then a grep of the test tree for the module's
    name, bounded. Rows the callers grep already moved here stay.

    ``tests_searched`` is set when the search reached an answer. A search
    the read budget cut off before it reached anything is not an answer
    (``tests_stopped``); one the caps cut short is.
    """
    sources = changed_source_files(pr)
    ctx.source_files = sources[:MAX_SOURCE_FILES]
    ctx.source_files_unsearched = sources[MAX_SOURCE_FILES:]
    if not ctx.source_files:
        ctx.tests_searched = True  # nothing a test could reference: answered
        return ctx

    paths = tools.paths()
    if paths is None:
        # No file list to read (the remote tree is unreadable, or the
        # budget is gone): the search cannot start.
        ctx.tests_stopped = True
        ctx.source_files_unsearched = ctx.source_files + ctx.source_files_unsearched
        ctx.source_files = []
        return ctx
    ctx.test_roots = test_roots(paths)
    any_tests = any(is_test_path(p) for p in paths)

    searched: list[str] = []
    for source in ctx.source_files:
        if ctx.tests_stopped:
            ctx.source_files_unsearched.append(source)
            continue
        found = listed = 0
        for path in named_test_files(source, paths):
            row = f"{path}: (test file named for {source})"
            found += 1
            listed += _add_test_row(ctx, row)
        stem = module_stem(source)
        if any_tests and len(stem) >= 3:
            hits, rows = _grep_tests(tools, ctx, stem, source)
            found += hits
            listed += rows
        if ctx.tests_stopped:
            # Cut off part-way through this file: what was found stays,
            # but the file was not searched, and is listed as such.
            ctx.source_files_unsearched.append(source)
            continue
        searched.append(source)
        if not found:
            ctx.untested.append(source)
        elif not listed:
            # RC1-428: tests exist, the cap refused every row. Not "untested",
            # and the reviewers are told so, since they cannot see the rows.
            ctx.tests_unlisted.append(source)
    if ctx.tests_stopped:
        ctx.source_files = searched
    ctx.tests_searched = not ctx.tests_stopped
    return ctx


def _grep_tests(tools: Any, ctx: RepoContext, stem: str, source: str) -> tuple[int, int]:
    """Grep each test root for ``stem`` as a whole word; rows into ``ctx``
    up to the per-file cap. Returns ``(hits, rows listed)``: a hit the row
    cap refused is still a test that exists (RC1-428), so the search keeps
    counting after the list is full and stops at the per-file cap."""
    hits = listed = 0
    for root in ctx.test_roots or ["."]:
        try:
            out = tools.grep(
                rf"\b{re.escape(stem)}\b", path=root, max_results=MAX_TESTS_PER_FILE * 3
            )
        except RepositoryError as exc:
            logger.info("tests_search_stopped file=%s reason=%s", source, exc)
            ctx.tests_stopped = True
            return hits, listed
        for row in out.splitlines():
            if not _is_hit(row) or not is_test_path(row.split(":", 1)[0]):
                continue
            if row.split(":", 1)[0].rsplit("/", 1)[-1] == source.rsplit("/", 1)[-1]:
                continue  # the source is itself under a test root
            hits += 1
            listed += _add_test_row(ctx, row)
            if hits >= MAX_TESTS_PER_FILE:
                return hits, listed
    return hits, listed


def _add_test_row(ctx: RepoContext, row: str) -> bool:
    """Put ``row`` in the list if it fits; whether it is listed."""
    if row in ctx.tests:
        return True  # already there (the callers grep put it there)
    if len(ctx.tests) >= MAX_TEST_ROWS:
        ctx.tests_truncated = True
        return False
    ctx.tests.append(row)
    return True


# --- the whole context ---------------------------------------------------------

def build_repo_context(pr: PullRequest, tools: Any) -> RepoContext:
    """Conventions file, callers and tests, for ``pr``, from ``tools``.
    Never raises."""
    symbols = changed_symbols(pr)
    ctx = callers(pr, tools, symbols) if symbols else RepoContext()
    ctx.conventions_path, ctx.conventions, ctx.conventions_truncated = conventions_file(tools)
    tests_for(pr, tools, ctx)
    logger.info(
        "repo_context conventions=%s conventions_chars=%d symbols=%d callers=%d "
        "unresolved=%d stopped=%s tests=%d untested=%d tests_searched=%s complete=%s",
        ctx.conventions_path,
        len(ctx.conventions),
        len(ctx.symbols),
        len(ctx.callers),
        len(ctx.unresolved),
        ctx.search_stopped,
        len(ctx.tests),
        len(ctx.untested),
        ctx.tests_searched,
        ctx.complete,
    )
    return ctx
