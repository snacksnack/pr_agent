"""Deterministic, non-model checks, and the one place the pipeline runs them (RC1-425).

A check is a pure function of the pull request and a way to read a changed
file: ``run(pr, read_text) -> list[Finding]``. It needs no model, no network
of its own and no configuration; the n8n execution-cost check (RC1-112) is
the one there is. The registry below is what a new check adds itself to.

:func:`run_deterministic_checks` is the stage :mod:`app.agent.pipeline`
runs before any model call. It reads through the review's
:class:`~app.agent.repository.RepositoryAccess` — the checkout for the
dry-run CLI and the corpus, the Contents API at the PR head for the
webhook — so the two paths see the same bytes and the callers hand the
pipeline nothing. Reads ask for the file whole (``MAX_CHECK_FILE_BYTES``):
the contract's default clip is sized for the prefix, and a workflow export
cut at 64 KB is not JSON any more.

Recoverable on every axis: a check that raises is recorded by name as
failed and logged with its traceback, the other checks still run, and the
review goes on without it. Until RC1-425 the webhook and the CLI each ran
the check, each handed its findings to the pipeline as context and each
merged them into the result afterwards — two copies of the ordering and
two places a merge could be missed or doubled.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.agent.checks import n8n
from app.agent.repository import RepositoryAccess
from app.models import Finding, PullRequest

logger = logging.getLogger("app.agent.checks")

# A check reads a changed file whole. The GitHub Contents API stops at about
# 1 MB, so this is also where the local path stops; a workflow export past
# it is not one.
MAX_CHECK_FILE_BYTES = 1_000_000

# Sources one changed file's text by repo-relative path; ``None`` skips it.
ReadText = Callable[[str], "str | None"]


@dataclass(frozen=True)
class Check:
    """One deterministic check: a name for the record and the runner."""

    name: str
    run: Callable[[PullRequest, ReadText], list[Finding]]


#: Every check the pipeline runs, in this order.
CHECKS: tuple[Check, ...] = (Check("n8n", n8n.run_checks),)


@dataclass
class CheckRun:
    """What the checks stage produced: the findings, and which checks
    completed or failed, by name."""

    findings: list[Finding] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def run_deterministic_checks(
    pr: PullRequest,
    repository: RepositoryAccess,
    checks: Sequence[Check] = CHECKS,
) -> CheckRun:
    """Run every check over the PR, reading changed files through ``repository``.

    A check that raises is logged with its traceback and named in
    ``failed``; nothing it returned is kept, the checks after it still run,
    and the caller's review is untouched. Findings keep the checks' order.
    """

    def read(path: str) -> str | None:
        return repository.read_text(path, max_bytes=MAX_CHECK_FILE_BYTES)

    out = CheckRun()
    for check in checks:
        try:
            found = list(check.run(pr, read))
        except Exception:  # noqa: BLE001 — an advisory check never sinks a review
            logger.exception("check_failed name=%s", check.name)
            out.failed.append(check.name)
            continue
        out.ran.append(check.name)
        out.findings.extend(found)
        logger.info("check_done name=%s findings=%d", check.name, len(found))
    return out
