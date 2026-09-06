"""Deterministic routing for the multi-agent review (RC1-390).

Which reviewers run, and on which dimensions, is decided here from the PR's
file list — by Python, never by a model. The three reviewers are split by the
evidence each needs (see :mod:`app.agent.prompts`), and the only real
decisions are whether that evidence exists for this PR:

* the **scout** and the **repo-context reviewer** need code to explore; a
  change that touches only documentation gives them nothing, so they are
  skipped;
* the **change-intent reviewer** always reads the description against the
  diff and judges the scale of touched IO, and picks up dependency review
  only when a manifest changed and n8n review only when a workflow export
  changed;
* the **diff-local reviewer** always runs: a secret can be committed to any
  file.

The plan is a value, so a test can assert it and a log line can print it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.context import RepoContext
from app.agent.prompts import CHANGE_INTENT, DIFF_LOCAL, REPO_CONTEXT, ReviewerSpec
from app.agent.tools import is_lockfile
from app.models import ChangedFile, PullRequest

# Dependency manifests, by basename. Lock files count too (``is_lockfile``):
# their patch is omitted from the diff, but a bumped lock is still a changed
# dependency for the reviewer to weigh from the header.
MANIFEST_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "environment.yml",
        "package.json",
        "go.mod",
        "cargo.toml",
        "gemfile",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "composer.json",
    }
)
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

_DEPENDENCIES = 5
_N8N = 10


@dataclass(frozen=True)
class ReviewPlan:
    """What the multi-agent path will run for one PR, and why."""

    scout: bool
    reviewers: tuple[ReviewerSpec, ...]
    reasons: tuple[str, ...] = field(default=())

    @property
    def names(self) -> list[str]:
        return [r.name for r in self.reviewers]


def is_manifest(filename: str) -> bool:
    base = filename.rsplit("/", 1)[-1].lower()
    if base in MANIFEST_NAMES or is_lockfile(base):
        return True
    return base.startswith("requirements") and base.endswith(".txt")


def is_doc(filename: str) -> bool:
    return not is_manifest(filename) and filename.lower().endswith(DOC_SUFFIXES)


def looks_like_workflow(f: ChangedFile) -> bool:
    """An n8n workflow export, judged from what the router can see: the
    path, and the patch when there is one. The deterministic check parses
    the full file; this only decides whether the model reviewer looks."""
    if not f.filename.lower().endswith(".json"):
        return False
    if "workflow" in f.filename.lower():
        return True
    patch = f.patch or ""
    return "n8n-nodes-base" in patch or '"nodes"' in patch


def plan_review(pr: PullRequest, *, explorable: bool = True) -> ReviewPlan:
    """The plan for ``pr``. ``explorable`` is whether the repo tools have a
    repository behind them; without one (the dry-run CLI with no
    ``--repo-path``) the scout would spend its turns learning that every
    tool call fails, so Python skips it and the reviewers work from the
    diff, which is what they would have got anyway."""
    files = list(pr.files)
    docs_only = bool(files) and all(is_doc(f.filename) for f in files)
    manifests = [f.filename for f in files if is_manifest(f.filename)]
    workflows = [f.filename for f in files if looks_like_workflow(f)]

    reasons: list[str] = []
    reviewers: list[ReviewerSpec] = [DIFF_LOCAL]

    if docs_only:
        reasons.append("documentation-only change: scout and repo_context skipped")
    else:
        reviewers.append(REPO_CONTEXT)
    scout = not docs_only
    if scout and not explorable:
        scout = False
        reasons.append("no repository checkout to explore: scout skipped")

    dimensions = list(CHANGE_INTENT.dimensions)
    if manifests:
        reasons.append(f"manifest changed ({', '.join(manifests[:3])}): dependencies reviewed")
    else:
        dimensions.remove(_DEPENDENCIES)
    if workflows:
        reasons.append(f"workflow export changed ({', '.join(workflows[:3])}): n8n reviewed")
    else:
        dimensions.remove(_N8N)
    reviewers.append(CHANGE_INTENT.narrowed(tuple(dimensions)))

    return ReviewPlan(scout=scout, reviewers=tuple(reviewers), reasons=tuple(reasons))


def scout_turns(
    context: RepoContext, *, full: int, with_context: int, when_complete: int = 0
) -> int:
    """The scout's turn cap for this review, decided after the context is built
    (RC1-393, RC1-394).

    Three cases, from a value:

    * **Complete** — the conventions file was found, the callers search ran
      to completion and the tests search reached an answer: every kind of
      evidence the scout used to gather is in the prefix, and it gets
      ``when_complete`` turns, zero by default, which skips it (RC1-394).
    * **Answered but for the tests** — conventions and callers in hand, the
      tests search cut off before it started (the live path's read budget):
      the scout's remaining job is small and it gets the short
      ``with_context`` cap (RC1-393).
    * **Otherwise** — no conventions file, or the callers search was cut
      off: the scout has the whole job and the full cap.

    A diff that defines nothing Python could name still counts as answered:
    no changed interface is an answer to the callers question, not a gap;
    likewise a diff with no source file a test could reference. Measured in
    the RC1-393 record: with the full cap the scout spends every turn
    whatever it was handed, so the cap has to follow the context.
    """
    answered = bool(context.conventions) and not context.search_stopped
    if not answered:
        return full
    if context.complete:
        return min(full, when_complete)
    return min(full, with_context)
