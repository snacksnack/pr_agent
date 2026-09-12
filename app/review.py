"""Local dry-run CLI (RC1-113).

Runs the full review against a real PR and prints the would-be review to the
terminal. Nothing is posted to GitHub — this is the tool used to tune review
quality before the App and hosting exist.

Pipeline: ingest the PR (RC1-108) -> the review pipeline (checks, reviewers,
verifier, one result; RC1-422, RC1-425) -> formatted terminal output. The
process exit code reflects whether a ``block_on`` finding was present, so this
is usable in CI later.

Usage:

    python -m app.review --pr owner/repo#123
    python -m app.review --pr owner/repo#123 --repo-path /path/to/local/clone

The agent explores the repo's files via ``--repo-path`` (a local checkout). If
omitted, the review still runs from the diff alone, but file exploration is
disabled.

Network calls (GitHub + Anthropic) happen only inside the default ingestion and
review callables; both are injectable so the CLI can be tested offline.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agent.local_repository import LocalRepository
from app.agent.pipeline import review_pull_request
from app.agent.repository import RepositoryAccess, RepositoryError
from app.config import settings
from app.github import GitHubError, fetch_pull_request, parse_pr_spec
from app.models import Finding, PRRef, PullRequest, ReviewResult
from app.verdict import gating_findings

# Exit codes (documented for later CI use).
EXIT_OK = 0          # review ran; no blocking finding
EXIT_BLOCKED = 1     # review ran; a block_on finding was present
EXIT_ERROR = 2       # the review could not be produced (ingestion/loop failure)

FetchFn = Callable[[PRRef], PullRequest]
# The review callable receives the PR and the repository and returns the one
# complete result; the deterministic checks run inside it (RC1-425).
ReviewFn = Callable[[PullRequest, RepositoryAccess], ReviewResult]

_SEVERITY_LABEL = {"blocker": "BLOCKER", "warning": "WARNING", "nit": "NIT"}


# --- argument parsing -----------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.review",
        description="Dry-run the PR review agent against a real pull request "
        "and print the would-be review (nothing is posted to GitHub).",
    )
    parser.add_argument(
        "--pr",
        required=True,
        metavar="owner/repo#N",
        help="Target pull request, e.g. octocat/hello-world#42",
    )
    parser.add_argument(
        "--repo-path",
        metavar="PATH",
        default=None,
        help="Path to a local checkout of the repo for file exploration. "
        "If omitted, the review runs from the diff alone.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Override the review model (default: {settings.review_model}).",
    )
    return parser


# --- block_on / verdict ---------------------------------------------------

def blocking_findings(result: ReviewResult, block_on: list[str]) -> list[Finding]:
    """Findings that gate a merge under the verdict policy (RC1-117).

    Delegates to the shared policy in :mod:`app.verdict`: a finding gates only
    when its *category* is named in ``block_on`` (default ``leaked_secret``).
    Severity alone never gates — a non-secret ``blocker`` is surfaced but stays
    advisory (see docs/rc1-114-tuning.md).
    """
    return gating_findings(result.findings, block_on)


# --- output ---------------------------------------------------------------

def _location(f: Finding) -> str:
    if not f.file:
        return "(no file)"
    return f"{f.file}:{f.line}" if f.line else f.file


def format_review(result: ReviewResult, pr: PullRequest | None = None) -> str:
    """Render a :class:`ReviewResult` as readable terminal text."""
    lines: list[str] = []
    if pr is not None:
        header = f"Review of {pr.slug} — {pr.title}".rstrip()
        lines.append(header.rstrip("—").rstrip())
        if pr.html_url:
            lines.append(pr.html_url)
        lines.append("=" * 72)

    lines.append("Summary")
    lines.append(result.summary.strip() or "(no summary provided)")
    lines.append("")

    findings = result.sorted_findings
    if not findings:
        lines.append("No findings — looks clean.")
    else:
        lines.append(f"Findings ({len(findings)})")
        for f in findings:
            lines.append(
                f"  [{_SEVERITY_LABEL.get(f.severity, f.severity.upper())}] "
                f"{_location(f)}  ({f.category})"
            )
            lines.append(f"      {f.message.strip()}")
            if f.suggestion:
                lines.append(f"      fix: {f.suggestion.strip()}")
            lines.append("")

    blockers = sum(1 for f in findings if f.severity == "blocker")
    warnings = sum(1 for f in findings if f.severity == "warning")
    nits = sum(1 for f in findings if f.severity == "nit")
    lines.append(f"Totals: {blockers} blocker(s), {warnings} warning(s), {nits} nit(s)")

    meta = f"model={result.model or 'n/a'}  reviewers={','.join(result.reviewers_run)}"
    # RC1-393: what Python put in the prefix before the reviewers ran.
    meta += (
        f"  context(conventions={result.conventions_file or 'none'},"
        f" callers={result.callers_found})"
    )
    if result.off_scope_findings or result.deduplicated_findings:
        meta += (
            f"  merged(off_scope={result.off_scope_findings},"
            f" deduplicated={result.deduplicated_findings})"
        )
    # RC1-425: the deterministic checks the pipeline ran, and any that failed.
    if result.checks_run or result.checks_failed:
        meta += f"  checks(run={','.join(result.checks_run) or 'none'}"
        meta += f", findings={result.deterministic_findings}"
        if result.checks_failed:
            meta += f", failed={','.join(result.checks_failed)}"
        meta += ")"
    lines.append(meta)
    return "\n".join(lines)


# --- repo context ---------------------------------------------------------

def _resolve_repository(repo_path: str | None, tmpdirs: list[str]) -> LocalRepository:
    """A LocalRepository over --repo-path, or over an empty temp dir as a fallback."""
    if repo_path:
        root = Path(repo_path).expanduser()
        if not root.is_dir():
            raise GitHubError(f"--repo-path is not a directory: {repo_path}")
        return LocalRepository(root)
    # No checkout: an empty dir is not explorable, so the router skips the
    # repository context, the checks find nothing to read and the reviewers
    # work from the diff.
    tmp = tempfile.mkdtemp(prefix="pr-review-empty-")
    tmpdirs.append(tmp)
    return LocalRepository(tmp)


# --- entry point ----------------------------------------------------------

def main(
    argv: list[str] | None = None,
    *,
    fetch: FetchFn | None = None,
    review: ReviewFn | None = None,
    out: Any = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout

    try:
        ref = parse_pr_spec(args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    fetch = fetch or _default_fetch
    review = review or _default_review(model=args.model)

    tmpdirs: list[str] = []
    try:
        pr = fetch(ref)
        repository = _resolve_repository(args.repo_path, tmpdirs)
        if args.repo_path is None:
            print(
                "note: no --repo-path; reviewing from the diff only "
                "(file exploration disabled).",
                file=sys.stderr,
            )
        result = review(pr, repository)
    except (GitHubError, RepositoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        for d in tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    print(format_review(result, pr), file=out)

    blocking = blocking_findings(result, settings.block_on)
    if blocking:
        print(
            f"\nVerdict: BLOCK — {len(blocking)} blocking finding(s) "
            f"(block_on={settings.block_on}).",
            file=out,
        )
        return EXIT_BLOCKED
    print("\nVerdict: advisory — no blocking findings.", file=out)
    return EXIT_OK


def _default_fetch(ref: PRRef) -> PullRequest:
    # token=None: the client resolves it — gh CLI first, then GITHUB_TOKEN (RC1-430).
    return fetch_pull_request(ref.owner, ref.repo, ref.number)


def _default_review(*, model: str | None) -> ReviewFn:
    def _review(pr: PullRequest, repository: RepositoryAccess) -> ReviewResult:
        # client=None -> the pipeline lazily builds the Anthropic SDK from settings.
        return review_pull_request(pr, repository, client=None, model=model)

    return _review


if __name__ == "__main__":
    raise SystemExit(main())
