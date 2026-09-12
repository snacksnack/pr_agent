"""The one repository contract the review reads through (RC1-424).

The review's exploration is :mod:`app.agent.context`: Python reads the
conventions file, greps for callers of what changed and finds the tests
touching the changed paths, all before any model runs. It does that through
three calls and one property, and this module is where they are written down
once, as :class:`RepositoryAccess`, so the pipeline can be typed against the
contract rather than against whichever adapter it happens to be handed:

* :class:`app.agent.local_repository.LocalRepository` serves them from a
  checkout on disk — the dry-run CLI's ``--repo-path``, the eval corpus, the
  measurement scripts' worktrees;
* :class:`app.agent.github_repository.GitHubRepository` serves them from the
  Git Trees and Contents APIs at the PR head, under a per-review call budget
  (RC1-364) — the live webhook, which has no checkout.

The contract is the narrowest one that serves the pipeline. The model-facing
surface that once sat beside it — numbered ``read_file``, ``list_dir``, the
tool schemas and the dispatcher — went with the scout in RC1-427; nothing
reads a directory listing or a numbered page any more, so the adapters no
longer offer one.

What every adapter guarantees:

* **Bounded.** A read is clipped to ``MAX_READ_BYTES`` unless the caller
  names a larger ``max_bytes`` (a deterministic check parsing a whole
  workflow export, RC1-425); a grep stops at ``max_results`` rows and skips
  files over ``MAX_GREP_FILE_BYTES``.
* **Guarded.** Paths cannot escape the repository; secret and credential
  files (``.env``, keys, ``credentials``) and generated lock files are never
  read, grepped or listed; noise directories (``.git``, ``node_modules``,
  caches) are never walked. The rules are the helpers below, shared, so the
  two adapters cannot drift apart on what a review is allowed to see.
* **Recoverable in the same shape.** ``read_text`` and ``paths`` answer
  ``None`` when there is nothing to read — a missing or withheld file, an
  unreadable tree, a spent budget — and never raise. ``grep`` raises
  :class:`RepositoryError` when the search could not run at all (a bad
  pattern, a missing path, a budget that allows no file to be read) and
  returns what it found when it could run at least part-way. Context
  gathering treats the exception as "not searched", which the reviewers are
  told, so a search the budget refused is never rendered as "no callers".

The GitHub adapter's budget and tree fallback are its own (``api_calls``,
``tree_available``); the webhook that builds it logs them. The pipeline sees
only the contract.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Protocol, runtime_checkable

# Output guardrails (cost / context-window protection).
MAX_READ_BYTES = 64_000
MAX_GREP_MATCHES = 200
MAX_GREP_FILE_BYTES = 1_000_000
SNIPPET_MAX = 240

# Directories never worth reading or searching.
IGNORED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# Secret/credential files a review must never read, grep or list. These are
# typically gitignored and only present incidentally in a local checkout, so
# surfacing them caused a false "committed secret" blocker and leaked real
# local secrets into the model context (RC1-114 tuning finding). Genuine
# *committed* secrets are still caught from the PR diff at ingestion.
# Filename globs, matched case-insensitively.
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
# Lookalikes that are safe and useful context (templates, public keys): allowed
# even though they match a secret glob above. Checked first.
SECRET_ALLOW_GLOBS = (
    "*.example",
    "*.sample",
    "*.template",
    "*.dist",
    "*.pub",
)
# Generated dependency lock files (RC1-365). Not secret, just noise: thousands
# of lines of registry URLs and hashes that burn the read budget, fill the
# prefix, and light up Datadog's sensitive-data scan on every trace.
# Dependency changes are reviewed from the manifest and the diff header.
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


class RepositoryError(Exception):
    """A repository operation that could not run: a path outside the root,
    a pattern that does not compile, a search the API budget refused.

    Recoverable by design: context gathering catches it and records the
    search as not done. It reaches the dry-run CLI's boundary only when the
    checkout itself is unusable.
    """


@runtime_checkable
class RepositoryAccess(Protocol):
    """What the review may ask of a repository. Implemented by the local
    and GitHub adapters; injected into :func:`app.agent.pipeline.review_pull_request`."""

    @property
    def explorable(self) -> bool:
        """Whether there is a repository behind this at all. The dry-run CLI
        with no ``--repo-path`` hands the pipeline an empty directory; the
        router then skips the context and the reviewers work from the diff."""
        ...

    def read_text(self, path: str, *, max_bytes: int = MAX_READ_BYTES) -> str | None:
        """A file's raw text, clipped to ``max_bytes`` (the prefix-sized
        ``MAX_READ_BYTES`` unless a caller that parses the whole file — a
        deterministic check, RC1-425 — asks for more), or ``None`` when there
        is nothing to read: missing, a directory, binary, withheld (secret or
        lock file), outside the root, or out of budget."""
        ...

    def paths(self) -> list[str] | None:
        """Every file path the repository would serve, noise, secret and lock
        files left out; ``None`` when the list cannot be read at all."""
        ...

    def grep(self, pattern: str, path: str = ".", *, max_results: int = MAX_GREP_MATCHES) -> str:
        """Regex search under ``path``: ``file:line: text`` rows, one per
        line, ``(no matches)`` for none, a trailing ``... [note]`` line when
        the search was capped or cut short. Raises :class:`RepositoryError`
        when it could not search at all."""
        ...


# --- the guards, shared by both adapters ----------------------------------------

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


def is_withheld(name: str) -> bool:
    """True for a file the review never sees, by basename: secret or lock."""
    return is_secret_file(name) or is_lockfile(name)


def is_noise_path(path: str) -> bool:
    """True when any directory on a repo-relative POSIX path is ignored."""
    return any(part in IGNORED_DIRS for part in path.split("/"))


# --- grep, the parts that do not depend on where the files are ------------------

def compile_pattern(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RepositoryError(f"invalid regex {pattern!r}: {exc}") from exc


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


def render_grep(results: list[str], *, truncated: bool, max_results: int) -> str:
    """The rows as one string, with the cap note when the cap was hit."""
    out = "\n".join(results) if results else "(no matches)"
    if truncated:
        out += f"\n... [stopped at {max_results} matches]"
    return out
