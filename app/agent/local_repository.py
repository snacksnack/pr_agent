"""Repository access from a checkout on disk (RC1-109, RC1-424).

The :class:`app.agent.repository.RepositoryAccess` adapter for a local
checkout: the dry-run CLI's ``--repo-path``, the eval corpus's materialised
cases, the measurement scripts' worktrees. Every path is resolved against one
root and refused outside it, symlinks included; the guards on what a review
may see are the shared helpers in :mod:`app.agent.repository`.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from app.agent.repository import (
    IGNORED_DIRS,
    MAX_GREP_FILE_BYTES,
    MAX_GREP_MATCHES,
    MAX_READ_BYTES,
    RepositoryError,
    compile_pattern,
    grep_text,
    is_withheld,
    render_grep,
)


class LocalRepository:
    """Bounded reads and greps scoped to a single repository root."""

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise RepositoryError(f"repo root is not a directory: {root}")

    @property
    def explorable(self) -> bool:
        """Whether there is anything here to explore (RC1-390). The dry-run
        CLI hands the pipeline an empty directory when it has no checkout."""
        return any(child.name not in IGNORED_DIRS for child in self.root.iterdir())

    # -- path safety -----------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        """Resolve ``rel`` against the root and refuse anything outside it."""
        root = self.root.resolve()
        candidate = (self.root / rel).resolve()
        if candidate != root and root not in candidate.parents:
            raise RepositoryError(f"path escapes the repository root: {rel!r}")
        return candidate

    def _relpath(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(p)

    def _walk_files(self, base: Path) -> Iterator[Path]:
        if base.is_file():
            if not is_withheld(base.name):
                yield base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in sorted(filenames):
                if is_withheld(name):
                    continue  # never grep secret/credential or lock files
                yield Path(dirpath) / name

    # -- the contract ----------------------------------------------------

    def read_text(self, path: str, *, max_bytes: int = MAX_READ_BYTES) -> str | None:
        try:
            p = self._resolve(path)
        except RepositoryError:
            return None
        if is_withheld(p.name) or not p.is_file():
            return None
        try:
            return p.read_bytes()[:max_bytes].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def paths(self) -> list[str]:
        """Every file under the root the repository would serve, in walk
        order. Never ``None`` here: a checkout always has a file list."""
        return [self._relpath(f) for f in self._walk_files(self.root)]

    def grep(self, pattern: str, path: str = ".", *, max_results: int = MAX_GREP_MATCHES) -> str:
        regex = compile_pattern(pattern)
        base = self._resolve(path)
        if not base.exists():
            raise RepositoryError(f"no such path: {path!r}")

        results: list[str] = []
        truncated = False
        for file in self._walk_files(base):
            try:
                if file.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                content = file.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # skip binary / unreadable files
            if grep_text(regex, self._relpath(file), content, results, max_results):
                truncated = True
                break
        return render_grep(results, truncated=truncated, max_results=max_results)
