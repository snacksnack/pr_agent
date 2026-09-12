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


# Severity ordering for sorting/triage: blocker is most serious.
SEVERITY_ORDER = {"blocker": 0, "warning": 1, "nit": 2}


@dataclass
class Finding:
    """A single review observation produced by the agent (RC1-110)."""

    severity: str  # blocker | warning | nit
    category: str  # e.g. security, convention, pythonic, tests, n8n, ...
    message: str
    file: str | None = None
    line: int | None = None
    suggestion: str | None = None

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 99)


@dataclass(frozen=True)
class TokenUsage:
    """One or more model calls' token counts, as the API reports them.

    ``input_tokens`` is the *uncached* input only — since prompt caching
    (RC1-350) most of a review's context arrives as ``cache_read`` or
    ``cache_creation`` tokens, which the API bills at 0.1x and 1.25x the
    input price and reports separately. Summing only ``input_tokens`` after
    RC1-350 undercounted a review's cost by roughly 2.5x (found by RC1-387's
    baseline run); anything pricing a review must use all four.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def context_tokens(self) -> int:
        """Everything the model read: uncached + cache writes + cache reads."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


@dataclass
class ReviewResult:
    """The structured outcome of a review, ready for downstream formatting."""

    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    # Run metadata.
    model: str = ""
    tool_turns: int = 0
    files_read: int = 0
    # True if the run hit a guardrail (turn or file-read cap) before finishing.
    truncated: bool = False
    # Findings the model submitted that the loop could not read (missing
    # severity or message) and skipped. Counted rather than silently dropped
    # (RC1-387): three corpus misses in a row returned zero findings on a diff
    # with an obvious defect, and the record could not say whether the model
    # found nothing or the loop threw its answer away.
    malformed_findings: int = 0
    # Findings whose severity was not one of blocker/warning/nit and was
    # coerced to warning (RC1-387: the corrected flag-on run returned one
    # with severity "breaking_change" — the tool schema's enum is advisory to
    # the model, not enforced — and the loop would have posted it as-is).
    coerced_findings: int = 0
    # Token spend summed across every model call in the loop, forced
    # submission included, so a caller can price the review (RC1-269). The
    # verifier pass (RC1-387), when it ran, is included in these totals and
    # also broken out in ``verifier_usage`` so the two can be compared.
    # ``input_tokens`` is the uncached input only; see :class:`TokenUsage`.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # RC1-387: what the verifier pass did. ``verified`` is False when the pass
    # did not run (flag off, or nothing to verify); the rest are then empty.
    verified: bool = False
    verifier_dropped: list[Finding] = field(default_factory=list)
    verifier_downgraded: int = 0
    verifier_usage: TokenUsage = field(default_factory=TokenUsage)
    # RC1-390: which path produced the review. Since RC1-422 there is one —
    # ``multi``: context + [scout] + routed reviewers + merge + verifier —
    # and the rest of these fields describe it (``tool_turns`` and
    # ``files_read`` above are the scout's). Eval-store history still carries
    # ``single`` rows from the retired loop.
    mode: str = "multi"
    reviewers_run: list[str] = field(default_factory=list)
    brief: str = ""
    # Token spend per stage — ``scout``, ``warm_cache``, ``reviewer:<name>``,
    # ``verifier`` — so the cache premise (reviewers read the prefix, they do
    # not write it) is checkable per call, not inferred from the total.
    stage_usage: dict[str, TokenUsage] = field(default_factory=dict)
    # Wall clock per stage (``scout``, ``fan_out``, ``verifier``), so the
    # latency claim — three reviewers cost one reviewer's wall clock, not
    # three — is a number in the record.
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    # Findings a reviewer raised outside its categories and the merge
    # discarded; findings the merge folded into an earlier one at the same
    # file, line and category; reviewer calls that came back without a
    # usable submit_review.
    off_scope_findings: int = 0
    deduplicated_findings: int = 0
    unusable_reviewer_calls: int = 0
    # RC1-393: what Python put in the shared prefix before any model ran —
    # the conventions file it found (``None`` when the repo has none) and
    # how many caller rows the grep produced — so a run record can say
    # whether the scout had the cheap context or re-derived it.
    conventions_file: str | None = None
    callers_found: int = 0
    # RC1-394: test rows Python found for the changed paths, and whether the
    # context was complete enough for the router to skip the scout.
    tests_found: int = 0
    context_complete: bool = False
    # RC1-395: the three facts pricing and the per-review metric need that
    # the fields above do not carry. ``verifier_model`` is the model the
    # verifier pass actually ran on (``review_verify_model`` may differ from
    # the review model, and its tokens are priced at its own rate); empty
    # when the pass did not run. ``scout_ran`` is whether the
    # scout made model calls — a skipped scout and a scout that ran both
    # leave a brief, and only the second cost anything. ``latency_ms`` is the
    # wall clock of the whole review, both paths, set by the dispatcher.
    verifier_model: str = ""
    scout_ran: bool = False
    latency_ms: float = 0.0

    @property
    def usage(self) -> TokenUsage:
        """The whole review's token counts, verifier included."""
        return TokenUsage(
            self.input_tokens,
            self.output_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
        )

    @property
    def sorted_findings(self) -> list[Finding]:
        """Findings ordered by severity, then file, then line."""
        return sorted(
            self.findings,
            key=lambda f: (f.severity_rank, f.file or "", f.line or 0),
        )

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocker"]

    @property
    def has_blocking(self) -> bool:
        return any(f.severity == "blocker" for f in self.findings)
