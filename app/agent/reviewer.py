"""Agentic review loop (RC1-110).

Orchestrates the model + repo-exploration tools: seeds the loop with the PR diff
and metadata, lets the model explore across turns (within tool-turn / file-read
caps), and collects structured findings. Implemented in RC1-110.
"""
from __future__ import annotations


def review_pull_request(pull_request) -> object:
    """Run the agent loop over a PR and return structured findings.

    Placeholder — implemented in RC1-110.
    """
    raise NotImplementedError("RC1-110: agentic review loop not yet implemented")
