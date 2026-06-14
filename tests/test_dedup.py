"""Tests for the re-push dedup store (RC1-118)."""
from __future__ import annotations

from app.dedup import DedupStore


def test_seen_delivery_is_check_and_set():
    store = DedupStore()
    assert store.seen_delivery("d1") is False  # first sight
    assert store.seen_delivery("d1") is True   # now remembered
    assert store.seen_delivery("d2") is False


def test_reviewed_is_tracked_per_pr_and_sha():
    store = DedupStore()
    assert store.already_reviewed("o/r#1", "sha-a") is False
    store.mark_reviewed("o/r#1", "sha-a")
    assert store.already_reviewed("o/r#1", "sha-a") is True
    # A new head SHA on the same PR is not yet reviewed.
    assert store.already_reviewed("o/r#1", "sha-b") is False
    # Same SHA, different PR is independent.
    assert store.already_reviewed("o/r#2", "sha-a") is False


def test_bounded_eviction_drops_oldest():
    store = DedupStore(max_entries=3)
    for d in ("d1", "d2", "d3"):
        store.seen_delivery(d)
    store.seen_delivery("d4")  # evicts d1 (oldest)
    assert store.seen_delivery("d1") is False  # was evicted -> treated as new
    assert store.seen_delivery("d4") is True   # still remembered
