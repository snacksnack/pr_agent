"""Repo-exploration tools for the agent (RC1-109).

Gives the review agent its "eyes" on a repository: read a file, list a
directory, and grep for a pattern. All three are scoped to a single repo root
with strict path-safety (no escaping the root, no symlink traversal) and bounded
output so a huge file or a noisy grep can't blow up the context window or cost.

The tools here operate on a local checkout (the dry-run CLI's ``--repo-path``).
The live webhook has no checkout; :mod:`app.agent.remote_tools` serves the same
three tools from the GitHub Contents API at the PR head (RC1-364). Both share
the formatting and matching helpers below and the :func:`dispatch_tool` contract,
so the agent loop cannot tell which one it is talking to.

Each tool is also exposed as an Anthropic tool schema (``TOOL_SCHEMAS``) and
invoked via :meth:`RepoTools.dispatch`, which the agent loop (RC1-110) drives.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

# Output guardrails (cost / context-window protection).
MAX_READ_BYTES = 64_000
MAX_READ_LINES = 800
MAX_LIST_ENTRIES = 400
MAX_GREP_MATCHES = 200
MAX_GREP_FILE_BYTES = 1_000_000
SNIPPET_MAX = 240

# Directories never worth reading/searching.
IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

# Secret/credential files the exploration tools must never read, grep, or list.
# These are typically gitignored and only present incidentally in a local
# checkout, so surfacing them caused a false "committed secret" blocker and
# leaked real local secrets into the model context (RC1-114 tuning finding).
# Genuine *committed* secrets are still caught from the PR diff at ingestion;
# the exploration tools just shouldn't be the vector. Filename globs, matched
# case-insensitively.
SECRET_FILE_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.*",
    ".npmrc",
    ".pypirc",
    ".netrc",
)
# Generated dependency lock files (RC1-365). Not secret, just noise: thousands
# of lines of registry URLs and hashes that burn the read budget, fill the seed
# diff, and light up Datadog's sensitive-data scan on every trace. Dependency
# changes are reviewed from the manifest and the diff header instead.
LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "uv.lock",
        "poetry.lock",
        "pipfile.lock",
        "pdm.lock",
        "cargo.lock",
        "go.sum",
        "composer.lock",
        "gemfile.lock",
        "packages.lock.json",
        "flake.lock",
    }
)

# Lookalikes that are safe and useful context (templates, public keys): allowed
# even though they match a secret glob above. Checked first.
SECRET_ALLOW_GLOBS = (
    "*.example",
    "*.sample",
    "*.template",
    "*.dist",
    "*.pub",
)


def is_secret_file(name: str) -> bool:
    """True if a filename looks like a secret/credential file to be withheld.

    Allow-list (templates, ``*.pub``) wins over the secret globs so that, e.g.,
    ``.env.example`` stays readable while ``.env`` / ``.env.local`` do not.
    """
    lowered = name.lower()
    if any(fnmatch.fnmatch(lowered, pat) for pat in SECRET_ALLOW_GLOBS):
        return False
    return any(fnmatch.fnmatch(lowered, pat) for pat in SECRET_FILE_GLOBS)


def is_lockfile(name: str) -> bool:
    """True for a generated dependency lock file, matched by basename."""
    return name.lower() in LOCKFILE_NAMES


def lockfile_refusal(path: str) -> ToolError:
    return ToolError(
        f"refused: {path!r} is a generated lock file; review dependency changes "
        "from the manifest (package.json, pyproject.toml, ...) and the diff header."
    )


class ToolError(Exception):
    """A recoverable tool failure (bad path, binary file, etc.).

    The dispatcher turns these into strings so the model can adjust and retry
    rather than crashing the review loop.
    """


class RepoTools:
    """Filesystem tools scoped to a single repository root."""

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise ToolError(f"repo root is not a directory: {root}")

    @property
    def explorable(self) -> bool:
        """Whether there is anything here to explore (RC1-390). The dry-run
        CLI hands the agent an empty directory when it has no checkout."""
        return any(child.name not in IGNORED_DIRS for child in self.root.iterdir())

    # -- path safety -----------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        """Resolve ``rel`` against the root and refuse anything outside it."""
        root = self.root.resolve()
        candidate = (self.root / rel).resolve()
        if candidate != root and root not in candidate.parents:
            raise ToolError(f"path escapes the repository root: {rel!r}")
        return candidate

    def _relpath(self, p: Path) -> str:
        try:
            return str(p.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(p)

    def _walk_files(self, base: Path):
        if base.is_file():
            if not is_secret_file(base.name) and not is_lockfile(base.name):
                yield base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in sorted(filenames):
                if is_secret_file(name) or is_lockfile(name):
                    continue  # never grep secret/credential or lock files
                yield Path(dirpath) / name

    # -- tools -----------------------------------------------------------

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Return a file's contents with line numbers, optionally a line range."""
        p = self._resolve(path)
        if is_secret_file(p.name):
            raise ToolError(
                f"refused: {path!r} looks like a secrets/credentials file; the "
                "reviewer does not read these (they're typically gitignored and "
                "not part of the PR). Review committed changes from the diff."
            )
        if is_lockfile(p.name):
            raise lockfile_refusal(path)
        if not p.exists():
            raise ToolError(f"no such file: {path!r}")
        if p.is_dir():
            raise ToolError(f"{path!r} is a directory; use list_dir")

        size = p.stat().st_size
        return format_file_text(path, p.read_bytes()[:MAX_READ_BYTES], size, start_line, end_line)

    def read_text(self, path: str) -> str | None:
        """A file's raw text, or ``None`` when there is nothing to read.

        For Python callers (RC1-393's deterministic context), not the model:
        no line numbers, no tool error — a missing, secret, lock, binary or
        oversized file is ``None`` and the caller carries on. Clipped to
        ``MAX_READ_BYTES`` like ``read_file``.
        """
        try:
            p = self._resolve(path)
        except ToolError:
            return None
        if is_secret_file(p.name) or is_lockfile(p.name) or not p.is_file():
            return None
        try:
            return p.read_bytes()[:MAX_READ_BYTES].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def paths(self) -> list[str]:
        """Every file path under the root the tools would serve, for Python
        callers (RC1-394's tests search). Noise directories, secret and lock
        files are left out, as ``grep`` leaves them out; order is the walk's.
        Never raises."""
        return [self._relpath(f) for f in self._walk_files(self.root)]

    def list_dir(self, path: str = ".") -> str:
        """List a directory (dirs first), excluding noise dirs like .git."""
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"no such directory: {path!r}")
        if not p.is_dir():
            raise ToolError(f"{path!r} is not a directory; use read_file")

        entries: list[str] = []
        children = sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))
        for child in children:
            if child.name in IGNORED_DIRS:
                continue
            if child.is_file() and (is_secret_file(child.name) or is_lockfile(child.name)):
                continue  # hide secret/credential and lock files from listings
            if child.is_dir():
                entries.append(f"{child.name}/")
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"{child.name} ({size} B)")

        shown = entries[:MAX_LIST_ENTRIES]
        header = f"{self._relpath(p)}/"
        out = header + "\n" + "\n".join(f"  {e}" for e in shown)
        if len(entries) > MAX_LIST_ENTRIES:
            out += f"\n  ... [{len(entries) - MAX_LIST_ENTRIES} more entries omitted]"
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
        """Search files under ``path`` for ``pattern``; return ``file:line: text``."""
        regex = compile_pattern(pattern, ignore_case=ignore_case, fixed=fixed)

        base = self._resolve(path)
        if not base.exists():
            raise ToolError(f"no such path: {path!r}")

        results: list[str] = []
        truncated = False
        for file in self._walk_files(base):
            if glob and not fnmatch.fnmatch(file.name, glob):
                continue
            try:
                if file.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                content = file.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # skip binary / unreadable files
            if grep_text(regex, self._relpath(file), content, results, max_results):
                truncated = True
                break

        if not results:
            return "(no matches)"
        out = "\n".join(results)
        if truncated:
            out += f"\n... [stopped at {max_results} matches]"
        return out

# --- shared by the local and remote backends --------------------------------

def format_file_text(
    path: str,
    raw: bytes,
    size: int,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Number ``raw`` (already clipped to MAX_READ_BYTES) and slice it.

    ``size`` is the file's true size so the clip note is accurate.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{path!r} is binary or not UTF-8 text") from exc

    lines = text.splitlines()
    total = len(lines)
    start = max(1, start_line) if start_line else 1
    end = min(total, end_line) if end_line else total
    if start > total:
        return f"({path!r} has {total} lines; start_line {start} is past the end)"

    selected = lines[start - 1 : end]
    line_truncated = False
    if len(selected) > MAX_READ_LINES:
        selected = selected[:MAX_READ_LINES]
        end = start + MAX_READ_LINES - 1
        line_truncated = True

    width = len(str(end)) or 1
    body = "\n".join(
        f"{i:>{width}}  {line}" for i, line in enumerate(selected, start=start)
    )

    notes = []
    if size > MAX_READ_BYTES:
        notes.append(f"file clipped to first {MAX_READ_BYTES} bytes")
    if line_truncated:
        notes.append(f"output capped at {MAX_READ_LINES} lines")
    return body + (f"\n... [{'; '.join(notes)}]" if notes else "")


def compile_pattern(pattern: str, *, ignore_case: bool = False, fixed: bool = False) -> re.Pattern:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(re.escape(pattern) if fixed else pattern, flags)
    except re.error as exc:
        raise ToolError(f"invalid regex {pattern!r}: {exc}") from exc


def grep_text(
    regex: re.Pattern, relpath: str, content: str, results: list[str], max_results: int
) -> bool:
    """Append ``relpath:line: snippet`` rows for matches; True once the cap is hit."""
    for num, line in enumerate(content.splitlines(), start=1):
        if regex.search(line):
            results.append(f"{relpath}:{num}: {line.strip()[:SNIPPET_MAX]}")
            if len(results) >= max_results:
                return True
    return False
