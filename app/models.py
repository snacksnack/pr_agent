"""Normalized internal models for a pull request under review (RC1-108).

These are source-agnostic: the dry-run CLI populates them from the GitHub REST
API via a PAT, and the webhook service (RC1-116) will populate the same shapes
using installation-token auth. Everything downstream — the agent loop, rubric,
and checks — consumes these models, not raw GitHub JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PRRef:
    """Identifies a pull request: ``owner/repo#number``."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass
class ChangedFile:
    """A single file changed by the PR, with its unified-diff patch."""

    filename: str
    status: str  # added | modified | removed | renamed | copied | changed | unchanged
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    # The unified-diff hunks for this file. ``None`` when GitHub omits the patch
    # (binary files, or diffs too large to inline) — callers must handle that.
    patch: str | None = None
    previous_filename: str | None = None
    sha: str | None = None

    @property
    def has_patch(self) -> bool:
        return bool(self.patch)


@dataclass
class PullRequest:
    """A pull request plus its changed files, normalized for review."""

    ref: PRRef
    title: str = ""
    body: str = ""
    state: str = ""
    author: str | None = None
    base_ref: str = ""
    head_ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files_count: int = 0
    files: list[ChangedFile] = field(default_factory=list)
    # True if we stopped collecting files at the configured cap (very large PR).
    truncated_files: bool = False
    html_url: str | None = None

    @property
    def slug(self) -> str:
        return self.ref.slug
