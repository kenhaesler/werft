"""Alert seam (Behavioral decision 9): the six operator notifications every
handler fires at its natural site. `AlertSink` is deliberately stdlib-only —
`observe` sits low enough in the layer stack (SPEC §1 layering; the import
contract lets it reach config/contracts/db/domain) that this protocol must
never gain a reason to import any of those just to describe "send this
notification"; T6's `NtfyAlertSink` is the one implementation that needs
more.

Sinks never raise: alerting must never break the run loop it is reporting
on. `NullAlertSink` is the thin-loop default until T6 wires a real one.
"""

import asyncio
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

import httpx
import structlog

logger = structlog.get_logger(__name__)


@runtime_checkable
class AlertSink(Protocol):
    """The closed set of operator notifications (SPEC §9). All six are
    fire-and-forget: `async ... -> None`, and implementations must never
    raise — a failed notification is not a reason to fail the run it
    describes."""

    async def review_waiting(self, project_slug: str, run_id: UUID, pr_url: str) -> None: ...

    async def run_parked(self, project_slug: str, run_id: UUID, reason: str) -> None: ...

    async def project_flipped(self, project_slug: str) -> None: ...

    async def auth_failure(self, provider: str) -> None: ...

    async def quota_exhausted_until(self, provider: str, until: datetime) -> None: ...

    async def disk_threshold(self, percent: float) -> None: ...


class NullAlertSink:
    """No-op `AlertSink` — every call is a no-op that never raises. The
    thin-loop default (T5) until T6 wires `NtfyAlertSink` in."""

    async def review_waiting(self, project_slug: str, run_id: UUID, pr_url: str) -> None:
        return None

    async def run_parked(self, project_slug: str, run_id: UUID, reason: str) -> None:
        return None

    async def project_flipped(self, project_slug: str) -> None:
        return None

    async def auth_failure(self, provider: str) -> None:
        return None

    async def quota_exhausted_until(self, provider: str, until: datetime) -> None:
        return None

    async def disk_threshold(self, percent: float) -> None:
        return None


class NtfyAlertSink:
    """`AlertSink` backed by ntfy (SPEC §9.5; research pin: `POST
    {url}/{topic}`, body = message text, `Title`/`Priority`/`Tags` headers,
    `Authorization: Bearer` when a token is configured).

    Every handler call site fires these from inside a DB transaction (module
    docstring), so this sink is fire-and-forget by construction, not just by
    intent: each method below builds its message and hands the POST to
    `asyncio.create_task`, returning before any network I/O happens —
    nothing here ever awaits the request in the caller's context. The
    spawned task is held in `_tasks` until it completes (nothing else keeps
    a bare `asyncio.create_task` result alive) and every exception —
    transport failure or a non-2xx response alike — is caught *inside* the
    task (`_post`), structlog-warned, and swallowed: it never propagates to
    the caller or to the loop's unhandled-exception handler. `drain()`
    awaits whatever is still in flight; tests use it for determinism, app
    shutdown uses it to give pending posts a bounded chance to land.
    """

    def __init__(self, http: httpx.AsyncClient, *, url: str, topic: str, token: str | None) -> None:
        self._http = http
        self._url = url.rstrip("/")
        self._topic = topic
        self._token = token
        self._tasks: set[asyncio.Task[None]] = set()

    def _fire(self, title: str, body: str, *, priority: str, tags: str) -> None:
        task = asyncio.create_task(self._post(title, body, priority=priority, tags=tags))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, title: str, body: str, *, priority: str, tags: str) -> None:
        headers = {"Title": title, "Priority": priority, "Tags": tags}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.post(
                f"{self._url}/{self._topic}", content=body, headers=headers
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - fire-and-forget must never raise
            logger.warning("ntfy.post_failed", title=title, error=str(exc))

    async def drain(self) -> None:
        """Await every currently-pending spawned task. Not part of
        `AlertSink` — only this implementation buffers work past its own
        method calls, so only this implementation needs a way to flush it.

        `return_exceptions=True`: `_post` already catches every `Exception`
        itself, but a task cancelled out from under `drain()` (rather than
        raising on its own) surfaces as a `CancelledError` `gather` would
        otherwise re-raise from *this* call — which callers (app.py's
        shutdown) bound with a timeout precisely so a slow ntfy host can't
        consume their whole teardown budget. `return_exceptions=True` keeps
        that per-task outcome contained here instead of unwinding out of
        `drain()` itself; a genuine external cancellation of the `drain()`
        call (not of one child task) still propagates as normal — bounding
        that is the caller's job, not this method's."""
        pending = list(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def review_waiting(self, project_slug: str, run_id: UUID, pr_url: str) -> None:
        self._fire(
            f"Review waiting: {project_slug}",
            f"Run {run_id} is ready for review: {pr_url}",
            priority="default",
            tags="eyes",
        )

    async def run_parked(self, project_slug: str, run_id: UUID, reason: str) -> None:
        self._fire(
            f"Run parked: {project_slug}",
            f"Run {run_id} parked: {reason}",
            priority="default",
            tags="warning",
        )

    async def project_flipped(self, project_slug: str) -> None:
        self._fire(
            f"Project flipped: {project_slug}",
            f"{project_slug} flipped to strict branch protection",
            priority="default",
            tags="rocket",
        )

    async def auth_failure(self, provider: str) -> None:
        self._fire(
            f"Auth failure: {provider}",
            f"Provider {provider} needs re-authentication",
            priority="high",
            tags="closed_lock_with_key",
        )

    async def quota_exhausted_until(self, provider: str, until: datetime) -> None:
        self._fire(
            f"Quota exhausted: {provider}",
            f"Provider {provider} exhausted until {until.isoformat()}",
            priority="default",
            tags="hourglass_flowing_sand",
        )

    async def disk_threshold(self, percent: float) -> None:
        self._fire(
            f"Disk threshold: {percent:.1f}%",
            f"Disk usage at {percent:.1f}%",
            priority="high",
            tags="floppy_disk",
        )
