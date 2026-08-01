from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import Run
from werft.domain.runs import RunStatus


async def transition_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    expected_version: int,
    new_status: RunStatus,
    extra: dict[str, Any] | None = None,
) -> bool:
    """CAS status change: WHERE id=:id AND version=:v (SPEC §3.2).

    Returns False when the row moved under us (lost race) — the caller
    re-reads and re-decides; it never retries blind. The DB trigger enforces
    legality, writes run_events, and NOTIFYes in the same transaction.
    """
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.version == expected_version)
        .values(status=new_status.value, version=expected_version + 1, **(extra or {}))
    )
    result = await session.execute(stmt)
    return result.rowcount == 1
