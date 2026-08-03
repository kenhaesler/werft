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

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID


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
