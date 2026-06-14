"""Tests for GitHub App authentication (RC1-115).

Fully offline: an ephemeral RSA keypair is generated in-process to sign/verify
JWTs, and the GitHub API is mocked with ``httpx.MockTransport``. Time is injected
so JWT claims and token-cache expiry are deterministic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import (
    EXPIRY_SKEW_SECONDS,
    GitHubAppAuth,
    GitHubAuthError,
    InstallationToken,
    build_app_jwt,
)

# --- key material (generated once for the module) -------------------------

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
PUBLIC_PEM = (
    _KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)

APP_ID = "123456"
NOW = 1_700_000_000  # fixed epoch for deterministic JWT claims


# --- JWT signing ----------------------------------------------------------

def test_build_app_jwt_claims_and_signature():
    token = build_app_jwt(APP_ID, PRIVATE_PEM, now=NOW)
    decoded = pyjwt.decode(
        token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert decoded["iss"] == APP_ID
    # iat back-dated 60s; exp under GitHub's 10-minute ceiling.
    assert decoded["iat"] == NOW - 60
    assert decoded["exp"] == NOW + 9 * 60
    assert decoded["exp"] - decoded["iat"] <= 10 * 60


def test_build_app_jwt_accepts_escaped_newline_pem():
    escaped = PRIVATE_PEM.replace("\n", "\\n")
    token = build_app_jwt(APP_ID, escaped, now=NOW)
    decoded = pyjwt.decode(
        token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert decoded["iss"] == APP_ID


def test_build_app_jwt_bad_key_raises():
    with pytest.raises(GitHubAuthError):
        build_app_jwt(APP_ID, "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----", now=NOW)


# --- InstallationToken freshness -----------------------------------------

def test_installation_token_is_fresh():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fresh = InstallationToken("t", now + timedelta(hours=1))
    near = InstallationToken("t", now + timedelta(seconds=EXPIRY_SKEW_SECONDS - 1))
    assert fresh.is_fresh(now=now) is True
    assert near.is_fresh(now=now) is False


# --- harness --------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Routes the App-auth endpoints and counts calls, validating auth headers."""

    def __init__(self, *, token_ttl: timedelta = timedelta(hours=1)):
        self.token_ttl = token_ttl
        self.install_calls = 0
        self.mint_calls = 0
        self.mint_bodies: list[dict] = []
        self.install_id = 555

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        auth = request.headers.get("Authorization", "")

        if path.endswith("/installation"):
            self.install_calls += 1
            assert auth.startswith("Bearer ")  # app JWT
            return httpx.Response(200, json={"id": self.install_id})

        if path.endswith("/access_tokens"):
            self.mint_calls += 1
            assert f"/app/installations/{self.install_id}/" in path
            import json as _json

            self.mint_bodies.append(_json.loads(request.content or b"{}"))
            expires = datetime.now(timezone.utc) + self.token_ttl
            return httpx.Response(
                201, json={"token": f"ghs_minted_{self.mint_calls}", "expires_at": _iso(expires)}
            )

        # Used by the client_for_repo end-to-end check.
        if path.endswith("/pulls/7") and not path.endswith("/files"):
            assert auth == "Bearer ghs_minted_1"
            return httpx.Response(200, json={"title": "T", "base": {}, "head": {}, "user": {}})
        if path.endswith("/pulls/7/files"):
            assert auth == "Bearer ghs_minted_1"
            return httpx.Response(200, json=[])

        return httpx.Response(404, json={"message": "unexpected", "path": path})


def _auth(fake: FakeGitHub, *, time_fn=lambda: NOW) -> GitHubAppAuth:
    transport = httpx.MockTransport(fake.handler)
    return GitHubAppAuth(
        APP_ID,
        PRIVATE_PEM,
        client=httpx.Client(transport=transport),
        time_fn=time_fn,
    )


# --- installation discovery + caching ------------------------------------

def test_installation_id_resolved_and_cached():
    fake = FakeGitHub()
    with _auth(fake) as auth:
        assert auth.installation_id_for_repo("octo", "repo") == 555
        assert auth.installation_id_for_repo("octo", "repo") == 555  # cached
    assert fake.install_calls == 1


# --- token minting + scope + caching -------------------------------------

def test_token_for_repo_is_scoped_to_that_repo():
    fake = FakeGitHub()
    with _auth(fake) as auth:
        tok = auth.token_for_repo("octo", "repo")
    assert tok.token == "ghs_minted_1"
    assert fake.mint_bodies[-1] == {"repositories": ["repo"]}  # bare name, scoped


def test_fresh_token_is_reused_not_reminted():
    fake = FakeGitHub(token_ttl=timedelta(hours=1))
    with _auth(fake) as auth:
        a = auth.token_for_installation(555)
        b = auth.token_for_installation(555)
    assert a.token == b.token
    assert fake.mint_calls == 1  # second call served from cache


def test_near_expiry_token_is_reminted():
    # TTL inside the skew window => never "fresh" => re-mint every call.
    fake = FakeGitHub(token_ttl=timedelta(seconds=EXPIRY_SKEW_SECONDS - 5))
    with _auth(fake) as auth:
        a = auth.token_for_installation(555)
        b = auth.token_for_installation(555)
    assert fake.mint_calls == 2
    assert (a.token, b.token) == ("ghs_minted_1", "ghs_minted_2")


def test_distinct_scopes_cache_separately():
    fake = FakeGitHub()
    with _auth(fake) as auth:
        auth.token_for_installation(555, repositories=["a"])
        auth.token_for_installation(555, repositories=["b"])
        auth.token_for_installation(555, repositories=["a"])  # cached
    assert fake.mint_calls == 2


# --- ready-to-use client --------------------------------------------------

def test_client_for_repo_uses_installation_token():
    from app.models import PRRef

    fake = FakeGitHub()
    with _auth(fake) as auth:
        gh = auth.client_for_repo("octo", "repo")
        pr = gh.fetch_pull_request(PRRef("octo", "repo", 7))
    assert pr.title == "T"
    # one installation lookup + one mint, token reused for both PR calls
    assert fake.install_calls == 1 and fake.mint_calls == 1


# --- error handling -------------------------------------------------------

def test_missing_credentials_raise():
    auth = GitHubAppAuth("", "", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(GitHubAuthError):
        auth.app_jwt()


def test_installation_lookup_404_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not installed"})

    auth = GitHubAppAuth(
        APP_ID, PRIVATE_PEM, client=httpx.Client(transport=httpx.MockTransport(handler)), time_fn=lambda: NOW
    )
    with pytest.raises(GitHubAuthError):
        auth.installation_id_for_repo("octo", "repo")


def test_mint_401_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(401, json={"message": "bad jwt"})
        return httpx.Response(200, json={"id": 1})

    auth = GitHubAppAuth(
        APP_ID, PRIVATE_PEM, client=httpx.Client(transport=httpx.MockTransport(handler)), time_fn=lambda: NOW
    )
    with pytest.raises(GitHubAuthError):
        auth.token_for_installation(1)
