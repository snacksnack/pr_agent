"""Normalized internal models for a pull request under review (RC1-108).

These are source-agnostic: the dry-run CLI populates them from the GitHub REST
API via a PAT, and the webhook service (RC1-116) populates the same shapes
using installation-token auth. Everything downstream — the pipeline, rubric,
and checks — consumes these models, not raw GitHub JSON. The pipeline's
output is two objects (RC1-429): the :class:`ReviewResult` that is posted
and the :class:`RunMetrics` that is priced and shipped.
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
    """The review itself: what is posted, printed and gated on (RC1-429).

    Summary and findings, nothing else. Everything about *how* the review
    was produced — tokens, latency, which reviewers ran, what the verifier
    dropped, what the checks found — is :class:`RunMetrics`, and the two
    travel together as a :class:`ReviewOutcome`. Verdict and posting read
    only this class, so a telemetry field can never leak into a review.
    """

    summary: str = ""
    findings: list[Finding] = field(default_factory=list)

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


@dataclass(frozen=True)
class RunMetrics:
    """Execution telemetry of one review (RC1-429): what pricing, Datadog,
    the eval store and the CLI's diagnostics line read. Immutable; the
    pipeline builds it once, after the last stage. It carries no summary
    and no kept finding — ``verifier_dropped`` is the record of what the
    verifier removed, diagnostics rather than review — so it cannot be
    posted by mistake.
    """

    # The review model; ``mode`` names the path — ``multi`` since RC1-422 is
    # the only one, and eval-store rows before it carry ``single``.
    model: str = ""
    mode: str = "multi"
    reviewers_run: tuple[str, ...] = ()
    # Token spend summed across every model call in the review, verifier
    # included (RC1-269); per stage — ``warm_cache``, ``reviewer:<name>``,
    # ``verifier`` — so the cache premise is checkable per call (RC1-390).
    # ``input_tokens`` is the uncached input only; see :class:`TokenUsage`.
    usage: TokenUsage = TokenUsage()
    stage_usage: dict[str, TokenUsage] = field(default_factory=dict)
    # Wall clock per stage (``checks``, ``context``, ``fan_out``,
    # ``verifier``) and of the whole review, set by ``review_pull_request``.
    stage_latency_ms: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    # Findings the model submitted that could not be read (RC1-387), or
    # whose severity was coerced to warning; findings the merge discarded as
    # off-scope or folded into an earlier one; reviewer calls that came
    # back without a usable submit_review (RC1-390).
    malformed_findings: int = 0
    coerced_findings: int = 0
    off_scope_findings: int = 0
    deduplicated_findings: int = 0
    unusable_reviewer_calls: int = 0
    # RC1-393/394: what Python put in the shared prefix before any model ran.
    conventions_file: str | None = None
    callers_found: int = 0
    tests_found: int = 0
    context_complete: bool = False
    # RC1-387: what the verifier pass did. ``verified`` is False when the
    # pass made no call (nothing to verify); the rest are then empty.
    # ``verifier_model`` is the model the pass actually ran on (the review
    # model since RC1-428; eval-store rows from before may differ).
    verified: bool = False
    verifier_dropped: tuple[Finding, ...] = ()
    verifier_downgraded: int = 0
    verifier_usage: TokenUsage = TokenUsage()
    verifier_model: str = ""
    # RC1-425: the deterministic checks — which completed, which raised, and
    # how many findings they contributed to the review.
    checks_run: tuple[str, ...] = ()
    checks_failed: tuple[str, ...] = ()
    deterministic_findings: int = 0


@dataclass(frozen=True)
class ReviewOutcome:
    """What one call of the pipeline returns (RC1-429): the review, and the
    metrics of the run that produced it. Callers publish the one and ship
    the other; nothing downstream needs both."""

    review: ReviewResult
    metrics: RunMetrics
