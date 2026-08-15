"""Tests for GitHub PR ingestion (RC1-108).

The GitHub API is mocked with ``httpx.MockTransport`` so these run offline.
"""
from __future__ import annotations

import base64

import httpx
import pytest

from app.github import GitHubClient, GitHubError, parse_pr_spec
from app.models import PRRef

# --- spec parsing ---------------------------------------------------------

def test_parse_pr_spec_valid():
    ref = parse_pr_spec("octocat/Hello-World#42")
    assert ref == PRRef(owner="octocat", repo="Hello-World", number=42)
    assert ref.slug == "octocat/Hello-World#42"


@pytest.mark.parametrize("bad", ["octocat/hello", "octocat#42", "owner/repo#", "owner/repo#x", ""])
def test_parse_pr_spec_invalid(bad):
    with pytest.raises(ValueError):
        parse_pr_spec(bad)


# --- helpers --------------------------------------------------------------

def _pr_json(**over):
    data = {
        "title": "Add feature",
        "body": "Implements the thing.",
        "state": "open",
        "user": {"login": "octocat"},
        "base": {"ref": "main", "sha": "base123"},
        "head": {"ref": "feature", "sha": "head456"},
        "additions": 10,
        "deletions": 2,
        "changed_files": 2,
        "html_url": "https://github.com/octocat/Hello-World/pull/42",
    }
    data.update(over)
    return data


def _file(name, **over):
    item = {
        "filename": name,
        "status": "modified",
        "additions": 1,
        "deletions": 0,
        "changes": 1,
        "patch": f"@@ -1 +1 @@\n-old\n+new  # {name}",
    }
    item.update(over)
    return item


def _client(handler) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    return GitHubClient(token="t", client=httpx.Client(transport=transport))


# --- fetch ----------------------------------------------------------------

def test_fetch_metadata_and_files_with_pagination():
    # 150 files => two pages (100 + 50).
    all_files = [_file(f"f{i}.py") for i in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/42") and not path.endswith("/files"):
            assert request.headers["Authorization"] == "Bearer t"
            return httpx.Response(200, json=_pr_json())
        if path.endswith("/pulls/42/files"):
            page = int(request.url.params["page"])
            per = int(request.url.params["per_page"])
            start = (page - 1) * per
            return httpx.Response(200, json=all_files[start : start + per])
        return httpx.Response(404)

    with _client(handler) as gh:
        pr = gh.fetch_pull_request(PRRef("octocat", "Hello-World", 42))

    assert pr.title == "Add feature"
    assert pr.author == "octocat"
    assert pr.base_sha == "base123" and pr.head_sha == "head456"
    assert pr.head_ref == "feature"
    assert len(pr.files) == 150
    assert pr.truncated_files is False
    assert pr.files[0].has_patch


def test_max_files_truncation():
    all_files = [_file(f"f{i}.py") for i in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            page = int(request.url.params["page"])
            per = int(request.url.params["per_page"])
            start = (page - 1) * per
            return httpx.Response(200, json=all_files[start : start + per])
        return httpx.Response(200, json=_pr_json())

    with _client(handler) as gh:
        pr = gh.fetch_pull_request(PRRef("o", "r", 42), max_files=120)

    assert len(pr.files) == 120
    assert pr.truncated_files is True


# --- get_file_text (Contents API; RC1-121 live n8n source) ----------------

def _contents_response(text: str) -> dict:
    return {
        "encoding": "base64",
        "content": base64.b64encode(text.encode()).decode(),
    }


def test_get_file_text_decodes_content_at_ref():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["ref"] = request.url.params.get("ref")
        return httpx.Response(200, json=_contents_response('{"hello": 1}'))

    with _client(handler) as gh:
        text = gh.get_file_text(PRRef("o", "r", 42), "flows/poll.json", git_ref="headsha")

    assert text == '{"hello": 1}'
    # The path is sourced at the requested ref, with slashes preserved.
    assert captured["path"] == "/repos/o/r/contents/flows/poll.json"
    assert captured["ref"] == "headsha"


def test_get_file_text_missing_file_returns_none():
    with _client(lambda req: httpx.Response(404)) as gh:
        assert gh.get_file_text(PRRef("o", "r", 42), "gone.json", git_ref="h") is None


def test_get_file_text_directory_or_oversized_returns_none():
    # A directory yields a JSON list; an oversized file omits inline content.
    def handler(request: httpx.Request) -> httpx.Response:
        if "dir" in request.url.path:
            return httpx.Response(200, json=[{"name": "a.json"}])
        return httpx.Response(200, json={"encoding": "none", "content": ""})

    with _client(handler) as gh:
        assert gh.get_file_text(PRRef("o", "r", 42), "dir", git_ref="h") is None
        assert gh.get_file_text(PRRef("o", "r", 42), "big.json", git_ref="h") is None


def test_get_file_text_undecodable_bytes_returns_none():
    bad = base64.b64encode(b"\xff\xfe\x00").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"encoding": "base64", "content": bad})

    with _client(handler) as gh:
        assert gh.get_file_text(PRRef("o", "r", 42), "x.json", git_ref="h") is None


def test_missing_patch_is_kept_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[_file("image.png", patch=None, status="added")])
        return httpx.Response(200, json=_pr_json())

    with _client(handler) as gh:
        pr = gh.fetch_pull_request(PRRef("o", "r", 42))

    assert len(pr.files) == 1
    assert pr.files[0].patch is None
    assert pr.files[0].has_patch is False


def test_404_raises_actionable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with _client(handler) as gh, pytest.raises(GitHubError):
        gh.fetch_pull_request(PRRef("o", "r", 999))


def test_auth_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with _client(handler) as gh, pytest.raises(GitHubError):
        gh.fetch_pull_request(PRRef("o", "r", 1))


# --- retry/backoff (RC1-120) ----------------------------------------------

def _retrying_client(handler) -> GitHubClient:
    """A client that retries but never actually sleeps (offline + instant)."""
    transport = httpx.MockTransport(handler)
    return GitHubClient(
        token="t", client=httpx.Client(transport=transport), sleep=lambda _d: None
    )


def test_get_retries_transient_5xx_then_succeeds():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/42") and not path.endswith("/files"):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503)  # transient
            return httpx.Response(200, json=_pr_json())
        if path.endswith("/files"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    with _retrying_client(handler) as gh:
        pr = gh.fetch_pull_request(PRRef("octocat", "Hello-World", 42))

    assert attempts["n"] == 3
    assert pr.title == "Add feature"


def test_get_gives_up_after_max_attempts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with _retrying_client(handler) as gh, pytest.raises(GitHubError):
        gh.fetch_pull_request(PRRef("o", "r", 42))
    # Default github_max_attempts = 4 (1 + 3 retries).
    assert calls["n"] == 4


def test_write_retries_rate_limited_403():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                403, headers={"Retry-After": "1"}, json={"message": "rate limited"}
            )
        return httpx.Response(200, json={"id": 99})

    with _retrying_client(handler) as gh:
        review = gh.create_review(PRRef("o", "r", 42), body="b", event="COMMENT")

    assert attempts["n"] == 2
    assert review["id"] == 99
