"""Stage egress, run the collector, and record artifact rows plus the
evidence event (SPEC §8; plan decisions D4/D5/D6/D11).

This is the integration point between the DB-blind `werft.runner` plane
(`collect_trees`, `extract_egress_lines`) and the DB-aware orchestrator
layer: it lives here, not in `werft.runner`, because `werft.runner` must
never import `werft.db` (SPEC §3.3.1).

The whole function is wrapped per D11: teardown calls this unconditionally,
so a collection failure (a hostile or vanished run directory, a filesystem
error, anything) must never raise — it is logged and swallowed, returning
`None`.
"""

import asyncio
import os
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from werft.db.models import Artifact, RunEvent
from werft.runner import collect as _collect
from werft.runner.collect import DIR_MODE, CollectionReport, TreeSource, collect_trees
from werft.runner.create_body import RunPlacement
from werft.runner.egress import extract_egress_lines

logger = structlog.get_logger(__name__)

#: Re-exported so a caller (or a test) can override the effective cap without
#: reaching into `werft.runner.collect` directly. Read at call time, not
#: bound as a parameter default, so monkeypatching this module attribute
#: takes effect.
MAX_TOTAL_BYTES = _collect.MAX_TOTAL_BYTES
MAX_FILE_BYTES = _collect.MAX_FILE_BYTES

#: (source dir under `placement.run_dir`, dest prefix). `"egress"` is staged
#: first by `_stage_egress`, into `placement.run_dir`, so it lands here too.
EVIDENCE_SOURCES: tuple[tuple[str, str], ...] = (
    ("workspace/.werft-artifacts", "werft-artifacts"),
    ("workspace/playwright-report", "playwright-report"),
    ("workspace/test-results", "test-results"),
    ("outputs", "outputs"),
    ("egress", "egress"),
)

#: (settings attr value, staged filename) pairs for the two configured logs.
_EGRESS_LOGS: tuple[tuple[str, str], ...] = (
    ("squid_access_log", "squid-access.log"),
    ("dns_guard_query_log", "dns-guard.log"),
)

_DROPPED_PAYLOAD_LIMIT = 100


def _stage_egress(
    run_dir: str, subnets: list[str], squid_access_log: str, dns_guard_query_log: str
) -> None:
    logs = {"squid_access_log": squid_access_log, "dns_guard_query_log": dns_guard_query_log}
    for attr, name in _EGRESS_LOGS:
        log_path = logs[attr]
        if not log_path:
            continue
        dest_path = os.path.join(run_dir, "egress", name)
        extract_egress_lines(log_path, subnets, dest_path)


def _build_existing(dest_root: str) -> dict[str, int]:
    existing: dict[str, int] = {}
    for dirpath, _dirnames, filenames in os.walk(dest_root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, dest_root).replace(os.sep, "/")
            try:
                existing[rel] = os.stat(full).st_size
            except OSError:
                continue
    return existing


def _collect_on_disk(
    *,
    run_id: UUID,
    run_dir: str,
    artifacts_root: str,
    subnets: list[str],
    squid_access_log: str,
    dns_guard_query_log: str,
) -> CollectionReport:
    """Every filesystem step of a collection pass, in one synchronous call.

    Extracted so `collect_run_evidence` can hand the whole thing to
    `asyncio.to_thread`: staging egress reads up to a 32 MiB tail of a shared
    proxy log, `_build_existing` walks the store, and `collect_trees` copies up
    to 100 MiB — hundreds of milliseconds of blocking syscalls that used to run
    on the orchestrator's *only* event loop, stalling every other run's
    heartbeat and lease renewal behind one teardown.
    """
    _stage_egress(run_dir, subnets, squid_access_log, dns_guard_query_log)

    run_store = os.path.join(artifacts_root, str(run_id))
    os.makedirs(run_store, mode=DIR_MODE, exist_ok=True)
    if os.name != "nt":  # POSIX-only, like `workspace._harden`
        os.chmod(run_store, DIR_MODE)
    dest = os.path.join(run_store, "artifacts")

    existing = _build_existing(dest)

    sources = [TreeSource(os.path.join(run_dir, src), prefix) for src, prefix in EVIDENCE_SOURCES]
    return collect_trees(
        sources,
        dest,
        max_total_bytes=MAX_TOTAL_BYTES,
        max_file_bytes=MAX_FILE_BYTES,
        existing=existing,
    )


def _event_payload(report: CollectionReport) -> dict[str, Any]:
    dropped = [
        {"path": d.rel_path, "reason": d.reason, "bytes": d.size_bytes} for d in report.dropped
    ]
    payload: dict[str, Any] = {
        "phase": "artifacts",
        "collected": len(report.artifacts),
        "bytes_total": report.bytes_total,
        "truncated": report.truncated,
        "dropped": dropped[:_DROPPED_PAYLOAD_LIMIT],
    }
    if len(dropped) > _DROPPED_PAYLOAD_LIMIT:
        payload["dropped_elided"] = len(dropped) - _DROPPED_PAYLOAD_LIMIT
    return payload


async def _record(session: AsyncSession, run_id: UUID, report: CollectionReport) -> None:
    if report.artifacts:
        insert_stmt = pg_insert(Artifact).values(
            [
                {"run_id": run_id, "path": artifact.rel_path, "bytes": artifact.size_bytes}
                for artifact in report.artifacts
            ]
        )
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=["run_id", "path"],
            set_={
                "bytes": insert_stmt.excluded.bytes,
                "collected_at": func.now(),
            },
        )
        await session.execute(upsert_stmt)

    session.add(RunEvent(run_id=run_id, event_type="dispatch", payload=_event_payload(report)))


async def collect_run_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    placement: RunPlacement,
    artifacts_root: str,
    subnets: list[str],
    squid_access_log: str,
    dns_guard_query_log: str,
) -> CollectionReport | None:
    """Stage egress, collect evidence trees, and record rows plus one event.

    Returns the `CollectionReport`, or `None` only when the whole pass was
    swallowed by the D11 wrapper (never raises).

    The filesystem half runs in a worker thread (`_collect_on_disk`); only the
    DB transaction stays on the loop.
    """
    try:
        report = await asyncio.to_thread(
            _collect_on_disk,
            run_id=run_id,
            run_dir=placement.run_dir,
            artifacts_root=artifacts_root,
            subnets=subnets,
            squid_access_log=squid_access_log,
            dns_guard_query_log=dns_guard_query_log,
        )

        async with session_factory() as session, session.begin():
            await _record(session, run_id, report)

        return report
    except Exception:
        logger.warning("evidence_collection_failed", run_id=str(run_id), exc_info=True)
        return None
