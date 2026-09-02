"""Config sanity check (RC1-107).

Run ``python -m app`` to confirm the package imports and configuration loads.
Secret values are never printed — only whether each one is set.
"""
from __future__ import annotations

from app import __version__
from app.config import settings


def _present(value: str | None) -> str:
    return "set" if value else "not set"


def main() -> int:
    print(f"PR Review Agent v{__version__} — config check\n")

    print("Credentials")
    print(f"  ANTHROPIC_API_KEY       {_present(settings.anthropic_api_key)}")
    print(f"  GITHUB_TOKEN            {_present(settings.github_token)}")

    print("\nReview behavior")
    print(f"  review_model           {settings.review_model}")
    print(f"  deep_review_model      {settings.deep_review_model}")
    print(f"  block_on               {settings.block_on}")
    print(f"  max_tool_turns         {settings.max_tool_turns}")
    print(f"  max_files_read         {settings.max_files_read}")
    print(f"  remote_api_budget      {settings.remote_api_budget}")
    print(f"  github_max_attempts    {settings.github_max_attempts}")
    print(f"  log_level              {settings.log_level}")

    print("\nLive GitHub App (used later — RC1-115 / RC1-116)")
    print(f"  GITHUB_APP_ID          {_present(settings.github_app_id)}")
    print(f"  GITHUB_APP_PRIVATE_KEY  {_present(settings.github_app_private_key)}")
    print(f"  GITHUB_WEBHOOK_SECRET   {_present(settings.github_webhook_secret)}")

    print()
    if not settings.anthropic_api_key:
        print("Note: set ANTHROPIC_API_KEY before running a real review (RC1-113).")
    print("Config loaded OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
