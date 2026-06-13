"""Tests for GitHub PR ingestion (RC1-108).

The GitHub API is mocked with ``httpx.MockTransport`` so these run offline.
"""
from __future__ import annotations

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

    with _client(handler) as gh:
        with pytest.raises(GitHubError):
            gh.fetch_pull_request(PRRef("o", "r", 999))


def test_auth_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with _client(handler) as gh:
        with pytest.raises(GitHubError):
            gh.fetch_pull_request(PRRef("o", "r", 1))
