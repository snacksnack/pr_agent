"""The durable review job store (RC1-423).

One SQLite file — on a Fly volume in production, a temp path in tests —
holds two tables: the **jobs** the webhook persists before it answers
GitHub, and the **reviewed** ``(PR, head SHA)`` pairs the worker records
after a review is posted. Together they replace the in-process background
task and the in-memory dedup store of RC1-116/118: a 202 now means the job
is on disk, and both idempotency keys survive a process or machine restart.

A job's life::

    queued --claim--> running --succeed--> succeeded
                             --skip-----> skipped   (stale head, already reviewed)
                             --retry----> queued    (transient error; next_attempt_at set)
                             --fail-----> failed    (terminal error, or attempts exhausted)

``attempts`` counts claims. A job found ``running`` at startup was
interrupted — the process or the machine went away mid-review — and is put
back to ``queued`` by :meth:`JobStore.recover_interrupted`; the worker's
attempt cap bounds how often that can happen.

Single writer by design: the service is one process on one machine, so the
store is one connection in WAL mode, and ``claim_next`` uses an immediate
transaction so a claim is atomic even if a second worker ever appears. The
connection is shared across threads behind one lock — the worker calls from
the loop thread, FastAPI may serve ``/healthz`` from its threadpool — and
every call is a few microseconds of SQLite, cheaper than an executor hop.
Timestamps are epoch seconds; ``clock`` is injectable so tests can move
time.
"""
from __future__ import annotations

import functools
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the webhook imports this module; the event type is only a hint here
    from app.webhook import WebhookEvent

logger = logging.getLogger("app.jobs")

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
SKIPPED = "skipped"
FAILED = "failed"
STATES = (QUEUED, RUNNING, SUCCEEDED, SKIPPED, FAILED)

# Retention: finished jobs are history, kept a month for the runbook's
# "what happened to delivery X"; reviewed pairs are the idempotency key and
# are kept a quarter, longer than any PR is likely to be re-delivered.
KEEP_FINISHED_JOBS_S = 30 * 24 * 3600
KEEP_REVIEWED_S = 90 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    installation_id INTEGER,
    author TEXT,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (state, next_attempt_at);
CREATE TABLE IF NOT EXISTS reviewed (
    slug TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    reviewed_at REAL NOT NULL,
    PRIMARY KEY (slug, head_sha)
);
"""

def _locked(method):
    """Serialize a store method on the store's lock."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


_COLUMNS = (
    "id, delivery_id, action, owner, repo, number, head_sha, installation_id, author, "
    "state, attempts, next_attempt_at, last_error, reason, created_at, updated_at"
)


@dataclass(frozen=True)
class Job:
    """One persisted review job: the delivery's fields plus its lifecycle."""

    id: int
    delivery_id: str
    action: str
    owner: str
    repo: str
    number: int
    head_sha: str
    installation_id: int | None
    author: str | None
    state: str
    attempts: int
    next_attempt_at: float
    last_error: str | None
    reason: str | None
    created_at: float
    updated_at: float

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


class JobStore:
    """The jobs and reviewed tables behind one SQLite file."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # -- connection --------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        """Open on first use, so importing the app touches no disk."""
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    @_locked
    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(**dict(zip(row.keys(), tuple(row), strict=True)))

    def _get(self, job_id: int) -> Job:
        row = self._db().execute(f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"no job {job_id}")
        return self._job(row)

    def _set(self, job_id: int, **fields: object) -> Job:
        fields["updated_at"] = self._now()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self._db().execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id)
        )
        return self._get(job_id)

    # -- the webhook's side --------------------------------------------------

    @_locked
    def enqueue(self, event: WebhookEvent) -> Job | None:
        """Persist a delivery as a queued job. ``None`` when this delivery id
        is already on file: GitHub redelivered, and the first record stands."""
        now = self._now()
        cursor = self._db().execute(
            "INSERT OR IGNORE INTO jobs (delivery_id, action, owner, repo, number, head_sha, "
            "installation_id, author, state, attempts, next_attempt_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                event.delivery_id, event.action, event.owner, event.repo, event.number,
                event.head_sha, event.installation_id, event.author, QUEUED, now, now, now,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return self._get(cursor.lastrowid)

    # -- the worker's side ---------------------------------------------------

    @_locked
    def claim_next(self) -> Job | None:
        """Atomically take the oldest due job: ``queued`` → ``running``,
        ``attempts`` + 1. ``None`` when nothing is due."""
        db = self._db()
        now = self._now()
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT id FROM jobs WHERE state = ? AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, id LIMIT 1",
                (QUEUED, now),
            ).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            db.execute(
                "UPDATE jobs SET state = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (RUNNING, now, row["id"]),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return self._get(row["id"])

    @_locked
    def succeed(self, job_id: int) -> Job:
        return self._set(job_id, state=SUCCEEDED, last_error=None)

    @_locked
    def skip(self, job_id: int, reason: str) -> Job:
        return self._set(job_id, state=SKIPPED, reason=reason)

    @_locked
    def retry(self, job_id: int, error: str, *, delay_s: float) -> Job:
        """Back to the queue, due after ``delay_s``; the error is kept for
        the record."""
        return self._set(
            job_id, state=QUEUED, last_error=error[:2000], next_attempt_at=self._now() + delay_s
        )

    @_locked
    def fail(self, job_id: int, error: str) -> Job:
        return self._set(job_id, state=FAILED, last_error=error[:2000])

    @_locked
    def seconds_until_next_due(self) -> float | None:
        """How long the worker may sleep: 0 when a job is due now, ``None``
        when nothing is queued at all."""
        row = self._db().execute(
            "SELECT MIN(next_attempt_at) AS due FROM jobs WHERE state = ?", (QUEUED,)
        ).fetchone()
        if row is None or row["due"] is None:
            return None
        return max(0.0, float(row["due"]) - self._now())

    # -- idempotency of the review itself -----------------------------------

    @_locked
    def already_reviewed(self, slug: str, head_sha: str) -> bool:
        row = self._db().execute(
            "SELECT 1 FROM reviewed WHERE slug = ? AND head_sha = ?", (slug, head_sha)
        ).fetchone()
        return row is not None

    @_locked
    def mark_reviewed(self, slug: str, head_sha: str) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO reviewed (slug, head_sha, reviewed_at) VALUES (?, ?, ?)",
            (slug, head_sha, self._now()),
        )

    # -- startup: recovery and retention --------------------------------------

    @_locked
    def recover_interrupted(self) -> list[Job]:
        """Every job still ``running`` is one the last process never finished:
        back to ``queued``, due now. Its attempts stay counted, so a job that
        keeps taking the machine down with it stops after the cap."""
        db = self._db()
        rows = db.execute(f"SELECT {_COLUMNS} FROM jobs WHERE state = ?", (RUNNING,)).fetchall()
        now = self._now()
        for row in rows:
            db.execute(
                "UPDATE jobs SET state = ?, next_attempt_at = ?, updated_at = ?, "
                "last_error = ? WHERE id = ?",
                (QUEUED, now, now, "interrupted: the process stopped mid-review", row["id"]),
            )
        return [self._get(row["id"]) for row in rows]

    @_locked
    def prune(self) -> tuple[int, int]:
        """Drop finished jobs older than a month and reviewed pairs older
        than a quarter. Returns ``(jobs, reviewed)`` removed."""
        db = self._db()
        now = self._now()
        jobs = db.execute(
            "DELETE FROM jobs WHERE state IN (?, ?, ?) AND updated_at < ?",
            (SUCCEEDED, SKIPPED, FAILED, now - KEEP_FINISHED_JOBS_S),
        ).rowcount
        reviewed = db.execute(
            "DELETE FROM reviewed WHERE reviewed_at < ?", (now - KEEP_REVIEWED_S,)
        ).rowcount
        return jobs, reviewed

    # -- observability ---------------------------------------------------------

    @_locked
    def counts(self) -> dict[str, int]:
        """Jobs per state, every state present (zero included), for /healthz."""
        out = dict.fromkeys(STATES, 0)
        for row in self._db().execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
            out[row["state"]] = row["n"]
        return out

    @_locked
    def jobs(self, *, state: str | None = None, limit: int = 50) -> list[Job]:
        """Newest first; the runbook's view of what happened to a delivery."""
        if state is None:
            rows = self._db().execute(
                f"SELECT {_COLUMNS} FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._db().execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE state = ? ORDER BY id DESC LIMIT ?",
                (state, limit),
            )
        return [self._job(row) for row in rows]
