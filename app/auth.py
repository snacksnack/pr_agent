"""GitHub App authentication (RC1-115).

Turn the App's static credentials (App ID + private key) into the short-lived
tokens GitHub actually accepts on API calls:

    App private key --(sign RS256 JWT)--> app JWT
    app JWT         --(POST access_tokens)--> installation token (~1h)

The app JWT only authenticates *as the App* (used to discover installations and
mint tokens). All repo reads/writes use an **installation token**, which is
scoped to a single installation and — here — narrowed further to the one repo we
touch. Installation tokens are cached per (installation, repo-scope) until just
before expiry so a burst of calls re-uses one token instead of re-minting.

Auth-agnostic downstream: a minted installation token is just a bearer string,
so the same :class:`~app.github.GitHubClient` used by the dry-run CLI (PAT) works
unchanged with it (acceptance criterion 3).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from app.config import settings
from app.github import API_VERSION, DEFAULT_TIMEOUT, GITHUB_API, GitHubClient
from app.retry import request_with_retry

# GitHub caps the app JWT at 10 minutes. Stay safely under it, and back-date
# ``iat`` to absorb clock drift between us and GitHub (their docs recommend 60s).
JWT_LIFETIME_SECONDS = 9 * 60
JWT_BACKDATE_SECONDS = 60
# Treat an installation token as expired this many seconds early, so we never
# hand out a token that lapses mid-request.
EXPIRY_SKEW_SECONDS = 60

_TokenScope = tuple[str, ...]  # sorted "owner/repo" slugs, () = whole installation


class GitHubAuthError(RuntimeError):
    """Raised when App auth fails: missing creds, bad key, or a mint failure."""


def _normalize_pem(private_key: str) -> str:
    """Accept PEM keys stored with literal ``\\n`` escapes (common in env vars)."""
    key = private_key.strip()
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")
    return key


def build_app_jwt(
    app_id: str,
    private_key: str,
    *,
    now: float | None = None,
) -> str:
    """Sign a short-lived RS256 JWT that authenticates as the App itself.

    ``now`` (epoch seconds) is injectable for deterministic tests.
    """
    import jwt  # lazy: keep PyJWT out of the import path for the dry-run phase

    issued = int(now if now is not None else time.time())
    payload = {
        "iat": issued - JWT_BACKDATE_SECONDS,
        "exp": issued + JWT_LIFETIME_SECONDS,
        "iss": str(app_id),
    }
    try:
        return jwt.encode(payload, _normalize_pem(private_key), algorithm="RS256")
    except Exception as exc:  # bad/garbled key, wrong type, etc.
        raise GitHubAuthError(
            "Failed to sign the App JWT. Check GITHUB_APP_PRIVATE_KEY is the "
            "App's full PEM private key and GITHUB_APP_ID is set."
        ) from exc


@dataclass(frozen=True)
class InstallationToken:
    """A minted installation token and its absolute (tz-aware UTC) expiry."""

    token: str
    expires_at: datetime

    def is_fresh(
        self, *, skew: int = EXPIRY_SKEW_SECONDS, now: datetime | None = None
    ) -> bool:
        """True if the token is still safely usable (not within ``skew`` of expiry)."""
        moment = now or datetime.now(timezone.utc)
        return moment < self.expires_at - timedelta(seconds=skew)


def _parse_expiry(value: str | None) -> datetime:
    """Parse GitHub's ISO-8601 ``expires_at`` (e.g. ``2026-06-14T22:14:10Z``)."""
    if not value:
        # Be conservative: assume the documented 1-hour lifetime from now.
        return datetime.now(timezone.utc) + timedelta(hours=1)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class GitHubAppAuth:
    """Mints and caches installation tokens for the PR-review GitHub App.

    Credentials fall back to ``settings`` so the webhook service can construct
    this with no args. A pre-built ``httpx.Client`` can be injected for offline
    tests, as can ``time_fn`` to make JWT/expiry timing deterministic.
    """

    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        *,
        base_url: str = GITHUB_API,
        client: httpx.Client | None = None,
        time_fn=time.time,
        max_attempts: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._app_id = app_id if app_id is not None else settings.github_app_id
        self._private_key = (
            private_key if private_key is not None else settings.github_app_private_key
        )
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._owns_client = client is None
        self._time_fn = time_fn
        self._max_attempts = max_attempts or settings.github_max_attempts
        self._sleep = sleep
        # (installation_id, scope) -> InstallationToken
        self._token_cache: dict[tuple[int, _TokenScope], InstallationToken] = {}
        # "owner/repo" -> installation_id (stable; safe to memoize)
        self._install_cache: dict[str, int] = {}

    def __enter__(self) -> "GitHubAppAuth":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- internals -------------------------------------------------------

    def _require_creds(self) -> tuple[str, str]:
        if not self._app_id or not self._private_key:
            raise GitHubAuthError(
                "GitHub App credentials are not configured. Set GITHUB_APP_ID "
                "and GITHUB_APP_PRIVATE_KEY (the App's PEM private key)."
            )
        return self._app_id, self._private_key

    def app_jwt(self) -> str:
        """Build a fresh app JWT from the configured credentials."""
        app_id, private_key = self._require_creds()
        return build_app_jwt(app_id, private_key, now=self._time_fn())

    def _headers(self, bearer: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {bearer}",
        }

    def _request(
        self, method: str, path: str, *, bearer: str, json: dict | None = None
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            resp = request_with_retry(
                lambda: self._client.request(
                    method, url, headers=self._headers(bearer), json=json
                ),
                max_attempts=self._max_attempts,
                sleep=self._sleep,
                describe=f"{method} {url}",
            )
        except httpx.HTTPError as exc:  # network / timeout, retries exhausted
            raise GitHubAuthError(f"Request to {url} failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise GitHubAuthError(
                f"App auth rejected ({resp.status_code}) for {url}. Check the "
                "App ID / private key and that the App is installed with access "
                "to this repository."
            )
        if resp.status_code == 404:
            raise GitHubAuthError(
                f"Not found: {url}. The App may not be installed on this "
                "account/repo, or lacks access to it."
            )
        if resp.status_code >= 400:
            raise GitHubAuthError(f"GitHub returned {resp.status_code} for {url}")
        return resp

    # -- installation discovery -----------------------------------------

    def installation_id_for_repo(self, owner: str, repo: str) -> int:
        """Resolve the installation that grants the App access to ``owner/repo``."""
        slug = f"{owner}/{repo}"
        cached = self._install_cache.get(slug)
        if cached is not None:
            return cached
        data = self._request(
            "GET", f"/repos/{owner}/{repo}/installation", bearer=self.app_jwt()
        ).json()
        install_id = data.get("id")
        if not isinstance(install_id, int):
            raise GitHubAuthError(
                f"Unexpected installation response for {slug}: missing numeric id."
            )
        self._install_cache[slug] = install_id
        return install_id

    # -- token minting ---------------------------------------------------

    def _mint(
        self, installation_id: int, *, repositories: list[str] | None
    ) -> InstallationToken:
        body: dict[str, object] = {}
        if repositories:
            # The endpoint wants bare repo names, not "owner/repo" slugs.
            body["repositories"] = [r.split("/", 1)[-1] for r in repositories]
        data = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            bearer=self.app_jwt(),
            json=body or None,
        ).json()
        token = data.get("token")
        if not token:
            raise GitHubAuthError(
                f"Token mint for installation {installation_id} returned no token."
            )
        return InstallationToken(token=token, expires_at=_parse_expiry(data.get("expires_at")))

    def token_for_installation(
        self, installation_id: int, *, repositories: list[str] | None = None
    ) -> InstallationToken:
        """Return a cached-or-fresh installation token, optionally repo-scoped."""
        scope: _TokenScope = tuple(sorted(repositories or ()))
        key = (installation_id, scope)
        now = datetime.now(timezone.utc)
        cached = self._token_cache.get(key)
        if cached is not None and cached.is_fresh(now=now):
            return cached
        minted = self._mint(installation_id, repositories=list(scope) or None)
        self._token_cache[key] = minted
        return minted

    def token_for_repo(self, owner: str, repo: str) -> InstallationToken:
        """Mint (or reuse) a token scoped to just ``owner/repo``."""
        installation_id = self.installation_id_for_repo(owner, repo)
        return self.token_for_installation(
            installation_id, repositories=[f"{owner}/{repo}"]
        )

    # -- ready-to-use clients -------------------------------------------

    def client_for_repo(self, owner: str, repo: str) -> GitHubClient:
        """A :class:`GitHubClient` authed with a token scoped to ``owner/repo``.

        Shares this auth object's underlying ``httpx.Client``, so it does not own
        (and won't close) the transport — manage that via :meth:`close`.
        """
        token = self.token_for_repo(owner, repo).token
        return GitHubClient(
            token=token,
            base_url=self._base_url,
            client=self._client,
            max_attempts=self._max_attempts,
            sleep=self._sleep,
        )
