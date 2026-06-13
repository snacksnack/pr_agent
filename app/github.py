"""GitHub PR ingestion (RC1-108).

Fetch a pull request's metadata, changed files, and per-file patches. Used by
the dry-run CLI (PAT auth) and later by the webhook service (installation-token
auth). Implemented in RC1-108.
"""
from __future__ import annotations


def fetch_pull_request(owner: str, repo: str, number: int):
    """Return normalized PR metadata + per-file patches.

    Placeholder — implemented in RC1-108.
    """
    raise NotImplementedError("RC1-108: GitHub PR ingestion not yet implemented")
