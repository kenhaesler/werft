"""Protocol-level tests for the thin GitHub REST wrapper (SPEC §6.2,
`werft/github/client.py`): the three standard headers, the transient
(`GitHubUnavailable`) vs. fatal (`GitHubApiError`) status split, `expect`
pass-through, and the ETag-conditional store — all against
`httpx.MockTransport`.
"""

import json

import httpx
import pytest

from werft.github.client import ConditionalResult, GitHubApiError, GitHubClient, GitHubUnavailable

API_URL = "https://api.github.test"


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def client_with(handler, *, token: str = "ghs_test", clock=None) -> GitHubClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def token_provider() -> str:
        return token

    if clock is None:
        return GitHubClient(http, api_url=API_URL, token_provider=token_provider)
    return GitHubClient(http, api_url=API_URL, token_provider=token_provider, clock=clock)


async def test_request_injects_the_three_standard_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = client_with(handler, token="ghs_abc")
    await client.request("GET", "/repos/acme/widgets", expect=(200,))

    assert len(seen) == 1
    headers = seen[0].headers
    assert headers["authorization"] == "Bearer ghs_abc"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"


async def test_request_forwards_method_path_json_and_params():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"ok": True})

    client = client_with(handler)
    response = await client.request(
        "POST",
        "/repos/acme/widgets/issues",
        json={"title": "hi"},
        params={"state": "open"},
        expect=(201,),
    )

    assert response.status_code == 201
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/repos/acme/widgets/issues"
    assert request.url.params["state"] == "open"
    assert json.loads(request.content) == {"title": "hi"}


async def test_request_returns_response_unraised_when_status_in_expect():
    client = client_with(lambda r: httpx.Response(404, json={"message": "not found"}))

    response = await client.request("GET", "/repos/acme/widgets", expect=(200, 404))

    assert response.status_code == 404


async def test_request_raises_github_api_error_with_json_message_outside_expect():
    client = client_with(lambda r: httpx.Response(422, json={"message": "Validation Failed"}))

    with pytest.raises(GitHubApiError) as exc_info:
        await client.request("POST", "/repos/acme/widgets/pulls", expect=(201,))

    error = exc_info.value
    assert error.status == 422
    assert error.message == "Validation Failed"
    assert not isinstance(error, GitHubUnavailable)


async def test_request_raises_github_unavailable_on_500():
    client = client_with(lambda r: httpx.Response(500, json={"message": "boom"}))

    with pytest.raises(GitHubUnavailable):
        await client.request("GET", "/repos/acme/widgets", expect=(200,))


async def test_request_raises_github_unavailable_with_retry_after_on_403():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"retry-after": "60"}, json={"message": "rate limited"})

    client = client_with(handler)

    with pytest.raises(GitHubUnavailable) as exc_info:
        await client.request("GET", "/repos/acme/widgets", expect=(200,))

    assert exc_info.value.status == 403
    assert exc_info.value.retry_after == 60


async def test_request_raises_github_unavailable_on_429_with_ratelimit_remaining_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"x-ratelimit-remaining": "0"},
            json={"message": "secondary rate limit"},
        )

    client = client_with(handler)

    with pytest.raises(GitHubUnavailable):
        await client.request("GET", "/repos/acme/widgets", expect=(200,))


async def test_request_does_not_treat_plain_403_as_unavailable():
    """A 403 with neither retry-after nor a zeroed rate-limit header is a
    permission problem, not the transient family."""
    client = client_with(lambda r: httpx.Response(403, json={"message": "forbidden"}))

    with pytest.raises(GitHubApiError) as exc_info:
        await client.request("GET", "/repos/acme/widgets", expect=(200,))

    assert not isinstance(exc_info.value, GitHubUnavailable)


async def test_request_raises_github_unavailable_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = client_with(handler)

    with pytest.raises(GitHubUnavailable):
        await client.request("GET", "/repos/acme/widgets", expect=(200,))


async def test_get_conditional_first_call_has_no_if_none_match_and_stores_etag():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "if-none-match" not in request.headers
        return httpx.Response(200, headers={"etag": '"abc123"'}, json=[{"id": 1}])

    client = client_with(handler)

    result = await client.get_conditional("/repos/acme/widgets/issues", params={"state": "open"})

    assert result == ConditionalResult(modified=True, data=[{"id": 1}])


async def test_get_conditional_second_call_sends_if_none_match_and_maps_304():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, headers={"etag": '"abc123"'}, json=[{"id": 1}])
        assert request.headers["if-none-match"] == '"abc123"'
        return httpx.Response(304)

    client = client_with(handler)

    await client.get_conditional("/repos/acme/widgets/issues", params={"state": "open"})
    result = await client.get_conditional("/repos/acme/widgets/issues", params={"state": "open"})

    assert len(calls) == 2
    assert result == ConditionalResult(modified=False, data=None)


async def test_get_conditional_key_includes_params_so_different_params_get_own_etag():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # Different params each time: neither call should carry If-None-Match.
        assert "if-none-match" not in request.headers
        return httpx.Response(200, headers={"etag": '"e1"'}, json=[])

    client = client_with(handler)

    await client.get_conditional("/repos/acme/widgets/issues", params={"state": "open"})
    await client.get_conditional("/repos/acme/widgets/issues", params={"state": "closed"})

    assert len(calls) == 2


# -- secondary-rate-limit cooldown ------------------------------------------


async def test_a_retry_after_response_starts_a_cooldown_that_short_circuits_every_call():
    """GitHub's documented requirement is to *wait out* `retry-after` before
    retrying; continuing to request while secondary-rate-limited is grounds
    for extending the block or suspending the integration. Nothing outside
    this client is in a position to honour it — every handler swallows
    `GitHubUnavailable` and returns, and the tick/poll loops then re-issue
    the same calls on their fixed 15/30/60 s cadences, deepening the outage
    they are reporting. So the client itself refuses to send.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            403, headers={"retry-after": "60"}, json={"message": "secondary rate limit"}
        )

    clock = FakeClock()
    client = client_with(handler, clock=clock)

    with pytest.raises(GitHubUnavailable):
        await client.request("GET", "/repos/acme/widgets", expect=(200,))
    assert len(calls) == 1

    with pytest.raises(GitHubUnavailable) as exc_info:
        await client.request("PUT", "/repos/acme/widgets/pulls/1/merge", expect=(200,))
    assert len(calls) == 1  # never reached the transport at all
    assert exc_info.value.status == 0
    assert exc_info.value.message.startswith("cooling down")
    assert exc_info.value.retry_after == 60

    with pytest.raises(GitHubUnavailable):  # conditional GETs are gated too
        await client.get_conditional("/repos/acme/widgets/issues")
    assert len(calls) == 1


async def test_requests_flow_again_once_the_cooldown_has_elapsed():
    responses = [
        httpx.Response(403, headers={"retry-after": "30"}, json={"message": "slow down"}),
        httpx.Response(200, json={"ok": True}),
    ]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    clock = FakeClock()
    client = client_with(handler, clock=clock)

    with pytest.raises(GitHubUnavailable):
        await client.request("GET", "/repos/acme/widgets", expect=(200,))

    clock.advance(29.0)
    with pytest.raises(GitHubUnavailable) as exc_info:
        await client.request("GET", "/repos/acme/widgets", expect=(200,))
    assert exc_info.value.retry_after == 1.0  # what is left of the window
    assert len(calls) == 1

    clock.advance(1.5)
    response = await client.request("GET", "/repos/acme/widgets", expect=(200,))
    assert response.status_code == 200
    assert len(calls) == 2


async def test_a_plain_error_without_retry_after_starts_no_cooldown():
    """Only the rate-limited family carries the signal; a 422 or a bare 403
    must not silence the client for a minute."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(422, json={"message": "Validation Failed"})

    client = client_with(handler, clock=FakeClock())

    for _ in range(2):
        with pytest.raises(GitHubApiError):
            await client.request("POST", "/repos/acme/widgets/pulls", expect=(201,))
    assert len(calls) == 2


async def test_invalidate_conditional_forgets_every_etag_so_the_next_get_refetches():
    """The ETag store advances the instant GitHub answers 200, but the rows
    derived from that body only become durable when the *caller's*
    transaction commits. A caller whose unit rolled back has to be able to
    say "forget what I just learned", or the next poll gets a free 304 for a
    body it never actually persisted — silently, forever."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "if-none-match" not in request.headers
        return httpx.Response(200, headers={"etag": '"abc123"'}, json=[{"id": 1}])

    client = client_with(handler)

    await client.get_conditional("/repos/acme/widgets/issues", params={"state": "open"})
    client.invalidate_conditional()
    result = await client.get_conditional("/repos/acme/widgets/issues", params={"state": "open"})

    assert len(calls) == 2
    assert result.modified is True  # a real body, not a 304 over lost writes


async def test_invalidate_conditional_with_a_prefix_leaves_other_paths_cached():
    """One project's rolled-back poll must not throw away another project's
    (or another endpoint's) hard-won ETag — the client is shared per
    project, but the store is keyed by path."""
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("if-none-match")))
        return httpx.Response(200, headers={"etag": '"e1"'}, json=[])

    client = client_with(handler)

    await client.get_conditional("/repos/acme/widgets/issues")
    await client.get_conditional("/repos/other/repo/issues")
    client.invalidate_conditional("/repos/acme/widgets")
    await client.get_conditional("/repos/acme/widgets/issues")
    await client.get_conditional("/repos/other/repo/issues")

    assert seen[2] == ("/repos/acme/widgets/issues", None)  # invalidated
    assert seen[3] == ("/repos/other/repo/issues", '"e1"')  # untouched


async def test_get_conditional_key_ignores_param_order():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, headers={"etag": '"e1"'}, json=[])
        assert request.headers["if-none-match"] == '"e1"'
        return httpx.Response(304)

    client = client_with(handler)

    await client.get_conditional("/repos/acme/widgets/issues", params={"a": "1", "b": "2"})
    result = await client.get_conditional("/repos/acme/widgets/issues", params={"b": "2", "a": "1"})

    assert result.modified is False
