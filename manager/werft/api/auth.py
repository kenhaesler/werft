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

from fastapi import HTTPException, Request

_BEARER_PREFIX = "Bearer "


def make_require_token(token: str | None) -> Callable[[Request], None]:
    """Build a FastAPI dependency bound to one fixed token.

    `token is None` means the secret was never configured — an empty
    `api_token_file` or a path that doesn't resolve to a file, both
    resolved once by the composition root before this is called. That case
    always raises 403: fail closed, never fall back to "auth disabled".

    A configured token still requires a well-formed
    `Authorization: Bearer <token>` header; a missing header, a wrong
    scheme, or a credential that doesn't match via `secrets.compare_digest`
    (constant-time, so a wrong guess can't be timed against the real value)
    is 401.
    """

    def require_token(request: Request) -> None:
        if token is None:
            raise HTTPException(status_code=403, detail="api token not configured")

        header = request.headers.get("Authorization")
        if header is None or not header.startswith(_BEARER_PREFIX):
            raise HTTPException(status_code=401, detail="missing or malformed authorization header")

        credential = header[len(_BEARER_PREFIX) :]
        if not credential or not secrets.compare_digest(credential, token):
            raise HTTPException(status_code=401, detail="invalid token")

    return require_token
