"""`NtfyAlertSink` (Task 14, SPEC §9.5 research pin: `POST {url}/{topic}`,
`Title`/`Priority`/`Tags` headers, bearer auth when configured) against
`httpx.MockTransport` — and the fire-and-forget contract (Task 5's binding
controller ruling): every method returns before the POST resolves, and no
exception — network failure or non-2xx response alike — is ever visible to
the caller. `drain()` is the test-only (and shutdown-only) way to wait for
what a method call scheduled.
"""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from werft.observe.alerts import AlertSink, NtfyAlertSink

URL = "https://ntfy.example.test"
TOPIC = "werft-test"
RUN_ID = uuid4()


def sink_with(handler, *, token: str | None = None) -> NtfyAlertSink:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NtfyAlertSink(http, url=URL, topic=TOPIC, token=token)


def recording_handler(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    return handler


async def test_ntfy_alert_sink_satisfies_the_protocol() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert isinstance(NtfyAlertSink(http, url=URL, topic=TOPIC, token=None), AlertSink)


CASES = [
    (
        "review_waiting",
        lambda sink: sink.review_waiting("acme", RUN_ID, "https://github.test/acme/pr/1"),
        "Review waiting: acme",
        "default",
        "https://github.test/acme/pr/1",
    ),
    (
        "run_parked",
        lambda sink: sink.run_parked("acme", RUN_ID, "ci_red"),
        "Run parked: acme",
        "default",
        "ci_red",
    ),
    (
        "project_flipped",
        lambda sink: sink.project_flipped("acme"),
        "Project flipped: acme",
        "default",
        "acme",
    ),
    (
        "auth_failure",
        lambda sink: sink.auth_failure("claude"),
        "Auth failure: claude",
        "high",
        "claude",
    ),
    (
        "quota_exhausted_until",
        lambda sink: sink.quota_exhausted_until("claude", datetime(2026, 8, 16, 12, 0, tzinfo=UTC)),
        "Quota exhausted: claude",
        "default",
        "2026-08-16T12:00:00+00:00",
    ),
    (
        "disk_threshold",
        lambda sink: sink.disk_threshold(92.5),
        "Disk threshold: 92.5%",
        "high",
        "92.5",
    ),
]


@pytest.mark.parametrize("name,call,title,priority,body_substr", CASES, ids=[c[0] for c in CASES])
async def test_method_posts_url_topic_title_priority_body(name, call, title, priority, body_substr):
    seen: list[httpx.Request] = []
    sink = sink_with(recording_handler(seen))

    await call(sink)
    await sink.drain()

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == f"{URL}/{TOPIC}"
    assert request.headers["title"] == title
    assert request.headers["priority"] == priority
    body = request.content.decode()
    assert body_substr in body
    assert "\n" not in body
    assert "authorization" not in request.headers


async def test_bearer_attached_when_token_configured():
    seen: list[httpx.Request] = []
    sink = sink_with(recording_handler(seen), token="tk_secret")

    await sink.project_flipped("acme")
    await sink.drain()

    assert seen[0].headers["authorization"] == "Bearer tk_secret"


async def test_no_authorization_header_when_token_not_configured():
    seen: list[httpx.Request] = []
    sink = sink_with(recording_handler(seen))

    await sink.project_flipped("acme")
    await sink.drain()

    assert "authorization" not in seen[0].headers


async def test_method_call_returns_before_the_post_resolves():
    """The binding ruling: the caller never awaits network I/O. Proven by a
    handler that blocks until the test lets it go — if `project_flipped`
    awaited the POST inline, this test would deadlock."""
    import asyncio

    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow_handler))
    sink = NtfyAlertSink(http, url=URL, topic=TOPIC, token=None)

    await asyncio.wait_for(sink.project_flipped("acme"), timeout=1.0)

    release.set()
    await sink.drain()


async def test_a_500_response_is_swallowed_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    sink = sink_with(handler)

    await sink.project_flipped("acme")  # must not raise
    await sink.drain()  # must not raise; no "exception never retrieved"


async def test_a_connect_error_is_swallowed_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    sink = sink_with(handler)

    await sink.project_flipped("acme")  # must not raise
    await sink.drain()  # must not raise; no "exception never retrieved"


async def test_drain_with_nothing_pending_is_a_noop():
    sink = sink_with(lambda request: httpx.Response(200))
    await sink.drain()
