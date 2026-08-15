"""Tests for review posting + re-push dedup/supersede (RC1-117 / RC1-118).

Patch parsing and payload building are pure functions. The orchestration in
``post_review`` is exercised against a fake client that records calls; the new
GitHubClient HTTP methods are checked against ``httpx.MockTransport``.
"""
from __future__ import annotations

import json

import httpx

from app.github import GitHubClient, GitHubError
from app.models import ChangedFile, Finding, PRRef, PullRequest, ReviewResult
from app.posting import (
    REVIEW_MARKER,
    SUMMARY_MARKER,
    build_summary_body,
    commentable_lines,
    filter_new_comments,
    find_marked,
    post_review,
    split_findings,
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


def _pr(files=None) -> PullRequest:
    files = files if files is not None else [
        ChangedFile(filename="app/x.py", status="modified", patch=PATCH)
    ]
    return PullRequest(
        ref=PRRef("octo", "hello", 42), title="T", head_sha="headsha123", files=files
    )


def _result(findings, summary="Looks mostly fine.") -> ReviewResult:
    return ReviewResult(summary=summary, findings=findings, model="m")


# --- pure functions -------------------------------------------------------

def test_commentable_lines_added_and_context_not_removed():
    assert commentable_lines(PATCH) == {1, 2, 3, 4, 11}


def test_commentable_lines_none_is_empty():
    assert commentable_lines(None) == set()
    assert commentable_lines("") == set()


def test_split_findings_inline_vs_overflow():
    findings = [
        Finding("warning", "pythonic", "anchored", file="app/x.py", line=2),
        Finding("warning", "pr_drift", "PR-level"),  # no file/line
        Finding("nit", "docs", "off-diff", file="app/x.py", line=999),  # line not in diff
    ]
    comments, overflow = split_findings(_pr(), _result(findings))
    assert [c["line"] for c in comments] == [2]
    assert comments[0]["side"] == "RIGHT"
    assert {f.message for f in overflow} == {"PR-level", "off-diff"}


def test_build_summary_body_has_marker_and_verdict():
    body = build_summary_body("Top line.", [], [], ["leaked_secret"], EVENT_COMMENT)
    assert body.startswith(SUMMARY_MARKER)
    assert "Top line." in body and "advisory" in body.lower()


def test_filter_new_comments_drops_existing():
    comments = [
        {"path": "a.py", "line": 2, "side": "RIGHT", "body": "dup"},
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "fresh"},
    ]
    existing = [{"path": "a.py", "line": 2, "body": "dup"}]
    assert [c["body"] for c in filter_new_comments(comments, existing)] == ["fresh"]


def test_find_marked():
    comments = [{"id": 1, "body": "hi"}, {"id": 2, "body": f"{SUMMARY_MARKER}\nx"}]
    assert find_marked(comments, SUMMARY_MARKER)["id"] == 2
    assert find_marked(comments, "nope") is None


# --- orchestration via a recording fake client ----------------------------

class FakeGitHub:
    def __init__(self, *, issue_comments=None, review_comments=None, reviews=None, raise_422=False):
        self.issue_comments = issue_comments or []
        self.review_comments = review_comments or []
        self.reviews = reviews or []
        self.raise_422 = raise_422
        self.calls: list[tuple] = []
        self._id = 100

    def _log(self, *args):
        self.calls.append(args)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def list_issue_comments(self, ref):
        self._log("list_issue_comments")
        return list(self.issue_comments)

    def create_issue_comment(self, ref, body):
        self._log("create_issue_comment", body)
        return {"id": 1, "body": body}

    def update_issue_comment(self, ref, comment_id, body):
        self._log("update_issue_comment", comment_id, body)
        return {"id": comment_id, "body": body}

    def list_review_comments(self, ref):
        self._log("list_review_comments")
        return list(self.review_comments)

    def list_reviews(self, ref):
        self._log("list_reviews")
        return list(self.reviews)

    def dismiss_review(self, ref, review_id, message):
        self._log("dismiss_review", review_id)
        return {"id": review_id, "state": "DISMISSED"}

    def create_review(self, ref, *, body, event, comments=None, commit_id=None):
        self._log("create_review", {"event": event, "comments": comments, "commit_id": commit_id})
        if self.raise_422 and comments:
            raise GitHubError("line not part of the diff", status=422)
        self._id += 1
        return {
            "id": self._id,
            "state": "CHANGES_REQUESTED" if event == EVENT_REQUEST_CHANGES else "COMMENTED",
        }


def test_first_push_creates_summary_and_posts_inline():
    gh = FakeGitHub()
    findings = [Finding("warning", "pythonic", "x", file="app/x.py", line=2)]
    out = post_review(gh, _pr(), _result(findings), block_on=["leaked_secret"])
    assert out["summary_action"] == "created"
    assert out["new_comments"] == 1
    assert out["review_id"] is not None
    assert "create_issue_comment" in gh.names()


def test_existing_summary_is_updated_in_place():
    gh = FakeGitHub(issue_comments=[{"id": 55, "body": f"{SUMMARY_MARKER}\nold"}])
    out = post_review(gh, _pr(), _result([]), block_on=["leaked_secret"])
    assert out["summary_action"] == "updated"
    update_calls = [c for c in gh.calls if c[0] == "update_issue_comment"]
    assert len(update_calls) == 1 and update_calls[0][1] == 55  # edited that comment id
    assert "create_issue_comment" not in gh.names()


def test_repush_does_not_repost_identical_comment():
    # The inline comment we'd post already exists (same path/line/body) -> skipped,
    # and with an advisory verdict and nothing new, no review is created.
    pr = _pr()
    findings = [Finding("warning", "pythonic", "x", file="app/x.py", line=2)]
    body = split_findings(pr, _result(findings))[0][0]["body"]
    gh = FakeGitHub(review_comments=[{"path": "app/x.py", "line": 2, "body": body}])
    out = post_review(gh, pr, _result(findings), block_on=["leaked_secret"])
    assert out["new_comments"] == 0
    assert out["review_id"] is None
    assert "create_review" not in gh.names()


def test_request_changes_posts_review_even_with_no_new_comments():
    gh = FakeGitHub()
    findings = [Finding("blocker", "leaked_secret", "key committed")]  # PR-level -> overflow
    out = post_review(gh, _pr(), _result(findings), block_on=["leaked_secret"])
    assert out["event"] == EVENT_REQUEST_CHANGES
    assert out["new_comments"] == 0
    assert out["review_id"] is not None  # gate still asserted


def test_supersedes_only_our_marked_change_requests():
    reviews = [
        {"id": 1, "state": "CHANGES_REQUESTED", "body": f"{REVIEW_MARKER}\nold gate"},  # ours
        {"id": 2, "state": "CHANGES_REQUESTED", "body": "please fix this"},  # a human's
        {"id": 3, "state": "COMMENTED", "body": f"{REVIEW_MARKER}\nold note"},  # ours, not a gate
    ]
    gh = FakeGitHub(reviews=reviews)
    out = post_review(gh, _pr(), _result([]), block_on=["leaked_secret"])
    dismissed_ids = [c[1] for c in gh.calls if c[0] == "dismiss_review"]
    assert dismissed_ids == [1]  # only our active change-request
    assert out["dismissed"] == 1


def test_422_on_inline_retries_summary_only_review():
    gh = FakeGitHub(raise_422=True)
    findings = [Finding("blocker", "leaked_secret", "x", file="app/x.py", line=2)]
    out = post_review(gh, _pr(), _result(findings), block_on=["leaked_secret"])
    create_calls = [c for c in gh.calls if c[0] == "create_review"]
    assert len(create_calls) == 2  # first with comments (422), retry without
    assert create_calls[1][1]["comments"] is None
    assert out["review_id"] is not None


# --- new GitHubClient HTTP methods (wire shape) ---------------------------

def _client(handler) -> GitHubClient:
    return GitHubClient(token="t", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_client_issue_comment_and_dismiss_wire():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[(request.method, request.url.path)] = json.loads(request.content or b"{}")
        if request.url.path.endswith("/issues/42/comments"):
            return httpx.Response(201, json={"id": 9})
        if request.url.path.endswith("/issues/comments/9"):
            return httpx.Response(200, json={"id": 9})
        if request.url.path.endswith("/reviews/3/dismissals"):
            return httpx.Response(200, json={"id": 3, "state": "DISMISSED"})
        return httpx.Response(404, json={"message": "x"})

    ref = PRRef("octo", "hello", 42)
    gh = _client(handler)
    gh.create_issue_comment(ref, "hello")
    gh.update_issue_comment(ref, 9, "edited")
    gh.dismiss_review(ref, 3, "superseded")

    assert seen[("POST", "/repos/octo/hello/issues/42/comments")] == {"body": "hello"}
    assert seen[("PATCH", "/repos/octo/hello/issues/comments/9")] == {"body": "edited"}
    assert seen[("PUT", "/repos/octo/hello/pulls/42/reviews/3/dismissals")]["event"] == "DISMISS"
