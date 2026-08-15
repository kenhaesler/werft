"""`api/auth.py`'s `make_require_token` in isolation (SPEC §9: "Static
bearer token, Tailscale-only, TLS via `tailscale cert`" — the token check
is the one remaining application-layer control once a request is inside
the tailnet).

Tiny FastAPI apps built inline, one per configured token, so each test
exercises the real dependency-injection path (`Depends(require_token)`)
rather than calling the inner function directly.
"""

import httpx
import pytest
from fastapi import Depends, FastAPI

from werft.api.auth import make_require_token


def _protected_app(token: str | None) -> FastAPI:
    app = FastAPI()
    require_token = make_require_token(token)

    @app.get("/protected", dependencies=[Depends(require_token)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    return app


HeaderArg = dict[str, str] | list[tuple[bytes, bytes]] | None


async def _get(app: FastAPI, headers: HeaderArg = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/protected", headers=headers)


async def test_unconfigured_token_is_403_even_with_a_header_present() -> None:
    """`token is None` (empty `api_token_file` or a missing file, per the
    composition root) must fail closed regardless of what the caller
    sends — an unconfigured secret is never treated as "auth disabled"."""
    resp = await _get(_protected_app(None), headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "api token not configured"


async def test_missing_header_is_401() -> None:
    resp = await _get(_protected_app("s3cr3t"))
    assert resp.status_code == 401


async def test_malformed_header_no_bearer_scheme_is_401() -> None:
    resp = await _get(_protected_app("s3cr3t"), headers={"Authorization": "s3cr3t"})
    assert resp.status_code == 401


async def test_wrong_scheme_is_401() -> None:
    resp = await _get(_protected_app("s3cr3t"), headers={"Authorization": "Basic s3cr3t"})
    assert resp.status_code == 401


async def test_mismatched_token_is_401() -> None:
    resp = await _get(_protected_app("s3cr3t"), headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


async def test_correct_token_passes() -> None:
    resp = await _get(_protected_app("s3cr3t"), headers={"Authorization": "Bearer s3cr3t"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.parametrize("header", ["Bearer", "Bearer "])
async def test_bearer_scheme_with_no_credential_is_401(header: str) -> None:
    resp = await _get(_protected_app("s3cr3t"), headers={"Authorization": header})
    assert resp.status_code == 401


async def test_lowercase_bearer_scheme_passes() -> None:
    """RFC 9110 §11.1: auth-scheme tokens are case-insensitive. A literal
    `"Bearer "`-prefix match (the pre-fix implementation) rejects a
    perfectly legal `Authorization: bearer <token>` header with 401 even
    though the credential is correct."""
    resp = await _get(_protected_app("s3cr3t"), headers={"Authorization": "bearer s3cr3t"})
    assert resp.status_code == 200


async def test_401_responses_carry_www_authenticate_bearer_header() -> None:
    resp = await _get(_protected_app("s3cr3t"), headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_non_ascii_credential_is_401_not_500() -> None:
    """`secrets.compare_digest` only accepts ASCII-only `str` operands;
    Starlette decodes header bytes as latin-1, so a non-ASCII credential
    (e.g. `café`) used to raise `TypeError` from inside the dependency,
    which FastAPI surfaces as a 500 rather than a 401.

    httpx's own `dict[str, str]` header encoding is ASCII-only (it raises
    `UnicodeEncodeError` before a request is even sent), so the non-ASCII
    byte has to go in as a raw header tuple — latin-1-encoded, matching
    exactly what Starlette decodes `Authorization` headers as on the wire.
    """
    headers = [(b"Authorization", "Bearer café".encode("latin-1"))]
    resp = await _get(_protected_app("s3cr3t"), headers=headers)
    assert resp.status_code == 401
