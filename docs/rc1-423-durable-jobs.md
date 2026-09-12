# RC1-423 — A review job is on disk before GitHub hears 202

The webhook answered 202 and ran the review on a Starlette background
task, with delivery ids and reviewed heads remembered in process memory
(RC1-116, RC1-118). A crash, a deploy or a Fly auto-stop after the 202
lost the review; a restart forgot what had been reviewed. This story
replaces both with one durable job store and one worker, on the same
single Fly machine, and writes down how it recovers.

- **Ticket:** [RC1-423](https://hirereidcollins.atlassian.net/browse/RC1-423).
- **Related:** RC1-116 (the receiver), RC1-118 (dedup and idempotent
  posting), RC1-119/120 (Fly, go-live), RC1-426 (the async boundary the
  worker sits on).
- **Decision (Reid, 2026-09-12):** option A — keep scale-to-zero, store
  jobs in SQLite on a Fly volume, wake the machine on a schedule so a
  queued job never waits for the next delivery. Not B (a machine always
  running, a few dollars a month for one review a day) and not C (an
  external queue, more infrastructure than one machine warrants).

## The mechanism

| Piece | Where | What it does |
|---|---|---|
| `JobStore` | `app/jobs.py` | SQLite (stdlib) at `JOBS_DB_PATH`, `/data/jobs.db` on the volume. Tables `jobs` (one row per delivery, `delivery_id` unique) and `reviewed` (`(slug, head_sha)` pairs). WAL, one connection behind a lock, an immediate transaction to claim. |
| the endpoint | `app/webhook.py` | verifies, parses, applies the author skip, **persists**, nudges the worker, answers 202. A redelivered delivery id is acknowledged and not queued twice. |
| `Worker` | `app/worker.py` | one asyncio task on the receiver's loop. Claims due jobs oldest first, runs `process_job`, records `succeeded` / `skipped` / `queued`-for-retry / `failed`. Sleeps until a nudge or the next retry is due. |
| `process_job` | `app/worker.py` | the old worker body: skip if the head is already reviewed, mint, fetch, skip if GitHub's head moved, review, ship the cost point, post, then mark the head reviewed. |
| the lifespan | `app/webhook.py` | at startup re-queues jobs left `running`, prunes history, starts the worker; at shutdown cancels the worker and closes the store. |
| the wake cron | `.github/workflows/wake.yml` | `GET /healthz` every 15 minutes: starts a stopped machine, whose startup drains the queue; the queue counts land in the Actions log. |

States: `queued → running → succeeded | skipped | failed`, and `running →
queued` on a transient error (with a delay) or on recovery. `attempts`
counts claims; `job_max_attempts` (3) bounds them; `job_retry_base_s`
(60) doubles after each transient failure.

**Errors are sorted once** (`worker.is_transient`): a GitHub transport
failure or 429/5xx after `app.retry`'s own attempts, an Anthropic
connection error, or an Anthropic 429/5xx is transient and retried; a 4xx,
a parse error or a bug is terminal and fails on the first occurrence, with
the traceback in the log. A job that stops the process mid-run is neither:
recovery re-queues it with its attempts still counted, so a job that keeps
taking the machine down stops at the cap.

## Recovery, by where it failed

| Failure | What happens |
|---|---|
| before the 202 | GitHub gets no 2xx and redelivers; the endpoint persists it then. Nothing to recover. |
| after the 202, before the worker claims | the job is `queued` on disk; the next startup (a delivery or the wake cron) runs it. |
| mid-review (crash, deploy, auto-stop) | the job is `running` on disk; startup re-queues it (`job_recovered` in the log) and it runs again from the top. The pipeline's owned client is closed by its `finally` on cancellation. |
| after posting, before `mark_reviewed` | the re-run posts the same review again, and posting is idempotent (RC1-118): the summary comment is edited in place by its marker, inline comments already present are not repeated, our superseded change requests are dismissed. Then the head is marked. |
| a newer push arrived while queued | `process_job` fetches the PR, sees a newer head and records `skipped: stale_head`; the newer push has its own job. |
| the same delivery arrives twice | the endpoint answers 202 and logs `duplicate_delivery`; the first row stands. |
| the same head arrives under a new delivery | the worker records `skipped: already_reviewed`; no model call. |

## Operations

**Storage.** One volume, `pr_review_jobs`, 1 GB, region `iad`, mounted at
`/data`; the machine is the only reader and writer. Create it once,
before the first deploy that mounts it (the deploy fails without it):

```bash
fly volumes create pr_review_jobs --region iad --size 1 -a pr-review-agent-snacksnack
```

A volume belongs to one machine in one region. If the machine is ever
recreated, attach the same volume; if the volume is lost, so is the queue
and the reviewed history — the consequence is at most one redundant
review per already-reviewed head, never a wrong one.

**Retention.** At every startup: finished jobs (`succeeded`, `skipped`,
`failed`) older than 30 days and reviewed pairs older than 90 days are
deleted (`jobs_pruned`). Queued and running jobs are never pruned.

**Auto-stop and cold starts.** The machine still scales to zero. Fly
stops it a few minutes after the last request, with no regard for a
background review — a long review can be interrupted, which is the
mid-review case above. The wake cron guarantees the interrupted job runs
within 15 minutes; a delivery wakes it sooner. A cold start costs about 5
seconds before the endpoint answers, as before.

**Watching it.** `GET /healthz` returns `{"status": "ok", "jobs":
{"queued": n, "running": n, "succeeded": n, "skipped": n, "failed": n}}`.
The log carries one line per transition — `job_queued`, `job_started`,
`job_succeeded`, `job_skipped reason=`, `job_retry attempt= next_in=`,
`job_failed attempts= transient=`, `job_recovered`, `jobs_pruned` — each
bound to the job id, delivery id, repo, PR and head. A `failed` job is a
human's to look at: `fly ssh console` then
`sqlite3 /data/jobs.db "select id, delivery_id, owner, repo, number, attempts, last_error from jobs where state='failed'"`.
To run one again, set its state back to `queued` and `next_attempt_at` to
now; the next startup or nudge picks it up.

**Migration.** Nothing to migrate: the in-memory store held nothing
durable. `app/dedup.py`, its tests, the `BackgroundTasks` dispatch and
`process_event` are gone; `process_job` is their one successor.

## Evidence

Offline: the suite is green with coverage at 95 % against the 88 % floor.
`tests/test_jobs.py` covers persisting, the redelivery key, survival across
a close-and-reopen, claiming in order and by due time, every transition,
recovery of a running job, retention and the listing. `tests/test_worker.py`
covers the error policy table, retry-then-fail with the doubling delay, a
terminal error failing on the first attempt, the loop waking on a nudge
and on a due retry, **restart tests** — a job interrupted mid-review
completed by the next process, a job that keeps crashing the process
stopping at the cap, a cancelled run left for recovery, a crash between
posting and marking posting again through the upsert — and `process_job`
with its GitHub, pipeline and posting collaborators faked: ingest, review,
post, mark; already-reviewed and stale-head skips without a model call;
the cost point. `tests/test_webhook.py` proves the job is on disk before
the 202, a redelivery makes no second job, `/healthz` reports the queue,
and under the lifespan a posted delivery is run by the worker and a job
the last process left running is recovered and completed.

No model call changed, so no corpus run was owed.

## Left open

- **Live check after deploy.** Create the volume, merge, then: the first
  delivery logs `job_queued` → `job_started` → the usual pipeline lines →
  `job_succeeded`; `/healthz` shows the counts; the Wake workflow's first
  run prints them. A deliberate interruption test (stop the machine during
  a review, then wake it) is worth one run.
- The counts are on `/healthz` and in the log, not in Datadog. A
  `pr_agent.jobs.*` gauge from the wake cron or the worker is a small
  follow-up if the dashboard wants it.
