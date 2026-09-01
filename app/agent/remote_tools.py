"""Repo-exploration tools served from the GitHub API (RC1-364).

The live webhook has no checkout, and until RC1-364 the agent's file tools were
pointed at an empty directory: every ``read_file`` and ``grep`` in a live review
came back ``no such file``, the model spent turns learning it was blind, and the
review was written from the diff alone. This backend serves the same three tools
from the Contents API at the PR head, so the live path and the dry-run CLI see
the same repository.

Same contract as :class:`app.agent.tools.RepoTools` — ``read_file``, ``list_dir``,
``grep``, and ``dispatch`` — and the same formatting, caps, and secret-file
rules, so the agent loop cannot tell the two apart. Two things are different and
deliberate:

* **A per-review API budget.** Each uncached file read and the one tree fetch
  cost a call; past ``api_budget`` the tools refuse and tell the model to
  submit. Fetched files are cached, so re-reads are free.
* **grep is bounded, not exhaustive.** A local grep walks the whole checkout for
  nothing; here every candidate file is an API call. The search reads at most
  ``MAX_GREP_REMOTE_FILES`` files, changed files first, and says how many
  candidates it did not reach so the model can narrow with a path or glob.

Everything degrades rather than raises: an unreadable tree leaves ``read_file``
working and confines ``grep`` to the PR's changed files; a missing or oversized
file is a tool error string, never an exception out of the loop.
"""
from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from typing import Any

from app.agent.tools import (
    IGNORED_DIRS,
    MAX_GREP_FILE_BYTES,
    MAX_GREP_MATCHES,
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    ToolError,
    compile_pattern,
    dispatch_tool,
    format_file_text,
    grep_text,
    is_secret_file,
)
from app.models import PRRef

# Files one grep may fetch. Each is an API call; the per-review budget still
# applies on top.
MAX_GREP_REMOTE_FILES = 30

_UNSET = object()


class RemoteRepoTools:
    """The three exploration tools, read through ``gh`` at ``head_sha``."""

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

    # -- accounting ------------------------------------------------------

    @property
    def api_calls(self) -> int:
        return self._calls

    @property
    def tree_available(self) -> bool:
        return self._tree is not _UNSET and self._tree is not None

    def _spend(self) -> None:
        if self._calls >= self._budget:
            raise ToolError(
                f"GitHub API budget exhausted ({self._budget} calls this review); "
                "submit your review with what you have"
            )
        self._calls += 1

    # -- path safety -----------------------------------------------------

    @staticmethod
    def _normalize(rel: str) -> str:
        """Repo-relative POSIX path with no root escape; '' means the root."""
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ToolError(f"path escapes the repository root: {rel!r}")
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
    def _noise(path: str) -> bool:
        return any(part in IGNORED_DIRS for part in path.split("/"))

    # -- tools -----------------------------------------------------------

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        rel = self._normalize(path)
        if not rel:
            raise ToolError("'.' is a directory; use list_dir")
        if is_secret_file(PurePosixPath(rel).name):
            raise ToolError(
                f"refused: {path!r} looks like a secrets/credentials file; the "
                "reviewer does not read these. Review committed changes from the diff."
            )
        text = self._fetch(rel)
        if text is None:
            raise ToolError(
                f"no such file at the PR head: {path!r} (or it is a directory, "
                "binary, or over the API's 1 MB limit)"
            )
        raw = text.encode("utf-8")
        return format_file_text(rel, raw[:MAX_READ_BYTES], len(raw), start_line, end_line)

    def list_dir(self, path: str = ".") -> str:
        rel = self._normalize(path)
        entries = self._entries()
        if entries is None:
            raise ToolError(
                "the repository tree is not readable on this review; read_file "
                "still works for paths you know from the diff"
            )
        prefix = rel + "/" if rel else ""
        dirs: set[str] = set()
        files: list[tuple[str, int]] = []
        for e in entries:
            p = e["path"]
            if not p.startswith(prefix) or p == rel:
                continue
            rest = p[len(prefix):]
            head = rest.split("/", 1)[0]
            if head in IGNORED_DIRS:
                continue
            if "/" in rest or e.get("type") == "tree":
                dirs.add(head)
            elif not is_secret_file(rest):
                files.append((rest, int(e.get("size", 0))))
        if not dirs and not files:
            raise ToolError(f"no such directory: {path!r}")

        listed = [f"{d}/" for d in sorted(dirs, key=str.lower)]
        listed += [f"{n} ({s} B)" for n, s in sorted(files, key=lambda f: f[0].lower())]
        shown = listed[:MAX_LIST_ENTRIES]
        out = f"{rel or '.'}/\n" + "\n".join(f"  {e}" for e in shown)
        if len(listed) > MAX_LIST_ENTRIES:
            out += f"\n  ... [{len(listed) - MAX_LIST_ENTRIES} more entries omitted]"
        return out

    def grep(
        self,
        pattern: str,
        path: str = ".",
        *,
        ignore_case: bool = False,
        fixed: bool = False,
        glob: str | None = None,
        max_results: int = MAX_GREP_MATCHES,
    ) -> str:
        regex = compile_pattern(pattern, ignore_case=ignore_case, fixed=fixed)
        rel = self._normalize(path)
        candidates = self._grep_candidates(rel, glob)

        results: list[str] = []
        truncated = False
        scanned = 0
        stopped_early = False
        for file in candidates:
            if scanned >= MAX_GREP_REMOTE_FILES:
                stopped_early = True
                break
            if file not in self._cache and self._calls >= self._budget:
                stopped_early = True
                break
            content = self._fetch(file)
            scanned += 1
            if content is None:
                continue
            if grep_text(regex, file, content, results, max_results):
                truncated = True
                break

        out = "\n".join(results) if results else "(no matches)"
        if truncated:
            out += f"\n... [stopped at {max_results} matches]"
        if stopped_early:
            out += (
                f"\n... [searched {scanned} of {len(candidates)} candidate files "
                "(live-review API budget); narrow with a path or glob]"
            )
        return out

    def _grep_candidates(self, rel: str, glob: str | None) -> list[str]:
        """Files worth fetching for a grep under ``rel``, changed files first.

        With a tree: every blob under ``rel`` that is not noise, secret, or over
        the size cap, ordered changed -> already cached -> smallest first.
        Without one: only the PR's changed files, which are all the loop can
        know about.
        """
        entries = self._entries()
        changed = [f for f in self._changed if self._under(f, rel)]
        if entries is None:
            pool = changed
        else:
            pool = [
                e["path"]
                for e in entries
                if e.get("type") == "blob"
                and self._under(e["path"], rel)
                and not self._noise(e["path"])
                and not is_secret_file(PurePosixPath(e["path"]).name)
                and int(e.get("size", 0)) <= MAX_GREP_FILE_BYTES
            ]
            sizes = {e["path"]: int(e.get("size", 0)) for e in entries}
            changed_set = set(changed)
            pool.sort(
                key=lambda f: (
                    f not in changed_set,
                    f not in self._cache,
                    sizes.get(f, 0),
                    f,
                )
            )
        if glob:
            pool = [f for f in pool if fnmatch.fnmatch(PurePosixPath(f).name, glob)]
        return pool

    # -- dispatch --------------------------------------------------------

    def dispatch(self, name: str, tool_input: dict) -> str:
        return dispatch_tool(self, name, tool_input)
