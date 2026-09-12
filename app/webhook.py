"""FastAPI webhook receiver (RC1-116).

The front door of the live service: GitHub POSTs every event here, we verify it's
genuinely from GitHub, acknowledge fast, and hand the work to a background task.

Flow (the ack-fast / process-async pattern GitHub requires — deliveries must get
a 2xx inside ~10s or GitHub marks them failed and retries):

    POST /webhook
      -> verify the HMAC signature over the raw body      (reject forgeries)
      -> filter to pull_request opened/synchronize/reopened
      -> normalize into a WebhookEvent
      -> drop PRs from skipped authors (Dependabot by default)   (cost guardrail)
      -> persist the job in the durable store, return 202 (RC1-423)

Signature verification runs on *every* delivery against the raw bytes (re-encoding
the parsed JSON would change the bytes and break the HMAC). Anything unsigned or
forged is rejected with 401 before we parse or schedule anything.

The endpoint only verifies, parses and persists (RC1-423). A 202 means the
job is in :class:`app.jobs.JobStore` — SQLite on the machine's volume — and
the review itself is run by :class:`app.worker.Worker`, one task on this
loop that the app's lifespan starts and the endpoint nudges after every
persisted delivery. Redeliveries are the same delivery id and are not
queued twice; an already-reviewed head and a stale head are the worker's
to skip. The store and the runner are injectable so tests run offline.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response

from app.config import settings
from app.jobs import JobStore
from app.observability import enable_llm_obs
from app.worker import JobRunner, Worker, process_job, startup

logger = logging.getLogger("app.webhook")


def configure_logging() -> None:
    """Make our ``app.*`` loggers actually emit at the configured level.

    Under uvicorn the root logger defaults to WARNING with no handler for our
    namespace, so the INFO lifecycle lines (accepted / review_posted / dedup
    skips) and the retry WARNINGs would silently vanish — exactly the signal you
    need to see a live review working or failing. We attach one stdout handler to
    the ``app`` logger at ``settings.log_level`` and stop propagation so uvicorn's
    root handler doesn't double-print. Idempotent: safe to call on every app
    build (tests build many).
    """
    app_logger = logging.getLogger("app")
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    app_logger.setLevel(level)
    if not any(getattr(h, "_pr_agent", False) for h in app_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        handler._pr_agent = True  # type: ignore[attr-defined]  # mark as ours (idempotency)
        app_logger.addHandler(handler)
    app_logger.propagate = False

# pull_request actions worth a review. "opened"/"reopened" are new work to look
# at; "synchronize" fires when the head branch gets new commits (a re-push).
# Other actions (labeled, assigned, closed, edited, ...) are ignored.
PROCESSED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

@dataclass(frozen=True)
class WebhookEvent:
    """A pull_request delivery normalized to just what a review job needs."""

    delivery_id: str
    action: str
    owner: str
    repo: str
    number: int
    head_sha: str
    installation_id: int | None
    author: str | None = None  # pull_request.user.login; drives the skip list

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


class WebhookParseError(ValueError):
    """Raised when a pull_request payload is missing fields we require."""


# --- signature verification ----------------------------------------------

def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256`` over the raw body.

    The header looks like ``sha256=<hex>``. Returns ``False`` for a missing,
    malformed, or non-matching signature — never raises on bad input, so callers
    can treat any falsy result as "reject".
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# --- payload parsing ------------------------------------------------------

def parse_pull_request_event(delivery_id: str, payload: dict[str, Any]) -> WebhookEvent:
    """Pull the fields a review needs out of a ``pull_request`` payload."""
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    number = pr.get("number")
    head_sha = (pr.get("head") or {}).get("sha")
    if not (action and owner and name and isinstance(number, int) and head_sha):
        raise WebhookParseError(
            "pull_request payload missing required fields "
            "(action, repository.owner.login, repository.name, "
            "pull_request.number, pull_request.head.sha)"
        )
    installation_id = (payload.get("installation") or {}).get("id")
    author = (pr.get("user") or {}).get("login")
    return WebhookEvent(
        delivery_id=delivery_id,
        action=action,
        owner=owner,
        repo=name,
        number=number,
        head_sha=head_sha,
        installation_id=installation_id if isinstance(installation_id, int) else None,
        author=author if isinstance(author, str) else None,
    )


# --- structured logging ---------------------------------------------------

def _event_logger(event: WebhookEvent) -> logging.LoggerAdapter:
    """Logger bound to a delivery's identifying fields (never secrets)."""
    return logging.LoggerAdapter(
        logger,
        {
            "delivery": event.delivery_id,
            "repo": f"{event.owner}/{event.repo}",
            "pr": event.number,
            "action": event.action,
            "head": event.head_sha[:12],
        },
    )


# --- app factory ----------------------------------------------------------

def create_app(
    *,
    secret: str | None = None,
    store: JobStore | None = None,
    runner: JobRunner | None = None,
) -> FastAPI:
    """Build the webhook FastAPI app.

    ``secret``, ``store`` and ``runner`` are injectable for tests; in
    production they fall back to the configured webhook secret, a
    :class:`JobStore` at ``settings.jobs_db_path`` and :func:`process_job`.
    Resolving the secret per-request (not captured here) means rotating
    ``GITHUB_WEBHOOK_SECRET`` doesn't require rebuilding the app.

    The lifespan (RC1-423) re-queues jobs the last process left running,
    prunes old history, and runs the worker until shutdown; a shutdown
    mid-review cancels the review, leaves the job ``running``, and the next
    startup recovers it.
    """
    configure_logging()
    # RC1-322: reviews triggered by webhooks become LLM Obs traces; a no-op
    # without DD_API_KEY.
    enable_llm_obs("pr-review-agent", service="webhook")
    job_store = store if store is not None else JobStore(settings.jobs_db_path)
    worker = Worker(job_store, runner or process_job)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        recovered = startup(job_store)
        logger.info("worker_started recovered=%d %s", len(recovered), job_store.counts())
        task = asyncio.create_task(worker.run_forever(), name="review-worker")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            job_store.close()

    app = FastAPI(title="PR Review Agent webhook", version="RC1-116", lifespan=lifespan)
    app.state.store = job_store
    app.state.worker = worker

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        # RC1-423: the queue's state rides on the health check, which is also
        # what the wake cron reads.
        return {"status": "ok", "jobs": job_store.counts()}

    @app.post("/webhook")
    async def github_webhook(request: Request) -> Response:
        configured_secret = secret if secret is not None else settings.github_webhook_secret
        if not configured_secret:
            logger.error("webhook_misconfigured: GITHUB_WEBHOOK_SECRET is not set")
            return Response(status_code=500)

        body = await request.body()
        if not verify_signature(
            configured_secret, body, request.headers.get(SIGNATURE_HEADER)
        ):
            # Don't log the body or signature — just that a delivery was rejected.
            logger.warning(
                "signature_rejected delivery=%s event=%s",
                request.headers.get(DELIVERY_HEADER, "?"),
                request.headers.get(EVENT_HEADER, "?"),
            )
            return Response(status_code=401)

        delivery_id = request.headers.get(DELIVERY_HEADER, "unknown")
        event_type = request.headers.get(EVENT_HEADER, "")

        # GitHub sends a one-off "ping" when the hook is created — ack it.
        if event_type == "ping":
            logger.info("ping delivery=%s", delivery_id)
            return Response(status_code=200)
        if event_type != "pull_request":
            logger.info("ignored_event type=%s delivery=%s", event_type, delivery_id)
            return Response(status_code=200)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — malformed JSON from a signed body
            logger.warning("bad_json delivery=%s", delivery_id)
            return Response(status_code=400)

        action = payload.get("action")
        if action not in PROCESSED_ACTIONS:
            logger.info(
                "ignored_action action=%s delivery=%s", action, delivery_id
            )
            return Response(status_code=200)

        try:
            event = parse_pull_request_event(delivery_id, payload)
        except WebhookParseError as exc:
            logger.warning("unparseable_payload delivery=%s: %s", delivery_id, exc)
            return Response(status_code=400)

        # RC1-359: bot-authored PRs (Dependabot by default) are acknowledged but
        # never reviewed. Each review is a billed model call, and enabling
        # Dependabot alerts can open a dozen version-bump PRs at once; the
        # skip runs before persisting so no job is written and no token is minted.
        log = _event_logger(event)
        if event.author in settings.skip_authors:
            log.info("skip_author author=%s", event.author)
            return Response(status_code=200)

        # RC1-423: on disk before the 202. A redelivery carries the delivery id
        # already on file and is acknowledged without a second job.
        job = job_store.enqueue(event)
        if job is None:
            log.info("duplicate_delivery")
            return Response(status_code=202)
        log.info("job_queued id=%d", job.id)
        worker.nudge()
        # 202: accepted for async processing, review not done yet.
        return Response(status_code=202)

    return app


# Module-level app for `uvicorn app.webhook:app` (and Fly.io, RC1-119).
app = create_app()
