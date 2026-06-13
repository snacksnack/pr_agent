"""GitHub PR ingestion (RC1-108).

Fetch a pull request's metadata, changed files, and per-file patches from the
GitHub REST API and return the normalized models in ``app.models``.

The dry-run CLI (RC1-113) uses PAT auth; the webhook service (RC1-115/116) will
reuse this same client with a short-lived installation token. Either way the
output is identical, so everything downstream is auth-agnostic.
"""
from __future__ import annotations

import re

import httpx

from app.config import settings
from app.models import ChangedFile, PRRef, PullRequest

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub caps the files endpoint at 100 per page (and 3000 files total).
FILES_PER_PAGE = 100
# Our own safety cap so a pathological PR can't blow up cost/runtime.
DEFAULT_MAX_FILES = 300
DEFAULT_TIMEOUT = 30.0

_SPEC_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/#\s]+)#(?P<number>\d+)$")


class GitHubError(RuntimeError):
    """Raised for GitHub API failures with an actionable message."""


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
    ) -> None:
        self._token = token if token is not None else settings.github_token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._owns_client = client is None

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
            resp = self._client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:  # network/timeout
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
