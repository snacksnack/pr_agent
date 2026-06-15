"""Tests for GitHub API retry/backoff (RC1-120).

All offline: a fake ``send`` callable scripts the sequence of responses/errors,
and ``sleep`` is a no-op recorder so nothing actually waits.
"""
from __future__ import annotations

import httpx
import pytest

from app.retry import _is_retryable, _retry_after_seconds, request_with_retry


def _resp(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {})


class _Script:
    """A ``send`` callable that yields scripted responses/exceptions in order."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> httpx.Response:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _sleeper():
    slept: list[float] = []
    return slept, lambda d: slept.append(d)


# --- retryable classification --------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_are_retryable(status):
    assert _is_retryable(_resp(status)) is True


@pytest.mark.parametrize("status", [200, 201, 400, 401, 404, 422])
def test_terminal_statuses_are_not_retryable(status):
    assert _is_retryable(_resp(status)) is False


def test_plain_403_not_retryable_but_rate_limited_403_is():
    assert _is_retryable(_resp(403)) is False
    assert _is_retryable(_resp(403, {"Retry-After": "1"})) is True
    assert _is_retryable(
        _resp(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"})
    ) is True


# --- retry-after parsing --------------------------------------------------

def test_retry_after_header_wins():
    assert _retry_after_seconds(_resp(429, {"Retry-After": "7"})) == 7.0


def test_ratelimit_reset_only_when_exhausted():
    # remaining > 0 => not exhausted => no directed wait
    assert _retry_after_seconds(_resp(403, {"X-RateLimit-Remaining": "5"})) is None
    # remaining == 0 => wait until reset (clamped to >= 0)
    assert _retry_after_seconds(
        _resp(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})
    ) == 0.0


# --- the retry loop -------------------------------------------------------

def test_retries_5xx_then_succeeds():
    script = _Script(_resp(503), _resp(502), _resp(200))
    slept, sleep = _sleeper()
    resp = request_with_retry(script, sleep=sleep)
    assert resp.status_code == 200
    assert script.calls == 3
    assert len(slept) == 2  # slept between each retry


def test_returns_last_response_after_exhausting_attempts():
    script = _Script(_resp(500), _resp(500), _resp(500), _resp(500))
    slept, sleep = _sleeper()
    resp = request_with_retry(script, max_attempts=4, sleep=sleep)
    # Caller still gets the final 500 to convert into its own error.
    assert resp.status_code == 500
    assert script.calls == 4
    assert len(slept) == 3


def test_no_retry_on_terminal_status():
    script = _Script(_resp(404))
    slept, sleep = _sleeper()
    resp = request_with_retry(script, sleep=sleep)
    assert resp.status_code == 404
    assert script.calls == 1
    assert slept == []


def test_retries_transport_errors_then_reraises():
    boom = httpx.ConnectError("connection refused")
    script = _Script(boom, boom, boom, boom)
    slept, sleep = _sleeper()
    with pytest.raises(httpx.ConnectError):
        request_with_retry(script, max_attempts=4, sleep=sleep)
    assert script.calls == 4
    assert len(slept) == 3


def test_transport_error_then_recovers():
    script = _Script(httpx.ReadTimeout("slow"), _resp(200))
    slept, sleep = _sleeper()
    resp = request_with_retry(script, sleep=sleep)
    assert resp.status_code == 200
    assert script.calls == 2


def test_honors_retry_after_for_sleep_duration():
    script = _Script(_resp(429, {"Retry-After": "3"}), _resp(200))
    slept, sleep = _sleeper()
    request_with_retry(script, sleep=sleep, max_delay=30)
    assert slept == [3.0]


def test_retry_after_capped_at_max_delay():
    script = _Script(_resp(429, {"Retry-After": "999"}), _resp(200))
    slept, sleep = _sleeper()
    request_with_retry(script, sleep=sleep, max_delay=10)
    assert slept == [10.0]
