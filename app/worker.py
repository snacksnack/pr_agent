"""The durable review worker (RC1-423).

Drains the :class:`~app.jobs.JobStore`: claims each due job, runs
:func:`process_job` — mint a token, fetch the PR, hand the pipeline a
repository at the head, post what comes back — and records what became of
it. One asyncio task on the receiver's loop, started by the app's
lifespan; the webhook nudges it after every persisted delivery so a review
starts as fast as the old background task did, and it wakes on its own
when a retry falls due.

Errors are sorted once, here, into two kinds:

* **transient** — the network, a 5xx or a rate limit from GitHub or the
  model API: the job goes back to the queue with a growing delay, up to
  ``job_max_attempts`` claims in all;
* **terminal** — everything else (a 4xx, a bad payload, a bug): the job is
  failed on the first occurrence, with the error on the record.

A job whose process dies mid-run is neither: it is found ``running`` at
the next startup and re-queued by the store, attempts still counted.
Posting is idempotent (RC1-118: one upserted summary, inline comments not
repeated, superseded change requests dismissed), so a job that crashed
after posting but before being marked succeeded posts the same review
again and lands in the same place.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from app.config import settings
from app.github import GitHubError
from app.jobs import Job, JobStore
from app.observability import ship_review_metrics
from app.retry import RETRY_STATUSES

logger = logging.getLogger("app.worker")

# ``None`` for a posted review, else the reason the job was skipped.
JobRunner = Callable[[Job, JobStore], Awaitable["str | None"]]

STALE_HEAD = "stale_head"
ALREADY_REVIEWED = "already_reviewed"


# --- error policy -------------------------------------------------------------

def is_transient(exc: BaseException) -> bool:
    """Worth another attempt later: a transport failure, a server error or
    a rate limit from GitHub (after :mod:`app.retry`'s own attempts) or from
    the model API. A 4xx, a parse error or a bug is not."""
    if isinstance(exc, GitHubError):
        return exc.status is None or exc.status in RETRY_STATUSES
    try:
        import anthropic
    except ImportError:  # pragma: no cover — the SDK is a runtime requirement
        return False
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in RETRY_STATUSES or exc.status_code >= 500
    return False


def retry_delay_s(attempts: int, *, base_s: float) -> float:
    """``base`` after the first attempt, doubling after each later one."""
    return base_s * (2 ** max(0, attempts - 1))


# --- the job -------------------------------------------------------------------

async def process_job(job: Job, store: JobStore) -> str | None:
    """Review the PR a job names and post the review.

    Returns ``None`` when a review was posted, or the reason nothing was:
    ``already_reviewed`` (this head is on file — a redelivery, or a job
    re-queued after it had already posted) or ``stale_head`` (GitHub's
    current head has moved past the job's, and the newer push has its own
    job). Raises on failure; the worker classifies the exception.

    Async (RC1-426): the pipeline is awaited here; the token mint, the PR
    fetch and the posting are synchronous GitHub calls and run in the
    default executor so they never block the receiver.
    """
    from app.agent.github_repository import GitHubRepository
    from app.agent.pipeline import review_pull_request
    from app.auth import GitHubAppAuth
    from app.models import PRRef
    from app.posting import post_review

    log = _job_logger(job)
    if store.already_reviewed(job.slug, job.head_sha):
        log.info("skip_already_reviewed")
        return ALREADY_REVIEWED

    with GitHubAppAuth() as auth:
        gh = await asyncio.to_thread(auth.client_for_repo, job.owner, job.repo)
        pr = await asyncio.to_thread(gh.fetch_pull_request, PRRef(job.owner, job.repo, job.number))
        if pr.head_sha and pr.head_sha != job.head_sha:
            log.info("skip_stale_head current=%s", pr.head_sha[:12])
            return STALE_HEAD
        repository = GitHubRepository(
            gh,
            pr.ref,
            pr.head_sha,
            changed_files=[f.filename for f in pr.files],
            api_budget=settings.remote_api_budget,
        )
        reviewed = await review_pull_request(pr, repository)
        log.info("repository api_calls=%d tree=%s", repository.api_calls, repository.tree_available)
        # RC1-395: one cost point per review, from here only — the dry-run
        # CLI and the eval corpus run the same review function and must not
        # write into the production series.
        ship_review_metrics(reviewed.metrics, repo=f"{job.owner}/{job.repo}")
        outcome = await asyncio.to_thread(
            post_review,
            gh,
            pr,
            reviewed.review,
            block_on=settings.block_on,
            commit_id=job.head_sha,
        )
    store.mark_reviewed(job.slug, job.head_sha)
    log.info(
        "review_posted findings=%d event=%s summary=%s new_comments=%d dismissed=%d",
        len(reviewed.review.findings),
        outcome["event"],
        outcome["summary_action"],
        outcome["new_comments"],
        outcome["dismissed"],
    )
    return None


# --- the loop -------------------------------------------------------------------

class Worker:
    """Runs due jobs one at a time and records their lifecycle."""

    def __init__(
        self,
        store: JobStore,
        runner: JobRunner = process_job,
        *,
        max_attempts: int | None = None,
        retry_base_s: float | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._max_attempts = max_attempts or settings.job_max_attempts
        self._retry_base_s = retry_base_s or settings.job_retry_base_s
        self._wake = asyncio.Event()

    def nudge(self) -> None:
        """A job was just queued: run without waiting for the timer."""
        self._wake.set()

    async def run_due(self) -> int:
        """Run every job that is due now, in order. Returns how many ran."""
        ran = 0
        while (job := self._store.claim_next()) is not None:
            await self.run_one(job)
            ran += 1
        return ran

    async def run_one(self, job: Job) -> Job:
        log = _job_logger(job)
        log.info("job_started attempt=%d/%d", job.attempts, self._max_attempts)
        try:
            skipped = await self._runner(job, self._store)
        except asyncio.CancelledError:
            raise  # shutdown mid-review: the job stays running and is recovered at startup
        except Exception as exc:  # noqa: BLE001 — the worker is the last line of defense
            error = f"{type(exc).__name__}: {exc}"
            if is_transient(exc) and job.attempts < self._max_attempts:
                delay = retry_delay_s(job.attempts, base_s=self._retry_base_s)
                log.warning(
                    "job_retry attempt=%d next_in=%.0fs error=%s", job.attempts, delay, error
                )
                return self._store.retry(job.id, error, delay_s=delay)
            log.error(
                "job_failed attempts=%d transient=%s error=%s",
                job.attempts,
                is_transient(exc),
                error,
                exc_info=True,
            )
            return self._store.fail(job.id, error)
        if skipped is not None:
            log.info("job_skipped reason=%s", skipped)
            return self._store.skip(job.id, skipped)
        log.info("job_succeeded attempts=%d", job.attempts)
        return self._store.succeed(job.id)

    async def run_forever(self) -> None:
        """Drain, then sleep until a nudge or the next retry is due."""
        while True:
            if await self.run_due():
                continue
            delay = self._store.seconds_until_next_due()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)


def _job_logger(job: Job) -> logging.LoggerAdapter:
    """Logger bound to a job's identifying fields (never secrets)."""
    return logging.LoggerAdapter(
        logger,
        {
            "job": job.id,
            "delivery": job.delivery_id,
            "repo": f"{job.owner}/{job.repo}",
            "pr": job.number,
            "action": job.action,
            "head": job.head_sha[:12],
        },
    )


def startup(store: JobStore) -> list[Job]:
    """What the lifespan does before the worker starts: re-queue interrupted
    jobs and drop old history. Returns the recovered jobs."""
    recovered = store.recover_interrupted()
    for job in recovered:
        _job_logger(job).warning("job_recovered attempts=%d", job.attempts)
    jobs, reviewed = store.prune()
    if jobs or reviewed:
        logger.info("jobs_pruned jobs=%d reviewed=%d", jobs, reviewed)
    return recovered

