"""Tests for the durable job store (RC1-423). Offline: SQLite in ``tmp_path``,
with an injected clock so due times and retention can be moved."""
from __future__ import annotations

import pytest

from app import jobs
from app.jobs import JobStore
from app.webhook import WebhookEvent


class Clock:
    def __init__(self, at: float = 1_000_000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def _event(
    delivery: str = "d-1", *, sha: str = "abc123def4567890", number: int = 42
) -> WebhookEvent:
    return WebhookEvent(delivery, "opened", "octo", "hello", number, sha, 999, author="octocat")


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store(tmp_path, clock):
    return JobStore(tmp_path / "jobs.db", clock=clock)


# --- persisting a delivery -------------------------------------------------------

def test_enqueue_records_the_delivery_as_queued(store, clock):
    job = store.enqueue(_event())
    assert job is not None
    assert (job.state, job.attempts, job.slug, job.head_sha) == (
        jobs.QUEUED, 0, "octo/hello#42", "abc123def4567890"
    )
    assert job.next_attempt_at == clock.at and job.created_at == clock.at
    assert store.counts() == {"queued": 1, "running": 0, "succeeded": 0, "skipped": 0, "failed": 0}


def test_a_redelivered_delivery_id_is_not_queued_twice(store):
    first = store.enqueue(_event("d-1"))
    assert store.enqueue(_event("d-1", sha="ffff")) is None
    assert store.jobs() == [first]


def test_the_store_survives_a_restart(tmp_path, clock):
    path = tmp_path / "jobs.db"
    first = JobStore(path, clock=clock)
    first.enqueue(_event("d-1"))
    first.mark_reviewed("octo/hello#7", "sha7")
    first.close()

    second = JobStore(path, clock=clock)
    assert second.counts()["queued"] == 1
    assert second.enqueue(_event("d-1")) is None, "the delivery id is on disk, not in memory"
    assert second.already_reviewed("octo/hello#7", "sha7")


def test_the_file_is_not_touched_until_first_use(tmp_path):
    path = tmp_path / "later" / "jobs.db"
    JobStore(path)
    assert not path.exists()


# --- claiming and finishing --------------------------------------------------------

def test_claim_takes_the_oldest_due_job_and_marks_it_running(store, clock):
    a = store.enqueue(_event("d-a"))
    store.enqueue(_event("d-b", number=43))
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == a.id
    assert (claimed.state, claimed.attempts) == (jobs.RUNNING, 1)
    assert store.claim_next().delivery_id == "d-b"
    assert store.claim_next() is None


def test_a_job_due_in_the_future_is_not_claimed_until_then(store, clock):
    job = store.enqueue(_event())
    store.claim_next()
    store.retry(job.id, "boom", delay_s=60)
    assert store.claim_next() is None
    assert store.seconds_until_next_due() == 60
    clock.at += 60
    assert store.seconds_until_next_due() == 0
    assert store.claim_next().attempts == 2


def test_nothing_queued_means_no_due_time(store):
    assert store.seconds_until_next_due() is None


@pytest.mark.parametrize(
    "finish,state,field,value",
    [
        (lambda s, j: s.succeed(j), jobs.SUCCEEDED, "last_error", None),
        (lambda s, j: s.skip(j, "stale_head"), jobs.SKIPPED, "reason", "stale_head"),
        (lambda s, j: s.fail(j, "ValueError: bad"), jobs.FAILED, "last_error", "ValueError: bad"),
    ],
    ids=["succeed", "skip", "fail"],
)
def test_finishing_a_job_records_its_state(store, finish, state, field, value):
    job = store.claim_next() if store.enqueue(_event()) else None
    done = finish(store, job.id)
    assert done.state == state and getattr(done, field) == value
    assert store.counts()[state] == 1 and store.counts()["running"] == 0


def test_retry_keeps_the_error_on_the_record(store):
    job = store.enqueue(_event())
    store.claim_next()
    again = store.retry(job.id, "GitHubError: 502", delay_s=5)
    assert (again.state, again.attempts, again.last_error) == (jobs.QUEUED, 1, "GitHubError: 502")


# --- the review's own idempotency key ---------------------------------------------

def test_reviewed_is_tracked_per_pr_and_sha(store):
    assert not store.already_reviewed("octo/hello#42", "sha1")
    store.mark_reviewed("octo/hello#42", "sha1")
    assert store.already_reviewed("octo/hello#42", "sha1")
    assert not store.already_reviewed("octo/hello#42", "sha2")
    assert not store.already_reviewed("octo/hello#43", "sha1")
    store.mark_reviewed("octo/hello#42", "sha1")  # a second mark is fine


# --- startup: recovery and retention -----------------------------------------------

def test_recover_interrupted_requeues_running_jobs_due_now(store, clock):
    job = store.enqueue(_event())
    store.claim_next()  # the process dies here
    clock.at += 10
    recovered = store.recover_interrupted()
    assert [j.id for j in recovered] == [job.id]
    assert recovered[0].state == jobs.QUEUED and recovered[0].attempts == 1
    assert recovered[0].next_attempt_at == clock.at
    assert "interrupted" in recovered[0].last_error
    assert store.recover_interrupted() == []


def test_prune_drops_old_finished_jobs_and_old_reviewed_pairs(store, clock):
    old = store.enqueue(_event("d-old"))
    store.claim_next()
    store.succeed(old.id)
    store.mark_reviewed("octo/hello#42", "old")
    clock.at += jobs.KEEP_FINISHED_JOBS_S + 1
    fresh = store.enqueue(_event("d-new", number=43))
    store.mark_reviewed("octo/hello#43", "new")
    assert store.prune() == (1, 0)
    assert [j.id for j in store.jobs()] == [fresh.id]
    assert store.already_reviewed("octo/hello#42", "old"), "reviewed pairs live longer"
    clock.at += jobs.KEEP_REVIEWED_S
    assert store.prune() == (0, 1)
    assert not store.already_reviewed("octo/hello#42", "old")
    assert store.already_reviewed("octo/hello#43", "new")


def test_a_queued_or_running_job_is_never_pruned(store, clock):
    store.enqueue(_event("d-q"))
    store.enqueue(_event("d-r", number=43))
    store.claim_next()
    clock.at += jobs.KEEP_FINISHED_JOBS_S * 2
    assert store.prune() == (0, 0)


# --- observability -----------------------------------------------------------------

def test_jobs_lists_newest_first_and_filters_by_state(store):
    a = store.enqueue(_event("d-a"))
    b = store.enqueue(_event("d-b", number=43))
    store.claim_next()
    assert [j.id for j in store.jobs()] == [b.id, a.id]
    assert [j.id for j in store.jobs(state=jobs.RUNNING)] == [a.id]
    assert [j.id for j in store.jobs(state=jobs.QUEUED)] == [b.id]
