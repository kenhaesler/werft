"""Static bearer auth for the operator API (SPEC §9: "Static bearer token,
Tailscale-only, TLS via `tailscale cert`" — Tailscale is the network
perimeter; this dependency is the one remaining application-layer check
once a request is already inside the tailnet).

The composition root (`app.py`) reads the token exactly once, at startup,
from the file mounted at `Settings.api_token_file` (SPEC §10: "Secrets are
file mounts, never env") and closes over the result here — no per-request
file I/O, and no route ever touches `Settings` directly.
"""

import secrets
from collections.abc import Callable

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def make_require_token(token: str | None) -> Callable[..., None]:
    """Build a FastAPI dependency bound to one fixed token.

    `token is None` means the secret was never configured — an empty
    `api_token_file` or a path that doesn't resolve to a file, both
    resolved once by the composition root before this is called. That case
    always raises 403: fail closed, never fall back to "auth disabled".

    A configured token still requires a well-formed
    `Authorization: Bearer <token>` header; a missing header, a wrong
    scheme, or a credential that doesn't match via `secrets.compare_digest`
    is 401 — with a `WWW-Authenticate: Bearer` header, per RFC 9110.

    Parsing is delegated to FastAPI's own `HTTPBearer(auto_error=False)`
    sub-dependency rather than hand-rolled `str.startswith`: the scheme
    name in `Authorization` is case-insensitive (RFC 9110 §11.1), which a
    literal `"Bearer "` prefix match gets wrong, and `HTTPBearer` also
    documents `/api/v1` in OpenAPI's `securitySchemes` for free.
    """

    def require_token(
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008 - FastAPI's DI pattern
    ) -> None:
        if token is None:
            raise HTTPException(status_code=403, detail="api token not configured")

        if creds is None or creds.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="missing or malformed authorization header",
                headers=_UNAUTHORIZED_HEADERS,
            )

        credential = creds.credentials
        # `secrets.compare_digest` requires ASCII-only `str` operands;
        # Starlette decodes headers as latin-1, so a non-ASCII credential
        # (or a non-ASCII byte in the token file) raises `TypeError` rather
        # than comparing false. Comparing the raw bytes instead sidesteps
        # that constraint entirely while staying constant-time.
        credential_bytes = credential.encode("utf-8", "surrogateescape")
        token_bytes = token.encode("utf-8", "surrogateescape")
        if not credential or not secrets.compare_digest(credential_bytes, token_bytes):
            raise HTTPException(
                status_code=401, detail="invalid token", headers=_UNAUTHORIZED_HEADERS
            )

    return require_token
