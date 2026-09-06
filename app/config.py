"""Application configuration (RC1-107).

Loads settings from environment variables (and an optional ``.env`` file) into a
single typed ``Settings`` object. Import the shared instance anywhere:

    from app.config import settings

Credentials are optional so the skeleton imports cleanly before a ``.env`` is
configured; the dry-run CLI (RC1-113) checks for what it needs at call time.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, sourced from env / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Credentials (optional; required only when actually reviewing) ---
    anthropic_api_key: str | None = None
    github_token: str | None = None  # PAT for the dry-run CLI (RC1-113)

    # --- Review behavior ---
    review_model: str = "claude-sonnet-4-6"
    deep_review_model: str = "claude-opus-4-6"
    review_block_on: str = "leaked_secret"  # CSV; see ``block_on`` below
    # PR authors whose deliveries are acknowledged but never reviewed (CSV of
    # GitHub logins; see ``skip_authors``). Dependabot's version bumps arrive in
    # bursts once alerts are enabled and carry nothing for a reviewer to judge,
    # so they stay off the billed review path (RC1-359).
    review_skip_authors: str = "dependabot[bot]"
    max_tool_turns: int = 20
    max_files_read: int = 40
    # RC1-387: second pass that re-reads each finding against the diff before
    # it is posted, and may drop or downgrade it. Off by default; the corpus
    # is run with it both ways and the ADR (docs/rc1-387-verifier.md) says
    # what the numbers were. Never adds findings, never raises severity.
    review_verify_findings: bool = False
    # Model for the verifier pass; unset means the same model as the review.
    review_verify_model: str | None = None
    # RC1-390: split the review into a scout, three evidence-scoped reviewers
    # fanned out on one shared cached prefix, a Python merge, and the verifier.
    # Off by default: off is the single loop above, byte for byte. The corpus
    # is run both ways and docs/rc1-390-multi-agent.md records the numbers.
    review_multi_agent: bool = False
    # Turn cap for the scout (RC1-390). It writes a brief, not findings, so it
    # needs fewer turns than the single loop; every turn re-sends the growing
    # conversation, so the cap is the scout's cost ceiling.
    review_scout_max_turns: int = 8
    # RC1-393: the scout's turn cap when Python has already put the
    # conventions file and the callers list in front of it. Measured: with
    # the full cap the scout spends every turn regardless of what it was
    # handed, so the context only makes exploration cheaper if the budget
    # shrinks with it. What is left for the scout is tests for the changed
    # paths and whatever the callers list did not reach. Zero skips the scout
    # altogether when the context is complete: exploration is then Python's
    # alone, and the reviewers read the conventions file and the callers list
    # with no brief.
    review_scout_context_turns: int = 3
    # RC1-394: the scout's turn cap when the context is complete — the
    # conventions file found, the callers search finished and the tests for
    # the changed paths found by Python too. Zero, the default, skips the
    # scout: exploration is then Python's alone and the review is one prefix
    # write plus four cached reads. Set it to the context cap to measure
    # what a scout still adds on top of a complete context.
    review_scout_complete_turns: int = 0
    # Live reviews read the repo through the GitHub API (RC1-364); this caps
    # the Contents/Trees calls one review may spend so a curious model cannot
    # page through a large repository.
    remote_api_budget: int = 60
    log_level: str = "INFO"
    # Total GitHub API attempts per request before giving up (1 = no retry).
    # Transient failures (5xx / rate limit / dropped connection) back off
    # between attempts; see ``app.retry`` (RC1-120).
    github_max_attempts: int = 4

    # --- Live GitHub App (RC1-115 / RC1-116); unset during the dry-run phase ---
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_webhook_secret: str | None = None

    @property
    def block_on(self) -> list[str]:
        """Finding categories that escalate the verdict to 'Request changes'.

        Parsed from the comma-separated ``REVIEW_BLOCK_ON`` env var. An empty
        value means the reviewer is purely advisory and never blocks a merge.
        """
        return [item.strip() for item in self.review_block_on.split(",") if item.strip()]

    @property
    def skip_authors(self) -> list[str]:
        """PR author logins the webhook acknowledges without dispatching a review.

        Parsed from the comma-separated ``REVIEW_SKIP_AUTHORS`` env var. An empty
        value means every PR the App sees is reviewed.
        """
        return [item.strip() for item in self.review_skip_authors.split(",") if item.strip()]

    @field_validator(
        "max_tool_turns",
        "max_files_read",
        "remote_api_budget",
        "github_max_attempts",
        "review_scout_max_turns",
    )
    @classmethod
    def _must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v

    @field_validator("review_scout_context_turns", "review_scout_complete_turns")
    @classmethod
    def _must_not_be_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be zero or a positive integer")
        return v


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()


# Shared, import-friendly instance.
settings = get_settings()
