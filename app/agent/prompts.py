"""Review rubric and prompts (RC1-111).

The system prompt and rubric that drive the review across all dimensions
(convention consistency, pythonic-ness, security, tests, deps, error handling,
breaking changes, PR-description drift), plus the structured-output schema for
findings. Implemented in RC1-111.
"""
from __future__ import annotations

# TODO(RC1-111): system prompt, rubric, and a strict findings[] + summary schema.
