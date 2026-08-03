"""Protocol-level tests for GitHub App auth (SPEC §6, `werft/github/auth.py`):
JWT minting, attenuated installation tokens (cached, re-minted near expiry),
and revoke's never-raise contract — all against `httpx.MockTransport`.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from werft.domain.errors import PermanentError
from werft.github.auth import (
    ADMIN_PERMISSIONS,
    MANAGER_PERMISSIONS,
    AppAuth,
    InstallationToken,
)

CLIENT_ID = "Iv1.testclientid1234"
OWNER = "acme"
REPO = "widgets"
API_URL = "https://api.github.test"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
PUBLIC_KEY_PEM = (
    _PRIVATE_KEY.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)


def _mint_response(token: str, *, minutes_valid: int = 60) -> httpx.Response:
    expiry = datetime.now(UTC) + timedelta(minutes=minutes_valid)
    return httpx.Response(
        201, json={"token": token, "expires_at": expiry.isoformat().replace("+00:00", "Z")}
    )


def auth_with(handler) -> AppAuth:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AppAuth(client, client_id=CLIENT_ID, private_key_pem=PRIVATE_KEY_PEM, api_url=API_URL)


def _decode(token: str) -> dict:
    return jwt.decode(
        token, PUBLIC_KEY_PEM, algorithms=["RS256"], options={"verify_aud": False}, issuer=CLIENT_ID
    )


def test_app_jwt_is_valid_rs256_with_correct_claims():
    auth = auth_with(lambda r: httpx.Response(500))
    token = auth.app_jwt()

    claims = _decode(token)
    assert claims["iss"] == CLIENT_ID
    assert claims["exp"] - claims["iat"] <= 600

    now = datetime.now(UTC)
    iat = datetime.fromtimestamp(claims["iat"], tz=UTC)
    # iat = now - 60s: allow slack for test execution time.
    assert timedelta(seconds=55) <= now - iat <= timedelta(seconds=65)


async def test_installation_id_404_raises_permanent_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{OWNER}/{REPO}/installation"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(404, json={"message": "not found"})

    auth = auth_with(handler)
    with pytest.raises(PermanentError):
        await auth.installation_id(OWNER, REPO)


async def test_installation_id_returns_the_id_on_success():
    auth = auth_with(lambda r: httpx.Response(200, json={"id": 4242}))
    assert await auth.installation_id(OWNER, REPO) == 4242


async def test_token_for_uses_jwt_on_both_calls_and_posts_attenuation_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == f"/repos/{OWNER}/{REPO}/installation":
            return httpx.Response(200, json={"id": 4242})
        assert request.url.path == "/app/installations/4242/access_tokens"
        return _mint_response("ghs_abc123")

    auth = auth_with(handler)
    token = await auth.token_for(OWNER, REPO, MANAGER_PERMISSIONS)

    assert isinstance(token, InstallationToken)
    assert token.token == "ghs_abc123"
    assert len(seen) == 2
    for request in seen:
        auth_header = request.headers["authorization"]
        assert auth_header.startswith("Bearer ")
        # Both calls are authed with the App JWT, never an installation token.
        assert _decode(auth_header.removeprefix("Bearer "))["iss"] == CLIENT_ID

    body = json.loads(seen[1].content)
    assert body == {"permissions": MANAGER_PERMISSIONS, "repositories": [REPO]}


async def test_token_for_caches_and_mints_once_for_two_calls():
    mint_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mint_calls
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        mint_calls += 1
        return _mint_response("ghs_cached")

    auth = auth_with(handler)
    first = await auth.token_for(OWNER, REPO, MANAGER_PERMISSIONS)
    second = await auth.token_for(OWNER, REPO, MANAGER_PERMISSIONS)

    assert mint_calls == 1
    assert first.token == second.token == "ghs_cached"


async def test_token_for_remints_when_expiring_within_five_minutes():
    mint_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mint_calls
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        mint_calls += 1
        # Under the 5-minute renewal margin: the cache must not reuse this.
        return _mint_response(f"ghs_{mint_calls}", minutes_valid=2)

    auth = auth_with(handler)
    first = await auth.token_for(OWNER, REPO, MANAGER_PERMISSIONS)
    second = await auth.token_for(OWNER, REPO, MANAGER_PERMISSIONS)

    assert mint_calls == 2
    assert first.token != second.token


async def test_distinct_permission_sets_get_distinct_tokens():
    mint_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mint_calls
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 1})
        mint_calls += 1
        return _mint_response(f"ghs_{mint_calls}")

    auth = auth_with(handler)
    manager_token = await auth.token_for(OWNER, REPO, MANAGER_PERMISSIONS)
    admin_token = await auth.token_for(OWNER, REPO, ADMIN_PERMISSIONS)

    assert mint_calls == 2
    assert manager_token.token != admin_token.token


async def test_revoke_sends_delete_with_the_installation_token_not_the_jwt():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(204)

    auth = auth_with(handler)
    ok = await auth.revoke("ghs_the_token")

    assert ok is True
    assert seen == {
        "method": "DELETE",
        "path": "/installation/token",
        "auth": "Bearer ghs_the_token",
    }


async def test_revoke_returns_false_without_raising_on_500():
    auth = auth_with(lambda r: httpx.Response(500, json={"message": "boom"}))
    assert await auth.revoke("ghs_x") is False


async def test_revoke_returns_false_without_raising_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    auth = auth_with(handler)
    assert await auth.revoke("ghs_x") is False
