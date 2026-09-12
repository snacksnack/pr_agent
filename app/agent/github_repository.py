"""Repository access through the GitHub API at the PR head (RC1-364, RC1-424).

The :class:`app.agent.repository.RepositoryAccess` adapter for the live
webhook, which has no checkout. Until RC1-364 the live review's reads were
pointed at an empty directory: every read and grep came back ``no such
file`` and the review was written from the diff alone. This adapter serves
the same contract from the Git Trees API (the file list, one call) and the
Contents API (a file's text, one call each, cached), so the live path and the
dry-run CLI see the same repository.

Two things are different from the local adapter and deliberate:

* **A per-review API budget.** Each uncached file read and the one tree
  fetch cost a call; past ``api_budget`` the reads stop. Fetched files are
  cached, so re-reads are free. A ``read_text`` or ``paths`` the budget
  refuses is ``None``, like a missing file; a ``grep`` the budget refuses
  before it can read a single file raises :class:`RepositoryError`, so
  context gathering records it as not searched rather than as no matches.
* **grep is bounded, not exhaustive.** A local grep walks the whole checkout
  for nothing; here every candidate file is an API call. A search reads at
  most ``MAX_GREP_REMOTE_FILES`` files, changed files first, then what is
  already cached, then smallest first, and says how many candidates it did
  not reach.

Everything degrades rather than raises: an unreadable tree leaves
``read_text`` working, makes ``paths`` ``None`` and confines ``grep`` to the
PR's changed files, which are all this adapter can then know about.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from app.agent.repository import (
    MAX_GREP_FILE_BYTES,
    MAX_GREP_MATCHES,
    MAX_READ_BYTES,
    RepositoryError,
    compile_pattern,
    grep_text,
    is_noise_path,
    is_withheld,
    render_grep,
)
from app.models import PRRef

# Files one grep may fetch. Each is an API call; the per-review budget still
# applies on top.
MAX_GREP_REMOTE_FILES = 30

_UNSET = object()


class GitHubRepository:
    """The contract, read through ``gh`` (a :class:`app.github.GitHubClient`
    or anything with its ``get_file_text`` and ``get_tree``) at ``head_sha``."""

    def __init__(
        self,
        gh: Any,
        ref: PRRef,
        head_sha: str,
        *,
        changed_files: list[str] | tuple[str, ...] = (),
        api_budget: int = 60,
    ) -> None:
        self._gh = gh
        self._ref = ref
        self._sha = head_sha
        self._changed = [self._normalize(f) for f in changed_files]
        self._budget = api_budget
        self._calls = 0
        self._cache: dict[str, str | None] = {}
        self._tree: Any = _UNSET

    # A live review always has the repository behind it (RC1-390).
    explorable = True

    # -- accounting ------------------------------------------------------

    @property
    def api_calls(self) -> int:
        return self._calls

    @property
    def tree_available(self) -> bool:
        return self._tree is not _UNSET and self._tree is not None

    def _exhausted(self) -> RepositoryError:
        return RepositoryError(f"GitHub API budget exhausted ({self._budget} calls this review)")

    def _spend(self) -> None:
        if self._calls >= self._budget:
            raise self._exhausted()
        self._calls += 1

    # -- path safety -----------------------------------------------------

    @staticmethod
    def _normalize(rel: str) -> str:
        """Repo-relative POSIX path with no root escape; '' means the root."""
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise RepositoryError(f"path escapes the repository root: {rel!r}")
        return "/".join(parts)

    # -- sources ---------------------------------------------------------

    def _fetch(self, path: str) -> str | None:
        if path not in self._cache:
            self._spend()
            self._cache[path] = self._gh.get_file_text(self._ref, path, git_ref=self._sha)
        return self._cache[path]

    def _entries(self) -> list[dict] | None:
        if self._tree is _UNSET:
            self._spend()
            self._tree = self._gh.get_tree(self._ref, self._sha)
        return self._tree

    @staticmethod
    def _under(path: str, rel: str) -> bool:
        return not rel or path == rel or path.startswith(rel + "/")

    @staticmethod
    def _served(entry: dict) -> bool:
        """A blob the review may see: not noise, not secret, not a lock file."""
        path = entry["path"]
        return (
            entry.get("type") == "blob"
            and not is_noise_path(path)
            and not is_withheld(PurePosixPath(path).name)
        )

    # -- the contract ----------------------------------------------------

    def read_text(self, path: str) -> str | None:
        """One API call when uncached, none when the budget is spent: the
        context is optional, the review is not."""
        try:
            rel = self._normalize(path)
        except RepositoryError:
            return None
        if not rel or is_withheld(PurePosixPath(rel).name):
            return None
        try:
            text = self._fetch(rel)
        except RepositoryError:
            return None
        return text[:MAX_READ_BYTES] if text is not None else None

    def paths(self) -> list[str] | None:
        """Every served blob path in the tree at the PR head; ``None`` when
        there is no tree to read — not readable, or the budget is spent
        before the one call it costs."""
        try:
            entries = self._entries()
        except RepositoryError:
            return None
        if entries is None:
            return None
        return [e["path"] for e in entries if self._served(e)]

    def grep(self, pattern: str, path: str = ".", *, max_results: int = MAX_GREP_MATCHES) -> str:
        regex = compile_pattern(pattern)
        rel = self._normalize(path)
        candidates = self._grep_candidates(rel)

        results: list[str] = []
        truncated = False
        scanned = 0
        stopped_early = False
        for file in candidates:
            if scanned >= MAX_GREP_REMOTE_FILES:
                stopped_early = True
                break
            if file not in self._cache and self._calls >= self._budget:
                if scanned == 0:
                    raise self._exhausted()  # nothing searched is not "no matches"
                stopped_early = True
                break
            content = self._fetch(file)
            scanned += 1
            if content is None:
                continue
            if grep_text(regex, file, content, results, max_results):
                truncated = True
                break

        out = render_grep(results, truncated=truncated, max_results=max_results)
        if stopped_early:
            out += (
                f"\n... [searched {scanned} of {len(candidates)} candidate files "
                "under the live-review API budget]"
            )
        return out

    def _grep_candidates(self, rel: str) -> list[str]:
        """Files worth fetching for a grep under ``rel``, changed files first.

        With a tree: every served blob under ``rel`` that is not over the
        size cap, ordered changed -> already cached -> smallest first.
        Without one: only the PR's changed files, which are all the adapter
        can know about.
        """
        entries = self._entries()
        changed = [
            f for f in self._changed
            if self._under(f, rel) and not is_withheld(PurePosixPath(f).name)
        ]
        if entries is None:
            return changed
        pool = [
            e["path"]
            for e in entries
            if self._served(e)
            and self._under(e["path"], rel)
            and int(e.get("size", 0)) <= MAX_GREP_FILE_BYTES
        ]
        sizes = {e["path"]: int(e.get("size", 0)) for e in entries}
        changed_set = set(changed)
        pool.sort(key=lambda f: (f not in changed_set, f not in self._cache, sizes.get(f, 0), f))
        return pool
