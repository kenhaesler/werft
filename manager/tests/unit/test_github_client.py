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


def client_with(handler, *, token: str = "ghs_test") -> GitHubClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def token_provider() -> str:
        return token

    return GitHubClient(http, api_url=API_URL, token_provider=token_provider)


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
