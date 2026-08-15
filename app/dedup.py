"""Re-push / redelivery deduplication (RC1-118).

A small in-memory store that keeps the agent from reviewing the same thing
twice across GitHub's webhook redeliveries and rapid re-pushes:

- **delivery id** — GitHub may redeliver the same webhook; we process each
  ``X-GitHub-Delivery`` at most once (check-and-set).
- **(PR, head SHA)** — once we've reviewed a given head commit we don't review
  it again; a later push has its own SHA and its own event.

This is process-local and bounded (oldest entries evicted), which suits the
single-instance service. It resets on restart — acceptable, since a missed
dedup just means one redundant review, never a wrong one. Persisting it (e.g.
to Redis) is a later concern if the service scales horizontally.

The "only the latest head is reviewed" criterion is enforced separately, at
review time, by comparing the event's head SHA against the PR's current head
(see :func:`app.webhook.process_event`) — a stale event is dropped there.
"""
from __future__ import annotations

import threading
from collections import OrderedDict


class DedupStore:
    """Thread-safe, bounded record of seen deliveries and reviewed commits."""

    def __init__(self, *, max_entries: int = 2048) -> None:
        self._max = max_entries
        self._deliveries: OrderedDict[str, None] = OrderedDict()
        self._reviewed: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._lock = threading.Lock()

    def _remember(self, store: OrderedDict, key) -> bool:
        """Insert ``key``; return whether it was already present. Evicts oldest."""
        if key in store:
            store.move_to_end(key)
            return True
        store[key] = None
        if len(store) > self._max:
            store.popitem(last=False)
        return False

    def seen_delivery(self, delivery_id: str) -> bool:
        """True if this delivery was already seen; records it as seen either way."""
        with self._lock:
            return self._remember(self._deliveries, delivery_id)

    def already_reviewed(self, pr_slug: str, head_sha: str) -> bool:
        """True if (PR, head SHA) was already reviewed. Does not record."""
        with self._lock:
            return (pr_slug, head_sha) in self._reviewed

    def mark_reviewed(self, pr_slug: str, head_sha: str) -> None:
        """Record that (PR, head SHA) has been reviewed (call after a success)."""
        with self._lock:
            self._remember(self._reviewed, (pr_slug, head_sha))


# Shared, process-local store used by the live webhook worker.
dedup_store = DedupStore()
