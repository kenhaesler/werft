"""Small, process-local truth about the manager's scheduler activity.

This deliberately is not durable state.  The database remains the source of
truth for runs; this module only reports what this manager process is doing
now, or has just done, and therefore starts empty after a restart.
"""

from __future__ import annotations

from collections import deque
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID

_worker_name: ContextVar[str | None] = ContextVar("werft_activity_worker", default=None)
_WORKERS = ("tick", "issues", "checks")
_RECENT_LIMIT = 50


@dataclass
class _Worker:
    state: str = "idle"
    current_operation: dict[str, str] | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error_at: datetime | None = None
    waiting_until: datetime | None = None
    iteration_had_error: bool = False


class ManagerActivity:
    """Mutable scheduler observation state, confined to one asyncio process."""

    def __init__(self, *, available: bool, unavailable_reason: str | None = None) -> None:
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.started_at = datetime.now(UTC) if available else None
        self._workers = {name: _Worker() for name in _WORKERS}
        self._recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_LIMIT)
        self._live_driver_run_ids: set[UUID] = set()

    @classmethod
    def unavailable(cls, reason: str) -> ManagerActivity:
        return cls(available=False, unavailable_reason=reason)

    def bind_worker(self, name: str):
        return _worker_name.set(name)

    def reset_worker(self, token: Token[str | None]) -> None:
        _worker_name.reset(token)

    def iteration_started(self, name: str) -> None:
        worker = self._workers[name]
        worker.state = "running"
        worker.current_operation = {"kind": f"{name}_iteration", "key": ""}
        worker.last_started_at = datetime.now(UTC)
        worker.waiting_until = None
        worker.iteration_had_error = False

    def iteration_finished(self, name: str) -> None:
        worker = self._workers[name]
        worker.current_operation = None
        worker.last_completed_at = datetime.now(UTC)
        worker.state = "error" if worker.iteration_had_error else "waiting"

    def iteration_failed(self, name: str) -> None:
        worker = self._workers[name]
        worker.current_operation = None
        worker.last_error_at = datetime.now(UTC)
        worker.iteration_had_error = True
        worker.state = "error"

    def waiting(self, name: str, interval_seconds: float) -> None:
        worker = self._workers[name]
        worker.current_operation = None
        worker.waiting_until = datetime.now(UTC) + timedelta(seconds=interval_seconds)

    def unit_started(
        self, kind: str, key: Any
    ) -> tuple[str, datetime, float, dict[str, str] | None]:
        worker_name = _worker_name.get() or "direct"
        worker = self._workers.get(worker_name)
        now = datetime.now(UTC)
        previous_operation = worker.current_operation if worker is not None else None
        if worker is not None:
            worker.current_operation = {"kind": kind, "key": str(key)}
        return worker_name, now, monotonic(), previous_operation

    def unit_finished(
        self,
        token: tuple[str, datetime, float, dict[str, str] | None],
        *,
        kind: str,
        key: Any,
        succeeded: bool,
    ) -> None:
        worker_name, started_at, started_monotonic, previous_operation = token
        completed_at = datetime.now(UTC)
        worker = self._workers.get(worker_name)
        if worker is not None:
            worker.current_operation = previous_operation
            if not succeeded:
                worker.last_error_at = completed_at
                worker.iteration_had_error = True
        self._recent.appendleft(
            {
                "worker": worker_name,
                "kind": kind,
                "key": str(key),
                "outcome": "succeeded" if succeeded else "failed",
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": max(0, int((monotonic() - started_monotonic) * 1000)),
            }
        )

    def set_live_driver_run_ids(self, run_ids: set[UUID]) -> None:
        self._live_driver_run_ids = set(run_ids)

    def stop(self) -> None:
        """Mark this snapshot unavailable once its scheduler has exited."""
        self.available = False
        self.unavailable_reason = "stopped"
        self._live_driver_run_ids.clear()
        for worker in self._workers.values():
            worker.state = "idle"
            worker.current_operation = None
            worker.waiting_until = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "started_at": self.started_at,
            "workers": {name: asdict(worker) for name, worker in self._workers.items()},
            "recent_operations": list(self._recent),
            "live_driver_run_ids": sorted(self._live_driver_run_ids, key=str),
        }
