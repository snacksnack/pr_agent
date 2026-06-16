"""GitHub PR ingestion (RC1-108).

Fetch a pull request's metadata, changed files, and per-file patches from the
GitHub REST API and return the normalized models in ``app.models``.

The dry-run CLI (RC1-113) uses PAT auth; the webhook service (RC1-115/116) will
reuse this same client with a short-lived installation token. Either way the
output is identical, so everything downstream is auth-agnostic.
"""
from __future__ import annotations

import base64
import re
import time
from typing import Callable
from urllib.parse import quote

import httpx

from app.config import settings
from app.models import ChangedFile, PRRef, PullRequest
from app.retry import request_with_retry

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub caps the files endpoint at 100 per page (and 3000 files total).
FILES_PER_PAGE = 100
# Our own safety cap so a pathological PR can't blow up cost/runtime.
DEFAULT_MAX_FILES = 300
DEFAULT_TIMEOUT = 30.0

_SPEC_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/#\s]+)#(?P<number>\d+)$")


class GitHubError(RuntimeError):
    """Raised for GitHub API failures with an actionable message.

    ``status`` carries the HTTP status code when the failure is an API response
    (vs. a transport error), so callers can react to specific codes — e.g. the
    review poster retries summary-only on a 422 from a bad inline anchor.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def parse_pr_spec(spec: str) -> PRRef:
    """Parse an ``owner/repo#123`` string into a :class:`PRRef`."""
    match = _SPEC_RE.match(spec.strip())
    if not match:
        raise ValueError(
            f"Invalid PR spec {spec!r}; expected the form 'owner/repo#123'"
        )
    return PRRef(
        owner=match["owner"],
        repo=match["repo"],
        number=int(match["number"]),
    )


class GitHubClient:
    """Thin GitHub REST client for reading pull requests.

    Pass a token explicitly, or let it fall back to ``settings.github_token``.
    A pre-built ``httpx.Client`` can be injected (used by tests to mock the API).
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = GITHUB_API,
        client: httpx.Client | None = None,
        max_attempts: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token if token is not None else settings.github_token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._owns_client = client is None
        self._max_attempts = max_attempts or settings.github_max_attempts
        self._sleep = sleep

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- internals -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            resp = request_with_retry(
                lambda: self._client.get(url, headers=self._headers(), params=params),
                max_attempts=self._max_attempts,
                sleep=self._sleep,
                describe=f"GET {url}",
            )
        except httpx.HTTPError as exc:  # network/timeout, retries exhausted
            raise GitHubError(f"Request to {url} failed: {exc}") from exc

        if resp.status_code == 404:
            raise GitHubError(
                f"Not found: {url}. Check the owner/repo/number and that the "
                "token can access this repository."
            )
        if resp.status_code in (401, 403):
            raise GitHubError(
                f"Authentication or permission error ({resp.status_code}) for "
                f"{url}. Check GITHUB_TOKEN and its scopes."
            )
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub returned {resp.status_code} for {url}")
        return resp

    def _send(self, method: str, path: str, json: dict) -> httpx.Response:
        """POST/PATCH/PUT helper for write endpoints (reviews, comments)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            resp = request_with_retry(
                lambda: self._client.request(method, url, headers=self._headers(), json=json),
                max_attempts=self._max_attempts,
                sleep=self._sleep,
                describe=f"{method} {url}",
            )
        except httpx.HTTPError as exc:  # network/timeout, retries exhausted
            raise GitHubError(f"Request to {url} failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise GitHubError(
                f"Authentication or permission error ({resp.status_code}) for "
                f"{url}. The token needs pull-request write access.",
                status=resp.status_code,
            )
        if resp.status_code >= 400:
            # Surface GitHub's message (e.g. why a review/comment was rejected).
            detail = ""
            try:
                detail = f": {resp.json().get('message', '')}"
            except ValueError:
                pass
            raise GitHubError(
                f"GitHub returned {resp.status_code} for {url}{detail}",
                status=resp.status_code,
            )
        return resp

    def _post(self, path: str, json: dict) -> httpx.Response:
        return self._send("POST", path, json)

    def _patch(self, path: str, json: dict) -> httpx.Response:
        return self._send("PATCH", path, json)

    def _put(self, path: str, json: dict) -> httpx.Response:
        return self._send("PUT", path, json)

    def _get_all(self, path: str, *, max_items: int = 500) -> list[dict]:
        """Fetch a paginated list endpoint into one list (bounded)."""
        items: list[dict] = []
        page = 1
        while True:
            batch = self._get(path, params={"per_page": 100, "page": page}).json()
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 100 or len(items) >= max_items:
                break
            page += 1
        return items

    def _fetch_files(
        self, ref: PRRef, *, max_files: int
    ) -> tuple[list[ChangedFile], bool]:
        files: list[ChangedFile] = []
        truncated = False
        page = 1
        while True:
            batch = self._get(
                f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/files",
                params={"per_page": FILES_PER_PAGE, "page": page},
            ).json()
            if not batch:
                break
            for item in batch:
                files.append(_parse_file(item))
                if len(files) >= max_files:
                    truncated = True
                    break
            # Stop on the last (short) page or once we've hit the cap.
            if truncated or len(batch) < FILES_PER_PAGE:
                break
            page += 1
        return files, truncated

    # -- public API ------------------------------------------------------

    def fetch_pull_request(
        self, ref: PRRef, *, max_files: int = DEFAULT_MAX_FILES
    ) -> PullRequest:
        """Fetch PR metadata + changed files and return a :class:`PullRequest`."""
        data = self._get(
            f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
        ).json()
        files, truncated = self._fetch_files(ref, max_files=max_files)

        base = data.get("base") or {}
        head = data.get("head") or {}
        user = data.get("user") or {}
        return PullRequest(
            ref=ref,
            title=data.get("title") or "",
            body=data.get("body") or "",
            state=data.get("state") or "",
            author=user.get("login"),
            base_ref=base.get("ref") or "",
            head_ref=head.get("ref") or "",
            base_sha=base.get("sha") or "",
            head_sha=head.get("sha") or "",
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            changed_files_count=int(data.get("changed_files") or 0),
            files=files,
            truncated_files=truncated,
            html_url=data.get("html_url"),
        )

    def get_file_text(
        self, ref: PRRef, path: str, *, git_ref: str | None = None
    ) -> str | None:
        """Fetch a file's text content at a git ref via the Contents API.

        Used by the live review path, which has no local checkout: it sources a
        changed file's bytes at the PR head (``git_ref``) so deterministic
        checks (e.g. the n8n cost check) can run on real repos.

        Returns ``None`` — never raises — when the file can't be turned into
        usable text: it's absent at the ref, it's a directory, or GitHub omits
        the inline content (binary, or larger than the Contents API's ~1 MB
        cap). Callers treat ``None`` as "skip this file", so a missing or
        oversized file never fails the review.
        """
        # Encode the path but keep its slashes so subdirectories survive.
        encoded = quote(path, safe="/")
        params = {"ref": git_ref} if git_ref else None
        try:
            resp = self._get(
                f"/repos/{ref.owner}/{ref.repo}/contents/{encoded}", params=params
            )
        except GitHubError:
            return None  # 404/permission/transport — best-effort, just skip it
        data = resp.json()
        # A directory yields a list; a file yields a dict with base64 content.
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        content = data.get("content")
        if not isinstance(content, str):
            return None
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None  # undecodable bytes — not text we can check

    def create_review(
        self,
        ref: PRRef,
        *,
        body: str,
        event: str,
        comments: list[dict] | None = None,
        commit_id: str | None = None,
    ) -> dict:
        """Create a single PR review (summary + optional inline comments).

        Posts to the Reviews API so all comments land as one review object
        rather than a scatter of standalone comments. ``event`` is ``COMMENT``
        or ``REQUEST_CHANGES`` (see :mod:`app.verdict`); ``comments`` are inline
        anchors (``path``/``line``/``side``/``body``); ``commit_id`` pins the
        review to the head SHA so anchors resolve against the reviewed commit.
        """
        payload: dict[str, object] = {"body": body, "event": event}
        if commit_id:
            payload["commit_id"] = commit_id
        if comments:
            payload["comments"] = comments
        resp = self._post(
            f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews", payload
        )
        return resp.json()

    # -- issue comments (the upserted review summary lives here) ---------

    def list_issue_comments(self, ref: PRRef) -> list[dict]:
        """All issue comments on the PR (a PR is an issue for the comments API)."""
        return self._get_all(
            f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments"
        )

    def create_issue_comment(self, ref: PRRef, body: str) -> dict:
        return self._post(
            f"/repos/{ref.owner}/{ref.repo}/issues/{ref.number}/comments",
            {"body": body},
        ).json()

    def update_issue_comment(self, ref: PRRef, comment_id: int, body: str) -> dict:
        return self._patch(
            f"/repos/{ref.owner}/{ref.repo}/issues/comments/{comment_id}",
            {"body": body},
        ).json()

    # -- review comments + reviews (for dedup + supersede) --------------

    def list_review_comments(self, ref: PRRef) -> list[dict]:
        """Inline review comments already on the PR (used to skip duplicates)."""
        return self._get_all(
            f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments"
        )

    def list_reviews(self, ref: PRRef) -> list[dict]:
        return self._get_all(
            f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews"
        )

    def dismiss_review(self, ref: PRRef, review_id: int, message: str) -> dict:
        """Dismiss a prior review (only valid for CHANGES_REQUESTED / APPROVED)."""
        return self._put(
            f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews/{review_id}/dismissals",
            {"message": message, "event": "DISMISS"},
        ).json()


def _parse_file(item: dict) -> ChangedFile:
    return ChangedFile(
        filename=item.get("filename") or "",
        status=item.get("status") or "",
        additions=int(item.get("additions") or 0),
        deletions=int(item.get("deletions") or 0),
        changes=int(item.get("changes") or 0),
        patch=item.get("patch"),  # absent for binary / oversized diffs
        previous_filename=item.get("previous_filename"),
        sha=item.get("sha"),
    )


def fetch_pull_request(
    owner: str,
    repo: str,
    number: int,
    *,
    token: str | None = None,
    max_files: int = DEFAULT_MAX_FILES,
) -> PullRequest:
    """Convenience wrapper: fetch a PR by its parts in one call."""
    ref = PRRef(owner=owner, repo=repo, number=number)
    with GitHubClient(token=token) as gh:
        return gh.fetch_pull_request(ref, max_files=max_files)
