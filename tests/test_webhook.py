"""Tests for the FastAPI webhook receiver (RC1-116, RC1-423).

Fully offline: the job store is SQLite in a temp dir, the worker's runner is
a fake, signatures are computed with a known test secret, and requests go
through Starlette's ``TestClient``. Outside a ``with TestClient(...)`` block
the lifespan does not run, so the worker is idle and a persisted job stays
``queued`` — which is exactly what the endpoint is responsible for.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import jobs
from app.config import Settings
from app.jobs import JobStore
from app.webhook import (
    WebhookParseError,
    configure_logging,
    create_app,
    parse_pull_request_event,
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


def _store() -> JobStore:
    return JobStore(Path(tempfile.mkdtemp(prefix="pr-webhook-")) / "jobs.db")


async def _never_run(job, store):  # the worker is idle without the lifespan anyway
    raise AssertionError("the runner ran")


def _client(store: JobStore | None = None, *, secret: str | None = SECRET) -> TestClient:
    return TestClient(create_app(secret=secret, store=store or _store(), runner=_never_run))


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
def test_processed_actions_are_persisted_before_the_202(action):
    store = _store()
    resp = _post(_client(store), _pr_payload(action))
    assert resp.status_code == 202
    [job] = store.jobs()
    assert (job.state, job.action, job.slug, job.delivery_id) == (
        jobs.QUEUED, action, "octo/hello#42", "d-1"
    )
    assert job.head_sha == "abc123def4567890" and job.installation_id == 999


def test_a_redelivery_is_acked_without_a_second_job(caplog):
    store = _store()
    client = _client(store)
    assert _post(client, _pr_payload(), delivery="d-1").status_code == 202
    with caplog.at_level(logging.INFO, logger="app.webhook"):
        assert _post(client, _pr_payload(), delivery="d-1").status_code == 202
    assert len(store.jobs()) == 1
    assert "duplicate_delivery" in caplog.text
    assert _post(client, _pr_payload(), delivery="d-2").status_code == 202
    assert len(store.jobs()) == 2


def test_ignored_action_acks_200_without_a_job():
    store = _store()
    resp = _post(_client(store), _pr_payload("closed"))
    assert resp.status_code == 200
    assert store.jobs() == []


# --- endpoint: skipped authors (RC1-359 cost guardrail) -------------------

@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_dependabot_pr_acks_200_without_a_job(action, caplog):
    store = _store()
    with caplog.at_level(logging.INFO, logger="app.webhook"):
        resp = _post(_client(store), _pr_payload(action, author="dependabot[bot]"))
    assert resp.status_code == 200
    assert store.jobs() == []
    assert "skip_author author=dependabot[bot]" in caplog.text


def test_skip_authors_is_configurable(monkeypatch):
    # Swap in a whole Settings object (not one attribute) so the test resolves
    # the knob the same way production does: env -> Settings -> skip_authors.
    import app.webhook

    monkeypatch.setattr(
        app.webhook, "settings", Settings(_env_file=None, review_skip_authors="renovate[bot]")
    )
    store = _store()
    assert _post(_client(store), _pr_payload(author="renovate[bot]")).status_code == 200
    assert _post(_client(store), _pr_payload(author="dependabot[bot]")).status_code == 202
    assert [j.author for j in store.jobs()] == ["dependabot[bot]"]


def test_empty_skip_list_reviews_everyone(monkeypatch):
    import app.webhook

    monkeypatch.setattr(app.webhook, "settings", Settings(_env_file=None, review_skip_authors=""))
    store = _store()
    assert _post(_client(store), _pr_payload(author="dependabot[bot]")).status_code == 202
    assert len(store.jobs()) == 1


def test_missing_author_is_still_reviewed():
    store = _store()
    assert _post(_client(store), _pr_payload(author=None)).status_code == 202
    assert len(store.jobs()) == 1


# --- endpoint: signature gate ---------------------------------------------

def test_forged_signature_rejected_401_no_dispatch():
    store = _store()
    resp = _post(_client(store), _pr_payload(), secret="wrong-secret")
    assert resp.status_code == 401
    assert store.jobs() == []


def test_missing_signature_rejected_401():
    resp = _post(_client(), _pr_payload(), sign=False)
    assert resp.status_code == 401


def test_unconfigured_secret_returns_500():
    store = _store()
    # No signature header is even needed: the server bails before verification.
    resp = _post(_client(store, secret=None), _pr_payload(), sign=False)
    assert resp.status_code == 500
    assert store.jobs() == []


# --- endpoint: event filtering + bad input --------------------------------

def test_ping_event_acks_200():
    store = _store()
    resp = _post(_client(store), {"zen": "Keep it logically awesome."}, event="ping")
    assert resp.status_code == 200
    assert store.jobs() == []


def test_other_event_type_acks_200_without_dispatch():
    store = _store()
    resp = _post(_client(store), {"action": "created"}, event="issue_comment")
    assert resp.status_code == 200
    assert store.jobs() == []


def test_signed_but_malformed_json_returns_400():
    store = _store()
    body = b"{not valid json"
    resp = _client(store).post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-1",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 400
    assert store.jobs() == []


def test_pull_request_missing_fields_returns_400():
    store = _store()
    payload = _pr_payload()
    del payload["repository"]
    resp = _post(_client(store), payload)
    assert resp.status_code == 400
    assert store.jobs() == []


# --- health check ---------------------------------------------------------

def test_healthz_reports_the_queue(caplog):
    store = _store()
    client = _client(store)
    empty = {"queued": 0, "running": 0, "succeeded": 0, "skipped": 0, "failed": 0}
    assert client.get("/healthz").json() == {"status": "ok", "jobs": empty}
    _post(client, _pr_payload())
    assert client.get("/healthz").json()["jobs"] == {**empty, "queued": 1}


# --- the lifespan: recovery at startup, the worker drains what the endpoint queued ----

def test_the_worker_runs_a_persisted_job_end_to_end_under_the_lifespan():
    store = _store()
    ran = []

    async def runner(job, s):
        ran.append(job.delivery_id)
        return None

    with TestClient(create_app(secret=SECRET, store=store, runner=runner)) as client:
        assert _post(client, _pr_payload()).status_code == 202
        deadline = time.monotonic() + 5
        while store.jobs()[0].state != jobs.SUCCEEDED and time.monotonic() < deadline:
            time.sleep(0.02)
    assert ran == ["d-1"]
    assert store.jobs()[0].state == jobs.SUCCEEDED


def test_startup_recovers_a_job_the_last_process_left_running():
    store = _store()
    store.enqueue(
        parse_pull_request_event("d-old", _pr_payload())
    )
    store.claim_next()  # the last process died here
    ran = []

    async def runner(job, s):
        ran.append((job.delivery_id, job.attempts))
        return None

    with TestClient(create_app(secret=SECRET, store=store, runner=runner)):
        deadline = time.monotonic() + 5
        while store.jobs()[0].state != jobs.SUCCEEDED and time.monotonic() < deadline:
            time.sleep(0.02)
    assert ran == [("d-old", 2)]


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
