"""Thin typed GitHub REST wrapper (SPEC §6.2: "ETag conditional requests").

Every later GitHub operation (`github/ops.py`, orchestrator handlers) goes
through `GitHubClient` — it owns the three headers GitHub's API requires on
every call, the transient-vs-fatal error split callers key their retry
decision on (SPEC §6.2's "no inline sleeps": a `GitHubUnavailable` means
"leave state, the next tick retries" — never retried inline here), and the
in-memory ETag store that makes polling cheap (an authenticated 304 costs no
rate-limit unit).

Deliberately decoupled from `github/auth.py`: the constructor takes a generic
`token_provider` callable rather than an `AppAuth` instance, so this module
has no opinion on how tokens are minted or attenuated — composition happens
where the client is constructed (Task 8).
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from werft.domain.errors import WerftError

#: GitHub's documented API version header (SPEC's research pins).
_API_VERSION = "2022-11-28"

#: `GitHubUnavailable.status` for a transport-level failure (connect/timeout)
#: that never produced an HTTP response to read a real status code from.
_TRANSPORT_ERROR_STATUS = 0

#: Etag store key: the request path plus its params, order-independent.
_EtagKey = tuple[str, tuple[tuple[str, Any], ...]]


class GitHubApiError(WerftError):
    """A GitHub REST call returned a status outside the caller's `expect`."""

    def __init__(self, status: int, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after


class GitHubUnavailable(GitHubApiError):
    """The transient family: transport errors, any 5xx, and 403/429 carrying
    `retry-after` or `x-ratelimit-remaining: 0`. Callers leave state
    untouched and let the next tick retry."""


@dataclass(frozen=True)
class ConditionalResult:
    """The outcome of `get_conditional`. `data` is `None` iff `modified` is
    `False` — a 304 means GitHub sent nothing new to parse.

    `links` carries the response's parsed `Link` header (httpx's
    `Response.links`, keyed by `rel`) or `None` when there wasn't one: a
    caller that has to walk a paginated collection needs the `rel="next"`
    signal, and it is only visible on the response this method consumed.
    """

    modified: bool
    data: list | dict | None
    links: Mapping[str, Mapping[str, str]] | None = None


def _etag_key(path: str, params: Mapping[str, Any] | None) -> _EtagKey:
    return (path, tuple(sorted((params or {}).items())))


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])
    return response.text


def _map_error(response: httpx.Response) -> GitHubApiError:
    status = response.status_code
    message = _error_message(response)
    retry_after = _retry_after(response)
    rate_limited = status in (403, 429) and (
        retry_after is not None or response.headers.get("x-ratelimit-remaining") == "0"
    )
    if status >= 500 or rate_limited:
        return GitHubUnavailable(status, message, retry_after=retry_after)
    return GitHubApiError(status, message, retry_after=retry_after)


class GitHubClient:
    """Owns the process's `httpx.AsyncClient` calls to one GitHub API host.

    Two pieces of per-instance state, both in-memory and never persisted:

    - the ETag store (`get_conditional`) — a manager restart just means the
      next poll costs one real rate-limit unit instead of a free 304;
    - the `retry-after` cooldown (`_assert_not_cooling_down`) — while a
      rate-limit window GitHub named is still open, this client refuses to
      send at all rather than deepening the block. Because a
      `GitHubClient` is built per project (`app._ops_factory`), one
      project's secondary-rate-limit trip does not silence the others.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_url: str,
        token_provider: Callable[[], Awaitable[str]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._api_url = api_url.rstrip("/")
        self._token_provider = token_provider
        self._etags: dict[_EtagKey, str] = {}
        #: Monotonic deadline before which this client refuses to send
        #: (`_send`). Set from a rate-limited response's `retry-after`;
        #: monotonic specifically, so a wall-clock/NTP step can neither
        #: extend nor cancel the wait.
        self._cooldown_until: float | None = None
        self._clock = clock

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        expect: tuple[int, ...],
    ) -> httpx.Response:
        """Send one authenticated request; any status outside `expect` raises
        the mapped error (`GitHubUnavailable` for the transient family,
        `GitHubApiError` otherwise) carrying the response's JSON `message`."""
        return await self._send(
            method, path, json=json, params=params, expect=expect, extra_headers={}
        )

    async def get_conditional(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> ConditionalResult:
        """`GET path` with `If-None-Match` from the in-memory ETag store,
        keyed by `path` plus sorted `params`. A 304 is free (SPEC §6.2) and
        maps to `ConditionalResult(modified=False, data=None)`; a 200 stores
        the fresh ETag and returns the parsed body plus any `Link` header a
        paginating caller needs to keep walking."""
        key = _etag_key(path, params)
        etag = self._etags.get(key)
        extra_headers = {"If-None-Match": etag} if etag else {}
        response = await self._send(
            "GET", path, params=params, expect=(200, 304), extra_headers=extra_headers
        )
        if response.status_code == 304:
            return ConditionalResult(modified=False, data=None)
        new_etag = response.headers.get("etag")
        if new_etag:
            self._etags[key] = new_etag
        return ConditionalResult(modified=True, data=response.json(), links=response.links or None)

    def invalidate_conditional(self, path_prefix: str | None = None) -> None:
        """Forget stored ETags — all of them, or just those whose path
        starts with `path_prefix`.

        The ETag store and the caller's database transaction are two
        different commit domains: `get_conditional` advances the store the
        instant GitHub answers 200, but the rows a caller derives from that
        body only become durable when *its* transaction commits. A caller
        whose unit rolled back must be able to retract the advance, or the
        next poll takes a free 304 over writes that were never persisted —
        and, since nothing else re-fetches, stays wrong until GitHub's own
        representation changes for an unrelated reason.

        `path_prefix` keeps one caller's retraction from costing every other
        caller sharing this client its own hard-won ETags.
        """
        if path_prefix is None:
            self._etags.clear()
            return
        for key in [key for key in self._etags if key[0].startswith(path_prefix)]:
            del self._etags[key]

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        expect: tuple[int, ...],
        extra_headers: Mapping[str, str],
    ) -> httpx.Response:
        self._assert_not_cooling_down()
        token = await self._token_provider()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            **extra_headers,
        }
        try:
            response = await self._http.request(
                method, f"{self._api_url}{path}", json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise GitHubUnavailable(_TRANSPORT_ERROR_STATUS, str(exc)) from exc

        if response.status_code in expect:
            return response

        error = _map_error(response)
        if isinstance(error, GitHubUnavailable) and error.retry_after is not None:
            # GitHub told us how long to wait; honouring it is not optional.
            # Continuing to request while secondary-rate-limited is grounds
            # for extending the block or suspending the integration, and no
            # caller is in a position to do this instead: every handler
            # swallows `GitHubUnavailable` and returns, and the tick/poll
            # loops re-issue the same calls on their fixed cadences.
            self._cooldown_until = max(
                self._cooldown_until or 0.0, self._clock() + error.retry_after
            )
        raise error

    def _assert_not_cooling_down(self) -> None:
        """Refuse to send — with no HTTP call and no token minted — while a
        `retry-after` window is still open, raising the same transient
        `GitHubUnavailable` the callers already know how to leave state
        alone for. `retry_after` carries what is *left* of the window, so a
        caller that ever wants to schedule around it can."""
        if self._cooldown_until is None:
            return
        remaining = self._cooldown_until - self._clock()
        if remaining <= 0:
            self._cooldown_until = None
            return
        raise GitHubUnavailable(
            _TRANSPORT_ERROR_STATUS,
            "cooling down after a GitHub rate-limit response",
            retry_after=remaining,
        )
