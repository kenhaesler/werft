from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from werft.api.history import event_history


@pytest.mark.asyncio
async def test_event_history_returns_payload_and_paginates() -> None:
    run_id = uuid4()
    row = SimpleNamespace(
        id=9,
        run_id=run_id,
        project_slug="demo",
        issue_number=12,
        issue_title="Fix queue",
        run_status="running",
        event_type="status_changed",
        payload={"from": "queued", "to": "running", "secret": "never-filtered-out"},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    result = MagicMock()
    result.all.return_value = [row]
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=4)
    session.execute = AsyncMock(return_value=result)

    response = await event_history(project="demo", q="queue", limit=1, offset=2, session=session)

    assert response["total"] == 4
    assert response["events"][0]["payload"]["to"] == "running"
    statement = session.execute.await_args.args[0]
    assert "run_events" in str(statement)


@pytest.mark.asyncio
async def test_event_history_empty_result_is_clear() -> None:
    result = MagicMock()
    result.all.return_value = []
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=0)
    session.execute = AsyncMock(return_value=result)

    response = await event_history(session=session)

    assert response == {"total": 0, "events": []}
