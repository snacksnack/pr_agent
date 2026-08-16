"""Planted-defect recall for the review agent (RC1-253).

The harness is [`agent-evals`](https://github.com/snacksnack/agent-evals),
pinned by tag; what lives here is the corpus and the scoring, which are about
this repo. Third consumer of that library, after `launch-planner-agent` and
`tpm-automation-platform`.

    python -m evals --list        # the corpus, free
    python -m evals               # billed — needs ANTHROPIC_API_KEY
    python -m evals --case clean  # one case

Billed, so it stays out of `pytest` and out of CI. `tests/test_evals.py` holds
the free half: per-category coverage of the corpus, and the scorers checked
against hand-built findings in both directions.
"""

from __future__ import annotations

__version__ = "0.1.0"
