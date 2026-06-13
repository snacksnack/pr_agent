"""Local dry-run CLI (RC1-113).

Runs the full review against a real PR and prints the would-be review to the
terminal. Nothing is posted to GitHub — this is the tool used to tune review
quality before the App and hosting exist.

Pipeline: ingest the PR (RC1-108) -> agentic review loop + rubric (RC1-110 /
RC1-111) -> deterministic n8n execution-cost check (RC1-112, wired behind a
guard until its rules land) -> formatted terminal output. The process exit code
reflects whether a ``block_on`` finding was present, so this is usable in CI
later.

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
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.agent.checks import n8n
from app.agent.reviewer import review_pull_request
from app.agent.tools import RepoTools, ToolError
from app.config import settings
from app.github import GitHubError, fetch_pull_request, parse_pr_spec
from app.models import Finding, PRRef, PullRequest, ReviewResult

# Exit codes (documented for later CI use).
EXIT_OK = 0          # review ran; no blocking finding
EXIT_BLOCKED = 1     # review ran; a block_on finding was present
EXIT_ERROR = 2       # the review could not be produced (ingestion/loop failure)

FetchFn = Callable[[PRRef], PullRequest]
ReviewFn = Callable[[PullRequest, RepoTools], ReviewResult]

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
    """Findings that should gate a merge under the current ``block_on`` policy.

    A finding gates if its category is named in ``block_on`` or it is a
    ``blocker``-severity finding (the rubric reserves 'blocker' for the
    block_on cases, e.g. a committed secret). The final verdict policy is
    formalized in RC1-117; this is the dry-run approximation.
    """
    block = set(block_on)
    return [f for f in result.findings if f.category in block or f.severity == "blocker"]


# --- n8n hook (stub-tolerant until RC1-112 lands) -------------------------

def _coerce_finding(item: Any) -> Finding | None:
    """Normalize whatever the n8n check returns into a :class:`Finding`."""
    if isinstance(item, Finding):
        return item
    if isinstance(item, dict):
        severity, message = item.get("severity"), item.get("message")
        if not severity or not message:
            return None
        line = item.get("line")
        return Finding(
            severity=str(severity),
            category=str(item.get("category") or "n8n"),
            message=str(message),
            file=item.get("file"),
            line=int(line) if isinstance(line, int) else None,
            suggestion=item.get("suggestion"),
        )
    return None


def run_n8n_checks(pr: PullRequest, repo_root: Path | None) -> list[Finding]:
    """Run the deterministic n8n execution-cost check over changed JSON files.

    Guarded on every axis: does nothing without a local checkout, skips files
    that aren't n8n workflows, and treats the not-yet-implemented check
    (``NotImplementedError`` from RC1-112) — and any parse/read error — as
    "nothing to add" rather than failing the whole review.
    """
    if repo_root is None:
        return []
    findings: list[Finding] = []
    for f in pr.files:
        if f.status == "removed" or not f.filename.endswith(".json"):
            continue
        path = repo_root / f.filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # missing locally or not valid JSON — skip
        if not n8n.looks_like_n8n_workflow(data):
            continue
        try:
            raw = n8n.check_workflow(data)
        except NotImplementedError:
            return []  # RC1-112 not built yet — drop the whole step cleanly
        except Exception:
            continue  # a single bad workflow shouldn't sink the review
        for item in raw or []:
            finding = _coerce_finding(item)
            if finding is not None:
                if finding.file is None:
                    finding.file = f.filename
                findings.append(finding)
    return findings


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

    meta = (
        f"model={result.model or 'n/a'}  turns={result.tool_turns}  "
        f"files_read={result.files_read}"
    )
    if result.truncated:
        meta += "  (truncated: hit a turn/file budget)"
    lines.append(meta)
    return "\n".join(lines)


# --- repo context ---------------------------------------------------------

def _resolve_repo_tools(
    repo_path: str | None, tmpdirs: list[str]
) -> tuple[RepoTools, Path | None]:
    """Build RepoTools from --repo-path, or an empty temp dir as a fallback.

    Returns the tools plus the real checkout root (``None`` when we fell back to
    the empty temp dir, so the n8n hook knows there are no real files to read).
    """
    if repo_path:
        root = Path(repo_path).expanduser()
        if not root.is_dir():
            raise GitHubError(f"--repo-path is not a directory: {repo_path}")
        return RepoTools(root), root
    # No checkout: give the agent an empty dir so its tools return graceful
    # "no such file" errors and it reviews from the diff in the seed prompt.
    tmp = tempfile.mkdtemp(prefix="pr-review-empty-")
    tmpdirs.append(tmp)
    return RepoTools(tmp), None


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
        repo_tools, repo_root = _resolve_repo_tools(args.repo_path, tmpdirs)
        if args.repo_path is None:
            print(
                "note: no --repo-path; reviewing from the diff only "
                "(file exploration disabled).",
                file=sys.stderr,
            )
        result = review(pr, repo_tools)
        result.findings.extend(run_n8n_checks(pr, repo_root))
    except (GitHubError, ToolError) as exc:
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
    return fetch_pull_request(
        ref.owner, ref.repo, ref.number, token=settings.github_token
    )


def _default_review(*, model: str | None) -> ReviewFn:
    def _review(pr: PullRequest, repo_tools: RepoTools) -> ReviewResult:
        # client=None -> reviewer lazily builds the Anthropic SDK from settings.
        return review_pull_request(pr, repo_tools, client=None, model=model)

    return _review


if __name__ == "__main__":
    raise SystemExit(main())
