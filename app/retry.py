"""Retry/backoff for GitHub API calls (RC1-120).

A small wrapper that re-sends a GitHub request on *transient* failures so a brief
blip — a dropped connection, a 5xx, a rate-limit window — doesn't sink a review.
The live worker runs in the background with no client to retry it, so resilience
has to live here.

What's retried (and only this):

- transport errors (connect/read timeout, connection reset) — the request never
  got an answer, so re-sending is safe;
- ``5xx`` server errors and ``429 Too Many Requests`` — GitHub is asking us to
  back off;
- a ``403`` *only* when it carries a rate-limit signal (GitHub returns 403 for
  its secondary rate limit). A plain 403 is a permission error — not retryable.

Everything else (404, 401, 422, a normal 2xx) returns immediately for the caller
to handle. Backoff is exponential with jitter and honors GitHub's own
``Retry-After`` / ``X-RateLimit-Reset`` hints when present, all bounded so a
pathological case can't hang the worker. ``sleep`` is injectable so tests run
instantly and offline.
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger("app.retry")

# Transient status codes worth re-sending on: server errors + primary rate limit.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 4  # 1 initial try + 3 retries
DEFAULT_BASE_DELAY = 0.5  # seconds; doubles each retry (0.5, 1, 2, ...)
DEFAULT_MAX_DELAY = 30.0  # cap any single backoff (incl. server-directed waits)


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff for ``attempt`` (1-based) with full jitter, capped."""
    window = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0, window)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """GitHub's own hint for how long to wait, if it gave one.

    Prefers the ``Retry-After`` header (seconds); falls back to the time until
    ``X-RateLimit-Reset`` but only when the remaining quota is actually ``0``
    (so a normal response near reset isn't mistaken for exhaustion).
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time())
            except ValueError:
                pass
    return None


def _is_retryable(resp: httpx.Response) -> bool:
    """Whether a *response* (not a transport error) is worth re-sending."""
    if resp.status_code in RETRY_STATUSES:
        return True
    if resp.status_code == 403:
        # GitHub uses 403 for its secondary rate limit; retry only those, never
        # a genuine permission denial.
        return _retry_after_seconds(resp) is not None
    return False


def request_with_retry(
    send: Callable[[], httpx.Response],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    describe: str = "",
) -> httpx.Response:
    """Call ``send`` with bounded backoff on transient failures.

    Returns the final response — including a non-2xx one once retries are spent,
    so the caller's existing status handling still runs. Re-raises the last
    transport error if every attempt failed to reach GitHub.
    """
    for attempt in range(1, max_attempts + 1):
        last = attempt == max_attempts
        try:
            resp = send()
        except httpx.HTTPError as exc:  # connect/read timeout, conn reset, ...
            if last:
                logger.warning("github_retry_exhausted %s reason=transport err=%s", describe, exc)
                raise
            delay = _backoff(attempt, base_delay, max_delay)
            logger.warning(
                "github_retry %s attempt=%d/%d reason=transport err=%s sleep=%.1fs",
                describe, attempt, max_attempts, exc, delay,
            )
            sleep(delay)
            continue

        if last or not _is_retryable(resp):
            return resp

        directed = _retry_after_seconds(resp)
        delay = (
            min(directed, max_delay)
            if directed is not None
            else _backoff(attempt, base_delay, max_delay)
        )
        logger.warning(
            "github_retry %s attempt=%d/%d status=%d sleep=%.1fs",
            describe, attempt, max_attempts, resp.status_code, delay,
        )
        sleep(delay)

    # Unreachable: the loop always returns or raises on the final attempt.
    raise RuntimeError("request_with_retry exhausted without returning")  # pragma: no cover
