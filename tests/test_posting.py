"""Tests for review posting (RC1-117), fully offline.

Patch parsing and payload building are pure functions; the GitHub POST is mocked
with ``httpx.MockTransport`` so we assert the request shape without a network.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.github import GitHubClient
from app.models import ChangedFile, Finding, PRRef, PullRequest, ReviewResult
from app.posting import (
    build_review_payload,
    commentable_lines,
    post_review,
)
from app.verdict import EVENT_COMMENT, EVENT_REQUEST_CHANGES

PATCH = """@@ -1,3 +1,4 @@
 import os
+import sys
 x = 1
 y = 2
@@ -10,2 +11,2 @@
-old = removed
+new = added"""


def _pr(files: list[ChangedFile]) -> PullRequest:
    return PullRequest(
        ref=PRRef("octo", "hello", 42),
        title="T",
        head_sha="headsha123",
        files=files,
    )


def _result(findings: list[Finding], summary: str = "Looks mostly fine.") -> ReviewResult:
    return ReviewResult(summary=summary, findings=findings, model="m")


# --- commentable_lines ----------------------------------------------------

def test_commentable_lines_added_and_context_not_removed():
    lines = commentable_lines(PATCH)
    # First hunk: new lines 1..4 (context + added). Second hunk: 11 is removed
    # (left side, excluded), 11->"+new = added" is line 11 on the right? trace:
    #   @@ +11,2 @@  right=11
    #   "-old = removed"  -> left only, right stays 11
    #   "+new = added"    -> right line 11
    assert lines == {1, 2, 3, 4, 11}


def test_commentable_lines_none_or_binary_is_empty():
    assert commentable_lines(None) == set()
    assert commentable_lines("") == set()


def test_commentable_lines_ignores_no_newline_marker():
    patch = "@@ -1 +1 @@\n+only line\n\\ No newline at end of file"
    assert commentable_lines(patch) == {1}


# --- build_review_payload -------------------------------------------------

def test_anchorable_finding_becomes_inline_comment():
    pr = _pr([ChangedFile(filename="app/x.py", status="modified", patch=PATCH)])
    findings = [Finding("warning", "pythonic", "prefer pathlib", file="app/x.py", line=2,
                        suggestion="use Path")]
    payload = build_review_payload(pr, _result(findings), ["leaked_secret"])
    assert payload.event == EVENT_COMMENT
    assert len(payload.comments) == 1
    c = payload.comments[0]
    assert (c["path"], c["line"], c["side"]) == ("app/x.py", 2, "RIGHT")
    assert "warning" in c["body"] and "prefer pathlib" in c["body"]
    assert "use Path" in c["body"]


def test_unanchorable_findings_fold_into_summary():
    pr = _pr([ChangedFile(filename="app/x.py", status="modified", patch=PATCH)])
    findings = [
        Finding("warning", "pr_drift", "description omits the migration"),  # PR-level
        Finding("nit", "docs", "stale comment", file="app/x.py", line=999),  # line not in diff
    ]
    payload = build_review_payload(pr, _result(findings), ["leaked_secret"])
    assert payload.comments == []  # neither could anchor
    assert "Findings not tied to a specific line" in payload.body
    assert "description omits the migration" in payload.body
    assert "stale comment" in payload.body


def test_verdict_escalates_on_block_on_category():
    pr = _pr([ChangedFile(filename="cfg.py", status="added", patch="@@ -0,0 +1 @@\n+SECRET=abc")])
    findings = [Finding("blocker", "leaked_secret", "API key committed", file="cfg.py", line=1)]
    payload = build_review_payload(pr, _result(findings), ["leaked_secret"])
    assert payload.event == EVENT_REQUEST_CHANGES
    assert "Request changes" in payload.body


def test_summary_includes_verdict_when_advisory():
    pr = _pr([ChangedFile(filename="a.py", status="modified", patch=PATCH)])
    payload = build_review_payload(pr, _result([]), ["leaked_secret"])
    assert payload.event == EVENT_COMMENT
    assert "advisory" in payload.body.lower()
    assert "Looks mostly fine." in payload.body


# --- post_review (mocked GitHub) ------------------------------------------

def _client(handler) -> GitHubClient:
    return GitHubClient(token="t", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_post_review_posts_single_review_pinned_to_head():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octo/hello/pulls/42/reviews"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "state": "COMMENTED"})

    pr = _pr([ChangedFile(filename="app/x.py", status="modified", patch=PATCH)])
    findings = [Finding("warning", "pythonic", "prefer pathlib", file="app/x.py", line=2)]
    review = post_review(_client(handler), pr, _result(findings), block_on=["leaked_secret"])

    assert review == {"id": 7, "state": "COMMENTED"}
    assert seen["body"]["commit_id"] == "headsha123"
    assert seen["body"]["event"] == "COMMENT"
    assert len(seen["body"]["comments"]) == 1


def test_post_review_falls_back_to_summary_only_on_422():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("comments"):
            return httpx.Response(422, json={"message": "line not part of the diff"})
        return httpx.Response(200, json={"id": 9, "state": "COMMENTED"})

    pr = _pr([ChangedFile(filename="app/x.py", status="modified", patch=PATCH)])
    findings = [Finding("warning", "pythonic", "prefer pathlib", file="app/x.py", line=2)]
    review = post_review(_client(handler), pr, _result(findings), block_on=["leaked_secret"])

    assert review == {"id": 9, "state": "COMMENTED"}
    assert len(calls) == 2  # first with comments (422), retry without
    assert "comments" not in calls[1] or not calls[1]["comments"]
    assert "All findings" in calls[1]["body"]  # folded in


def test_post_review_does_not_retry_on_non_422():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    pr = _pr([ChangedFile(filename="app/x.py", status="modified", patch=PATCH)])
    findings = [Finding("warning", "pythonic", "x", file="app/x.py", line=2)]
    with pytest.raises(Exception):
        post_review(_client(handler), pr, _result(findings), block_on=["leaked_secret"])
