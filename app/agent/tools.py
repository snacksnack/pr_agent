"""Repo-exploration tools for the agent (RC1-109).

Defines the tools the model can call to investigate a repository — read_file,
list_dir, grep — each scoped safely to the PR's checkout. Implemented in
RC1-109.
"""
from __future__ import annotations

# TODO(RC1-109): implement read_file / list_dir / grep with safe path handling,
# bounded output, and Anthropic tool schemas.
