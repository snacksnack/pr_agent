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
    max_tool_turns: int = 20
    max_files_read: int = 40
    log_level: str = "INFO"

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

    @field_validator("max_tool_turns", "max_files_read")
    @classmethod
    def _must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()


# Shared, import-friendly instance.
settings = get_settings()
