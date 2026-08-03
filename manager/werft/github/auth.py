"""GitHub App authentication (SPEC §6; plan decision on `administration:write`).

Three permission ceilings, not one, because a single App-wide grant would hand
every installation token `administration:write` — the permission `PUT
/branches/{branch}/protection` needs for the automatic bootstrap flip (SPEC
§3.1), but one that must never ride along on a run's attenuated token or a
day-to-day ops call. `MANAGER_PERMISSIONS` is what the manager mints for
itself; `ADMIN_PERMISSIONS` is minted transiently, used for one protection
call, and revoked immediately (`AppAuth.revoke`; `app.py`'s
`TransientAdminOps` is the caller that actually does this); `RUNNER_PERMISSIONS`
is T7's per-run dispatch grant.

Installation tokens are cached per `(owner, repo, permission set)` — minting is
a JWT-authed round trip plus a write against GitHub's secondary rate limit, and
distinct permission sets are, by GitHub's own attenuation model, distinct
tokens that cannot be merged into one cache entry. `token_for`'s
`transient=True` kwarg bypasses that cache entirely (no read, no write) —
`TransientAdminOps`'s doctrine that an admin-scoped token is minted for one
call and revoked immediately means it must never be read back out of the
shared cache by some other caller, nor left occupying that cache's slot
after this caller has already asked to revoke it.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from werft.domain.errors import PermanentError

#: The manager's own day-to-day ceiling: everything the poller/orchestrator
#: needs, deliberately excluding `administration` (see module docstring).
MANAGER_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "metadata": "read",
}

#: Minted transiently, for one branch-protection call only, then revoked.
ADMIN_PERMISSIONS: dict[str, str] = {"administration": "write"}

#: T7's per-run dispatch grant: push access to the one repo, nothing else.
RUNNER_PERMISSIONS: dict[str, str] = {"contents": "write"}

#: GitHub's own guidance: back-date `iat` to absorb clock drift between the
#: manager and GitHub's servers.
_JWT_IAT_SKEW_SECONDS = 60

#: Comfortably under GitHub's 10-minute ceiling on App JWT lifetime.
_JWT_TTL_SECONDS = 540

#: Re-mint an installation token once less than this much validity remains,
#: so a long-running call never has its token expire mid-flight.
_RENEW_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class InstallationToken:
    """An attenuated, short-lived (1 h) installation access token."""

    token: str
    expires_at: datetime


def _cache_key(owner: str, repo: str, permissions: dict[str, str]) -> tuple[str, str, frozenset]:
    return (owner, repo, frozenset(permissions.items()))


class AppAuth:
    """Mints and caches GitHub App JWTs and attenuated installation tokens.

    One instance per manager process, sharing the process's `httpx.AsyncClient`.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        client_id: str,
        private_key_pem: str,
        api_url: str,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._private_key_pem = private_key_pem
        self._api_url = api_url.rstrip("/")
        self._cache: dict[tuple[str, str, frozenset], InstallationToken] = {}

    def app_jwt(self) -> str:
        """A fresh RS256 App JWT: `iss` = client ID, `iat` back-dated 60 s."""
        now = int(time.time())
        payload = {
            "iat": now - _JWT_IAT_SKEW_SECONDS,
            "exp": now + _JWT_TTL_SECONDS,
            "iss": self._client_id,
        }
        return jwt.encode(payload, self._private_key_pem, algorithm="RS256")

    async def installation_id(self, owner: str, repo: str) -> int:
        """The installation id for `owner/repo`. A 404 means the App is not
        installed there — not retryable, so it parks the caller (SPEC §6)."""
        response = await self._http.get(
            f"{self._api_url}/repos/{owner}/{repo}/installation",
            headers={"Authorization": f"Bearer {self.app_jwt()}"},
        )
        if response.status_code == 404:
            raise PermanentError(f"GitHub App is not installed on {owner}/{repo}")
        response.raise_for_status()
        return response.json()["id"]

    async def token_for(
        self, owner: str, repo: str, permissions: dict[str, str], *, transient: bool = False
    ) -> InstallationToken:
        """An installation token attenuated to `permissions`, scoped to `repo`.

        Cached per `(owner, repo, permission set)`; re-minted once fewer than
        five minutes of validity remain.

        `transient=True` bypasses the cache entirely — no read, no write.
        This is `TransientAdminOps` (`app.py`)'s doctrine: an admin-scoped
        token is minted for exactly one protection call and revoked
        immediately after, so it must never be handed back to some other,
        unrelated caller out of the shared cache, and must never itself
        occupy that cache slot for the next legitimate admin mint to find
        (already-revoked) stale.
        """
        key = _cache_key(owner, repo, permissions)
        if not transient:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at - datetime.now(UTC) > _RENEW_MARGIN:
                return cached

        installation_id = await self.installation_id(owner, repo)
        response = await self._http.post(
            f"{self._api_url}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self.app_jwt()}"},
            json={"permissions": permissions, "repositories": [repo]},
        )
        response.raise_for_status()
        data = response.json()
        token = InstallationToken(
            token=data["token"], expires_at=datetime.fromisoformat(data["expires_at"])
        )
        if not transient:
            self._cache[key] = token
        return token

    async def revoke(self, token: str) -> bool:
        """Revoke an installation token at teardown. Never raises: a failed
        revoke must not block or fail the caller's own teardown path — the
        token still expires on its own within the hour.

        Evicts the token from `_cache` regardless of whether the DELETE call
        itself succeeds, so a subsequent `token_for` for the same key never
        hands back a token this caller has already asked to revoke.
        """
        for key, cached in list(self._cache.items()):
            if cached.token == token:
                del self._cache[key]
        try:
            response = await self._http.delete(
                f"{self._api_url}/installation/token",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            return False
        return response.is_success
