"""Tests for the durable review worker (RC1-423). Offline: the store is SQLite
in ``tmp_path``; the job runner is a fake, or the real ``process_job`` with its
GitHub, pipeline and posting collaborators monkeypatched as the webhook tests
always did. The worker is async (RC1-426); these sync tests drive it under
``asyncio.run``."""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app import jobs, worker
from app.github import GitHubError
from app.jobs import JobStore
from app.webhook import WebhookEvent
from app.worker import Worker, process_job


class Clock:
    def __init__(self, at: float = 1_000_000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def _event(delivery: str = "d-1", *, sha: str = "abc123def4567890") -> WebhookEvent:
    return WebhookEvent(delivery, "opened", "octo", "hello", 42, sha, 999, author="octocat")


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store(tmp_path, clock):
    return JobStore(tmp_path / "jobs.db", clock=clock)


def _run(coro):
    return asyncio.run(coro)


# --- the error policy ------------------------------------------------------------

def _status_error(status: int):
    import anthropic

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError("x", response=response, body=None)


@pytest.mark.parametrize(
    "exc,transient",
    [
        (GitHubError("dropped", status=None), True),
        (GitHubError("rate limited", status=429), True),
        (GitHubError("bad gateway", status=502), True),
        (GitHubError("not found", status=404), False),
        (GitHubError("forbidden", status=403), False),
        (_status_error(529), True),
        (_status_error(500), True),
        (_status_error(429), True),
        (_status_error(400), False),
        (_status_error(401), False),
        (ValueError("bug"), False),
        (KeyError("bug"), False),
    ],
    ids=[
        "gh-transport", "gh-429", "gh-502", "gh-404", "gh-403",
        "api-529", "api-500", "api-429", "api-400", "api-401", "value", "key",
    ],
)
def test_is_transient_sorts_errors(exc, transient):
    assert worker.is_transient(exc) is transient


def test_api_connection_errors_are_transient():
    import anthropic

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    assert worker.is_transient(anthropic.APIConnectionError(request=request))
    assert worker.is_transient(anthropic.APITimeoutError(request=request))


def test_retry_delay_doubles_after_each_attempt():
    assert [worker.retry_delay_s(n, base_s=60) for n in (1, 2, 3)] == [60, 120, 240]


# --- the loop over a fake runner ---------------------------------------------------

def _worker(store, runner, **kw):
    kw.setdefault("max_attempts", 3)
    kw.setdefault("retry_base_s", 60)
    return Worker(store, runner, **kw)


def test_a_successful_run_marks_the_job_succeeded(store):
    seen = []

    async def runner(job, s):
        seen.append(job.delivery_id)
        return None

    store.enqueue(_event())
    assert _run(_worker(store, runner).run_due()) == 1
    assert seen == ["d-1"]
    assert store.jobs()[0].state == jobs.SUCCEEDED


def test_a_skipped_run_records_the_reason(store):
    async def runner(job, s):
        return worker.STALE_HEAD

    store.enqueue(_event())
    _run(_worker(store, runner).run_due())
    job = store.jobs()[0]
    assert (job.state, job.reason) == (jobs.SKIPPED, "stale_head")


def test_a_transient_error_retries_with_a_growing_delay_then_fails(store, clock, caplog):
    async def runner(job, s):
        raise GitHubError("bad gateway", status=502)

    store.enqueue(_event())
    w = _worker(store, runner)
    with caplog.at_level(logging.INFO, logger="app.worker"):
        _run(w.run_due())
        job = store.jobs()[0]
        assert (job.state, job.attempts) == (jobs.QUEUED, 1)
        assert store.seconds_until_next_due() == 60
        assert "job_retry attempt=1 next_in=60s" in caplog.text

        clock.at += 60
        _run(w.run_due())
        assert store.jobs()[0].attempts == 2 and store.seconds_until_next_due() == 120

        clock.at += 120
        _run(w.run_due())
    job = store.jobs()[0]
    assert (job.state, job.attempts) == (jobs.FAILED, 3)
    assert "bad gateway" in job.last_error
    assert "job_failed attempts=3 transient=True" in caplog.text


def test_a_terminal_error_fails_on_the_first_attempt(store, caplog):
    async def runner(job, s):
        raise ValueError("unparseable")

    store.enqueue(_event())
    with caplog.at_level(logging.INFO, logger="app.worker"):
        _run(_worker(store, runner).run_due())
    job = store.jobs()[0]
    assert (job.state, job.attempts) == (jobs.FAILED, 1)
    assert job.last_error == "ValueError: unparseable"
    assert "job_failed attempts=1 transient=False" in caplog.text
    assert "Traceback" in caplog.text, "the traceback is logged, not swallowed"


def test_run_due_runs_every_due_job_in_order_and_stops(store):
    order = []

    async def runner(job, s):
        order.append(job.delivery_id)
        return None

    for d in ("d-1", "d-2", "d-3"):
        store.enqueue(_event(d))
    assert _run(_worker(store, runner).run_due()) == 3
    assert order == ["d-1", "d-2", "d-3"]
    assert _run(_worker(store, runner).run_due()) == 0


def test_run_forever_wakes_on_a_nudge_and_on_a_due_retry(store, clock):
    calls = []

    async def runner(job, s):
        calls.append(job.attempts)
        if len(calls) == 1:
            raise GitHubError("dropped", status=None)
        return None

    async def scenario():
        w = _worker(store, runner, retry_base_s=0.05)
        task = asyncio.create_task(w.run_forever())
        await asyncio.sleep(0.01)  # idle: nothing queued
        store.enqueue(_event())
        w.nudge()
        await asyncio.sleep(0.02)
        assert store.jobs()[0].state == jobs.QUEUED and calls == [1], "retry scheduled"
        clock.at += 0.05  # the store's clock decides what is due; the loop's timer wakes it
        await asyncio.sleep(0.1)
        assert calls == [1, 2] and store.jobs()[0].state == jobs.SUCCEEDED
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _run(scenario())


# --- restart / crash recovery --------------------------------------------------------

def test_an_acknowledged_job_interrupted_mid_review_is_completed_after_restart(tmp_path, clock):
    """The process dies with the job running (a stop, a crash). The next
    process finds it, re-queues it and completes it — nothing was lost at
    the 202."""
    path = tmp_path / "jobs.db"
    first = JobStore(path, clock=clock)
    first.enqueue(_event())
    first.claim_next()  # running; the process dies here
    first.close()

    second = JobStore(path, clock=clock)
    recovered = worker.startup(second)
    assert [j.attempts for j in recovered] == [1]

    async def runner(job, s):
        return None

    _run(_worker(second, runner).run_due())
    job = second.jobs()[0]
    assert (job.state, job.attempts) == (jobs.SUCCEEDED, 2)


def test_a_job_that_keeps_taking_the_process_down_stops_at_the_cap(tmp_path, clock):
    path = tmp_path / "jobs.db"
    store = JobStore(path, clock=clock)
    store.enqueue(_event())
    for _ in range(3):  # three crashes mid-run
        worker.startup(store)
        assert store.claim_next() is not None
    worker.startup(store)
    job = store.jobs()[0]
    assert (job.state, job.attempts) == (jobs.QUEUED, 3)

    async def runner(job, s):
        raise GitHubError("dropped", status=None)

    _run(_worker(store, runner, max_attempts=3).run_due())
    assert store.jobs()[0].state == jobs.FAILED, "no fourth attempt"


def test_a_cancelled_run_leaves_the_job_running_for_recovery(store):
    async def runner(job, s):
        await asyncio.Event().wait()

    async def scenario():
        store.enqueue(_event())
        w = _worker(store, runner)
        task = asyncio.create_task(w.run_due())
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _run(scenario())
    assert store.jobs()[0].state == jobs.RUNNING
    assert [j.state for j in worker.startup(store)] == [jobs.QUEUED]


# --- process_job: ingest -> review -> post ------------------------------------------

def _wire_fakes(monkeypatch, pr, posted, *, review=None, get_file_text=None):
    """Stub the lazily-imported collaborators of process_job."""
    import app.agent.pipeline
    import app.auth
    import app.posting
    from app.models import ReviewOutcome, ReviewResult, RunMetrics

    class FakeClient:
        def fetch_pull_request(self, ref):
            return pr

        def get_file_text(self, ref, path, *, git_ref=None):
            return get_file_text(path) if get_file_text else None

    class FakeAuth:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def client_for_repo(self, owner, repo):
            return FakeClient()

    async def fake_review(pull_request, repository, client=None):
        posted["reviewed"] = posted.get("reviewed", 0) + 1
        posted["repository"] = repository
        return ReviewOutcome(ReviewResult(summary="ok"), RunMetrics(model="m"))

    def fake_post(client, pull_request, result, *, block_on, commit_id=None):
        posted.update(pr=pull_request, result=result, block_on=block_on, commit_id=commit_id)
        posted["posts"] = posted.get("posts", 0) + 1
        return {"summary_action": "created", "new_comments": 0, "dismissed": 0,
                "review_id": 5, "event": "COMMENT"}

    monkeypatch.setattr(app.auth, "GitHubAppAuth", FakeAuth)
    monkeypatch.setattr(app.agent.pipeline, "review_pull_request", review or fake_review)
    monkeypatch.setattr(app.posting, "post_review", fake_post)


def _pr(head="abc123def4567890"):
    from app.models import PRRef, PullRequest

    return PullRequest(ref=PRRef("octo", "hello", 42), title="T", head_sha=head)


def _job(store):
    store.enqueue(_event())
    return store.claim_next()


def test_process_job_ingests_reviews_posts_and_marks_the_head_reviewed(monkeypatch, store):
    from app.agent.github_repository import GitHubRepository

    posted: dict = {}
    _wire_fakes(monkeypatch, _pr(), posted)
    assert _run(process_job(_job(store), store)) is None
    assert posted["pr"].head_sha == "abc123def4567890"
    assert posted["commit_id"] == "abc123def4567890"
    assert "leaked_secret" in posted["block_on"]
    assert posted["result"].summary == "ok"
    # RC1-364: the live agent explores the repo through the API at the PR head.
    assert isinstance(posted["repository"], GitHubRepository)
    assert posted["repository"].api_calls == 0  # nothing spent until the pipeline reads
    assert store.already_reviewed("octo/hello#42", "abc123def4567890")


def test_process_job_skips_a_head_already_reviewed_without_a_model_run(monkeypatch, store):
    posted: dict = {}
    _wire_fakes(monkeypatch, _pr(), posted)
    store.mark_reviewed("octo/hello#42", "abc123def4567890")
    assert _run(process_job(_job(store), store)) == worker.ALREADY_REVIEWED
    assert "reviewed" not in posted and "posts" not in posted


def test_process_job_skips_a_stale_head_without_a_model_run(monkeypatch, store):
    posted: dict = {}
    _wire_fakes(monkeypatch, _pr(head="fedcba9876543210"), posted)  # GitHub has moved on
    assert _run(process_job(_job(store), store)) == worker.STALE_HEAD
    assert "reviewed" not in posted
    assert not store.already_reviewed("octo/hello#42", "abc123def4567890")


def test_a_crash_between_posting_and_marking_posts_again_through_the_upsert(
    monkeypatch, store, clock
):
    """Posting is idempotent (RC1-118): the re-run edits the same summary
    comment and skips inline comments already there, so the second post is
    the same review in the same place — and this time the head is marked."""
    posted: dict = {}
    _wire_fakes(monkeypatch, _pr(), posted)
    original = store.mark_reviewed
    calls = []

    def dies_once(slug, sha):
        calls.append(sha)
        if len(calls) == 1:
            raise RuntimeError("machine stopped")
        original(slug, sha)

    monkeypatch.setattr(store, "mark_reviewed", dies_once)
    job = _job(store)
    with pytest.raises(RuntimeError):
        _run(process_job(job, store))
    assert posted["posts"] == 1 and not store.already_reviewed(job.slug, job.head_sha)

    recovered = worker.startup(store)
    _run(process_job(recovered[0], store))
    assert posted["posts"] == 2 and store.already_reviewed(job.slug, job.head_sha)


def test_process_job_ships_one_cost_point_per_review(monkeypatch, store):
    """RC1-395: the metric leaves from the worker only, tagged with the repo."""
    posted: dict = {}
    _wire_fakes(monkeypatch, _pr(), posted)
    shipped = []
    monkeypatch.setattr(
        worker, "ship_review_metrics", lambda metrics, *, repo: shipped.append((metrics, repo))
    )
    _run(process_job(_job(store), store))
    assert len(shipped) == 1
    metrics, repo = shipped[0]
    assert repo == "octo/hello" and metrics.model == "m", "the run's metrics, not the review"


def test_the_worker_runs_process_job_end_to_end(monkeypatch, store):
    posted: dict = {}
    _wire_fakes(monkeypatch, _pr(), posted)
    store.enqueue(_event())
    _run(Worker(store).run_due())
    assert store.jobs()[0].state == jobs.SUCCEEDED and posted["posts"] == 1
