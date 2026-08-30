"""FastAPI webhook receiver (RC1-116).

The front door of the live service: GitHub POSTs every event here, we verify it's
genuinely from GitHub, acknowledge fast, and hand the work to a background task.

Flow (the ack-fast / process-async pattern GitHub requires — deliveries must get
a 2xx inside ~10s or GitHub marks them failed and retries):

    POST /webhook
      -> verify the HMAC signature over the raw body      (reject forgeries)
      -> filter to pull_request opened/synchronize/reopened
      -> normalize into a WebhookEvent
      -> schedule the review on a BackgroundTask, return 202 immediately

Signature verification runs on *every* delivery against the raw bytes (re-encoding
the parsed JSON would change the bytes and break the HMAC). Anything unsigned or
forged is rejected with 401 before we parse or schedule anything.

The background *processor* is injectable so tests run offline; the default
(:func:`process_event`) mints an installation token, ingests the PR, runs the
review loop, and posts the review (RC1-117), with re-push dedup (RC1-118).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_evals import llmobs
from fastapi import BackgroundTasks, FastAPI, Request, Response

from app.config import settings

if TYPE_CHECKING:  # avoid importing these at module load; worker imports lazily
    from app.dedup import DedupStore
    from app.models import Finding

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

# Type of the async worker the endpoint dispatches to.
Processor = Callable[["WebhookEvent"], None]


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
    return WebhookEvent(
        delivery_id=delivery_id,
        action=action,
        owner=owner,
        repo=name,
        number=number,
        head_sha=head_sha,
        installation_id=installation_id if isinstance(installation_id, int) else None,
    )


# --- default background processor ----------------------------------------

def process_event(event: WebhookEvent, *, store: DedupStore | None = None) -> None:
    """Run the review for a delivered PR and post it back (default worker).

    Mints an installation token, ingests the PR, runs the deterministic n8n
    execution-cost check over the PR's changed workflow JSON (sourced at the head
    via the Contents API, since there's no local checkout), runs the agentic
    review loop from the diff alone (the agent's file tools point at an empty dir
    and it reviews the patch) with those findings handed in as already-recorded
    context, then merges them once and posts/refreshes the review via
    :func:`app.posting.post_review` — mirroring the dry-run CLI's pipeline.

    Dedup (RC1-118): skip webhook redeliveries (same delivery id), commits we've
    already reviewed (same head SHA), and *stale* events — if the PR's current
    head has moved past the event's SHA, a newer push will (or already did)
    trigger its own review, so only the latest head is reviewed.

    Exceptions are swallowed and logged — a background task has no client to
    return an error to, and one bad delivery must not take the worker down.
    """
    from app.agent.reviewer import review_pull_request
    from app.agent.tools import RepoTools
    from app.auth import GitHubAppAuth
    from app.dedup import dedup_store
    from app.models import PRRef
    from app.posting import post_review

    store = store if store is not None else dedup_store
    log = _event_logger(event)

    if store.seen_delivery(event.delivery_id):
        log.info("skip_duplicate_delivery")
        return
    if store.already_reviewed(event.slug, event.head_sha):
        log.info("skip_already_reviewed")
        return

    try:
        with GitHubAppAuth() as auth:
            gh = auth.client_for_repo(event.owner, event.repo)
            pr = gh.fetch_pull_request(PRRef(event.owner, event.repo, event.number))
            if pr.head_sha and pr.head_sha != event.head_sha:
                log.info("skip_stale_head current=%s", pr.head_sha[:12])
                return
            # Deterministic checks run first (same as the dry-run CLI): their
            # findings are fed to the loop as already-recorded context so the
            # model builds on them instead of duplicating them, then merged once.
            precomputed = _run_n8n_checks(gh, pr, log)
            with tempfile.TemporaryDirectory(prefix="pr-review-") as empty:
                result = review_pull_request(
                    pr, RepoTools(empty), client=None, precomputed_findings=precomputed
                )
            result.findings.extend(precomputed)
            outcome = post_review(
                gh, pr, result, block_on=settings.block_on, commit_id=event.head_sha
            )
        store.mark_reviewed(event.slug, event.head_sha)
    except Exception:  # noqa: BLE001 — background worker is the last line of defense
        log.exception("review_failed")
        return

    log.info(
        "review_posted findings=%d event=%s summary=%s new_comments=%d dismissed=%d",
        len(result.findings),
        outcome["event"],
        outcome["summary_action"],
        outcome["new_comments"],
        outcome["dismissed"],
    )


# --- deterministic n8n check (live path) ----------------------------------

def _run_n8n_checks(gh: Any, pr: Any, log: logging.LoggerAdapter) -> list[Finding]:
    """Run the n8n execution-cost check over the PR's changed workflow JSON.

    Sources each changed file's contents at the PR head via the Contents API
    (there's no checkout live), then delegates to the shared runner the dry-run
    CLI uses. Best-effort: any failure degrades to "no findings" and is logged,
    never aborting the review the way a raise would.
    """
    from app.agent.checks import n8n

    def _read(filename: str) -> str | None:
        return gh.get_file_text(pr.ref, filename, git_ref=pr.head_sha)

    try:
        return n8n.run_checks(pr, _read)
    except Exception:  # noqa: BLE001 — an advisory side-check must never sink a review
        log.exception("n8n_check_failed")
        return []


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
    processor: Processor | None = None,
) -> FastAPI:
    """Build the webhook FastAPI app.

    ``secret`` and ``processor`` are injectable for tests; in production both
    fall back to the configured webhook secret and the real :func:`process_event`
    worker. Resolving the secret per-request (not captured here) means rotating
    ``GITHUB_WEBHOOK_SECRET`` doesn't require rebuilding the app.
    """
    configure_logging()
    # RC1-322: reviews triggered by webhooks become LLM Obs traces. The image
    # ships agent-evals (one requirements.txt); a no-op without DD_API_KEY.
    llmobs.enable("pr-review-agent", service="webhook")
    app = FastAPI(title="PR Review Agent webhook", version="RC1-116")
    worker = processor or process_event

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def github_webhook(
        request: Request, background_tasks: BackgroundTasks
    ) -> Response:
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

        _event_logger(event).info("accepted")
        background_tasks.add_task(worker, event)
        # 202: accepted for async processing, review not done yet.
        return Response(status_code=202)

    return app


# Module-level app for `uvicorn app.webhook:app` (and Fly.io, RC1-119).
app = create_app()
