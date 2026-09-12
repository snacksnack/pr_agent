"""Tests for the FastAPI webhook receiver (RC1-116).

Fully offline: the review worker is replaced with a recorder, signatures are
computed with a known test secret, and requests go through Starlette's
``TestClient`` (which runs background tasks synchronously after the response, so
we can assert dispatch right after the call returns).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.webhook import (
    WebhookEvent,
    WebhookParseError,
    configure_logging,
    create_app,
    parse_pull_request_event,
    process_event,
    verify_signature,
)

SECRET = "s3cr3t-webhook-key"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload(action: str = "opened", *, author: str | None = "octocat") -> dict:
    pull_request: dict = {"number": 42, "head": {"sha": "abc123def4567890"}}
    if author is not None:
        pull_request["user"] = {"login": author}
    return {
        "action": action,
        "number": 42,
        "pull_request": pull_request,
        "repository": {"name": "hello", "owner": {"login": "octo"}},
        "installation": {"id": 999},
    }


def _post(client: TestClient, payload: dict, *, event: str = "pull_request",
          secret: str = SECRET, delivery: str = "d-1", sign: bool = True):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return client.post("/webhook", content=body, headers=headers)


class Recorder:
    """Stand-in background worker that records the events it's handed."""

    def __init__(self) -> None:
        self.events: list[WebhookEvent] = []

    def __call__(self, event: WebhookEvent) -> None:
        self.events.append(event)


def _client(recorder: Recorder | None = None, *, secret: str | None = SECRET) -> TestClient:
    return TestClient(create_app(secret=secret, processor=recorder or Recorder()))


# --- verify_signature -----------------------------------------------------

def test_verify_signature_accepts_valid():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, _sign(body)) is True


def test_verify_signature_rejects_wrong_secret_and_tamper():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, _sign(body, "other")) is False
    assert verify_signature(SECRET, b'{"a":2}', _sign(body)) is False


@pytest.mark.parametrize("header", [None, "", "deadbeef", "sha1=abc", "sha256="])
def test_verify_signature_rejects_missing_or_malformed(header):
    assert verify_signature(SECRET, b"x", header) is False


# --- parse_pull_request_event ---------------------------------------------

def test_parse_pull_request_event_extracts_fields():
    event = parse_pull_request_event("d-9", _pr_payload("synchronize"))
    assert (event.owner, event.repo, event.number) == ("octo", "hello", 42)
    assert event.action == "synchronize"
    assert event.head_sha == "abc123def4567890"
    assert event.installation_id == 999
    assert event.author == "octocat"
    assert event.slug == "octo/hello#42"


def test_parse_pull_request_event_tolerates_missing_author():
    assert parse_pull_request_event("d-1", _pr_payload(author=None)).author is None


def test_parse_pull_request_event_missing_fields_raises():
    bad = _pr_payload()
    bad["pull_request"]["head"] = {}  # drop the head sha
    with pytest.raises(WebhookParseError):
        parse_pull_request_event("d-1", bad)


def test_parse_pull_request_event_tolerates_missing_installation():
    payload = _pr_payload()
    del payload["installation"]
    assert parse_pull_request_event("d-1", payload).installation_id is None


# --- endpoint: happy path + dispatch --------------------------------------

@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_processed_actions_ack_202_and_dispatch(action):
    rec = Recorder()
    resp = _post(_client(rec), _pr_payload(action))
    assert resp.status_code == 202
    assert len(rec.events) == 1
    assert rec.events[0].action == action
    assert rec.events[0].slug == "octo/hello#42"


def test_ignored_action_acks_200_without_dispatch():
    rec = Recorder()
    resp = _post(_client(rec), _pr_payload("closed"))
    assert resp.status_code == 200
    assert rec.events == []


# --- endpoint: skipped authors (RC1-359 cost guardrail) -------------------

@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_dependabot_pr_acks_200_without_dispatch(action, caplog):
    rec = Recorder()
    with caplog.at_level(logging.INFO, logger="app.webhook"):
        resp = _post(_client(rec), _pr_payload(action, author="dependabot[bot]"))
    assert resp.status_code == 200
    assert rec.events == []
    assert "skip_author author=dependabot[bot]" in caplog.text


def test_skip_authors_is_configurable(monkeypatch):
    # Swap in a whole Settings object (not one attribute) so the test resolves
    # the knob the same way production does: env -> Settings -> skip_authors.
    import app.webhook

    monkeypatch.setattr(
        app.webhook, "settings", Settings(_env_file=None, review_skip_authors="renovate[bot]")
    )
    rec = Recorder()
    assert _post(_client(rec), _pr_payload(author="renovate[bot]")).status_code == 200
    assert _post(_client(rec), _pr_payload(author="dependabot[bot]")).status_code == 202
    assert [e.author for e in rec.events] == ["dependabot[bot]"]


def test_empty_skip_list_reviews_everyone(monkeypatch):
    import app.webhook

    monkeypatch.setattr(app.webhook, "settings", Settings(_env_file=None, review_skip_authors=""))
    rec = Recorder()
    assert _post(_client(rec), _pr_payload(author="dependabot[bot]")).status_code == 202
    assert len(rec.events) == 1


def test_missing_author_is_still_reviewed():
    rec = Recorder()
    assert _post(_client(rec), _pr_payload(author=None)).status_code == 202
    assert len(rec.events) == 1


# --- endpoint: signature gate ---------------------------------------------

def test_forged_signature_rejected_401_no_dispatch():
    rec = Recorder()
    resp = _post(_client(rec), _pr_payload(), secret="wrong-secret")
    assert resp.status_code == 401
    assert rec.events == []


def test_missing_signature_rejected_401():
    resp = _post(_client(), _pr_payload(), sign=False)
    assert resp.status_code == 401


def test_unconfigured_secret_returns_500():
    rec = Recorder()
    # No signature header is even needed: the server bails before verification.
    resp = _post(_client(rec, secret=None), _pr_payload(), sign=False)
    assert resp.status_code == 500
    assert rec.events == []


# --- endpoint: event filtering + bad input --------------------------------

def test_ping_event_acks_200():
    rec = Recorder()
    resp = _post(_client(rec), {"zen": "Keep it logically awesome."}, event="ping")
    assert resp.status_code == 200
    assert rec.events == []


def test_other_event_type_acks_200_without_dispatch():
    rec = Recorder()
    resp = _post(_client(rec), {"action": "created"}, event="issue_comment")
    assert resp.status_code == 200
    assert rec.events == []


def test_signed_but_malformed_json_returns_400():
    rec = Recorder()
    body = b"{not valid json"
    resp = _client(rec).post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 400
    assert rec.events == []


def test_pull_request_missing_fields_returns_400():
    rec = Recorder()
    payload = _pr_payload()
    del payload["repository"]
    resp = _post(_client(rec), payload)
    assert resp.status_code == 400
    assert rec.events == []


# --- health check ---------------------------------------------------------

def test_healthz_ok():
    assert _client().get("/healthz").json() == {"status": "ok"}


# --- default processor wires ingest -> review -> post ---------------------

def _wire_fakes(monkeypatch, pr, posted):
    """Stub the lazily-imported deps of process_event; return nothing."""
    import app.agent.pipeline
    import app.auth
    import app.posting
    from app.models import ReviewOutcome, ReviewResult, RunMetrics

    class FakeClient:
        def fetch_pull_request(self, ref):
            return pr

        def get_file_text(self, ref, path, *, git_ref=None):
            return None  # no workflow files in the base fixture

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
        posted.update(pr=pull_request, block_on=block_on, commit_id=commit_id)
        return {"summary_action": "created", "new_comments": 0, "dismissed": 0,
                "review_id": 5, "event": "COMMENT"}

    monkeypatch.setattr(app.auth, "GitHubAppAuth", FakeAuth)
    monkeypatch.setattr(app.agent.pipeline, "review_pull_request", fake_review)
    monkeypatch.setattr(app.posting, "post_review", fake_post)


def test_process_event_ingests_reviews_and_posts(monkeypatch):
    from app.dedup import DedupStore
    from app.models import PRRef, PullRequest

    pr = PullRequest(ref=PRRef("octo", "hello", 42), title="T", head_sha="abc123def4567890")
    posted: dict = {}
    _wire_fakes(monkeypatch, pr, posted)

    event = WebhookEvent("d-1", "opened", "octo", "hello", 42, "abc123def4567890", 999)
    asyncio.run(process_event(event, store=DedupStore()))

    assert posted["pr"] is pr
    assert posted["commit_id"] == "abc123def4567890"
    assert "leaked_secret" in posted["block_on"]
    # RC1-364: the live agent explores the repo through the API at the PR head,
    # not an empty temp dir.
    from app.agent.github_repository import GitHubRepository
    assert isinstance(posted["repository"], GitHubRepository)
    assert posted["repository"].api_calls == 0  # nothing spent until the pipeline reads


def test_process_event_skips_duplicate_delivery_and_reviewed_sha(monkeypatch):
    from app.dedup import DedupStore
    from app.models import PRRef, PullRequest

    pr = PullRequest(ref=PRRef("octo", "hello", 42), title="T", head_sha="sha-head")
    posted: dict = {}
    _wire_fakes(monkeypatch, pr, posted)
    store = DedupStore()

    event = WebhookEvent("d-1", "opened", "octo", "hello", 42, "sha-head", 1)
    asyncio.run(process_event(event, store=store))
    assert posted.get("reviewed") == 1

    # Same delivery id again -> skipped before any work.
    asyncio.run(process_event(event, store=store))
    assert posted.get("reviewed") == 1

    # New delivery, but the same head SHA was already reviewed -> skipped.
    again = WebhookEvent("d-2", "synchronize", "octo", "hello", 42, "sha-head", 1)
    asyncio.run(process_event(again, store=store))
    assert posted.get("reviewed") == 1


def test_process_event_skips_stale_head(monkeypatch):
    from app.dedup import DedupStore
    from app.models import PRRef, PullRequest

    # The PR's current head has moved past the event's SHA -> stale, skip.
    pr = PullRequest(ref=PRRef("octo", "hello", 42), title="T", head_sha="newer-sha")
    posted: dict = {}
    _wire_fakes(monkeypatch, pr, posted)

    event = WebhookEvent("d-1", "synchronize", "octo", "hello", 42, "older-sha", 1)
    asyncio.run(process_event(event, store=DedupStore()))
    assert "reviewed" not in posted  # never ran the review


# --- the webhook loads and publishes; the pipeline owns the checks (RC1-425) --------

def test_process_event_posts_the_pipelines_result_unchanged(monkeypatch):
    # The deterministic n8n check runs inside the pipeline (RC1-425). The
    # webhook hands it a repository served at the PR head and posts exactly
    # the result it returns — nothing is added, merged or re-run here.
    import app.agent.pipeline
    import app.auth
    import app.posting
    from app.agent.github_repository import GitHubRepository
    from app.dedup import DedupStore
    from app.models import (
        ChangedFile,
        Finding,
        PRRef,
        PullRequest,
        ReviewOutcome,
        ReviewResult,
        RunMetrics,
    )

    pr = PullRequest(
        ref=PRRef("octo", "hello", 42),
        title="Add workflow",
        head_sha="abc123def4567890",
        files=[ChangedFile(filename="flows/poll.json", status="added")],
    )
    fetched: list = []

    class FakeClient:
        def fetch_pull_request(self, ref):
            return pr

        def get_file_text(self, ref, path, *, git_ref=None):
            fetched.append((path, git_ref))
            return "{}"

    class FakeAuth:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def client_for_repo(self, owner, repo):
            return FakeClient()

    findings = [
        Finding("warning", "security", "model claim", file="flows/poll.json", line=1),
        Finding("warning", "n8n", "cron fires every minute", file="flows/poll.json"),
    ]
    seen: dict = {}

    async def fake_review(pull_request, repository, client=None):
        seen["repository"] = repository
        review = ReviewResult(summary="ok", findings=list(findings))
        return ReviewOutcome(review, RunMetrics(model="m"))

    posted: dict = {}

    def fake_post(client, pull_request, result, *, block_on, commit_id=None):
        posted["result"] = result
        return {"summary_action": "created", "new_comments": 0, "dismissed": 0,
                "review_id": 5, "event": "COMMENT"}

    monkeypatch.setattr(app.auth, "GitHubAppAuth", FakeAuth)
    monkeypatch.setattr(app.agent.pipeline, "review_pull_request", fake_review)
    monkeypatch.setattr(app.posting, "post_review", fake_post)

    event = WebhookEvent("d-1", "opened", "octo", "hello", 42, "abc123def4567890", 1)
    asyncio.run(process_event(event, store=DedupStore()))

    assert posted["result"].findings == findings
    # The webhook read nothing itself; the pipeline's repository reads at the head.
    assert fetched == []
    repository = seen["repository"]
    assert isinstance(repository, GitHubRepository)
    assert repository.read_text("flows/poll.json") == "{}"
    assert fetched == [("flows/poll.json", "abc123def4567890")]


# --- logging never leaks secrets ------------------------------------------

def test_rejection_does_not_log_secret_or_signature(caplog):
    body = json.dumps(_pr_payload()).encode()
    sig = _sign(body, "wrong-secret")
    with caplog.at_level(logging.DEBUG, logger="app.webhook"):
        _client().post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d-1",
                "X-Hub-Signature-256": sig,
            },
        )
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in blob
    assert sig not in blob
    assert "signature_rejected" in blob


# --- logging configuration (RC1-120 error visibility) ---------------------

def test_configure_logging_attaches_one_handler_and_is_idempotent():
    app_logger = logging.getLogger("app")
    # Clean any handlers our prior calls (or app build) may have added.
    app_logger.handlers = [h for h in app_logger.handlers if not getattr(h, "_pr_agent", False)]
    configure_logging()
    configure_logging()  # second call must not stack a second handler
    ours = [h for h in app_logger.handlers if getattr(h, "_pr_agent", False)]
    assert len(ours) == 1
    # INFO lifecycle lines must be emittable, and we don't double-print via root.
    assert app_logger.level <= logging.INFO
    assert app_logger.propagate is False


def test_process_event_ships_one_cost_point_per_review(monkeypatch):
    """RC1-395: the metric leaves from the webhook only, tagged with the repo."""
    import app.webhook
    from app.dedup import DedupStore
    from app.models import PRRef, PullRequest

    pr = PullRequest(ref=PRRef("octo", "hello", 42), title="T", head_sha="abc123def4567890")
    posted: dict = {}
    _wire_fakes(monkeypatch, pr, posted)
    shipped = []
    monkeypatch.setattr(
        app.webhook, "ship_review_metrics", lambda result, *, repo: shipped.append((result, repo))
    )

    asyncio.run(
        process_event(
            WebhookEvent("d-1", "opened", "octo", "hello", 42, "abc123def4567890", 999),
            store=DedupStore(),
        )
    )

    assert len(shipped) == 1
    metrics, repo = shipped[0]
    assert repo == "octo/hello" and metrics.model == "m", "the run's metrics, not the review"
