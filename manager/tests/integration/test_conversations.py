"""Conversation API durability against the real migrated PostgreSQL schema."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from werft.api import conversations
from werft.api.routes import get_session
from werft.app import create_app
from werft.config.settings import Settings
from werft.db.models import ConversationMessage, Run
from werft.orchestrator.conversation import apply_tool, flush_conversation_outbox
from werft.orchestrator.conversation import ask as provider_ask

TOKEN = "conversation-test-token"


async def _seed(session: AsyncSession, *, running: bool = False) -> Run:
    tag = uuid.uuid4().hex[:8]
    project_id = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo) "
                "VALUES (:s, :owner, :repo) RETURNING id"
            ),
            {"s": f"p-{tag}", "owner": f"o-{tag}", "repo": f"r-{tag}"},
        )
    ).scalar_one()
    item_id = (
        await session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, github_updated_at) "
                "VALUES (:p, 1, 'task title', now()) RETURNING id"
            ),
            {"p": project_id},
        )
    ).scalar_one()
    run_id = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, attempt_count) "
                "VALUES (:p, :b, :s, :a) RETURNING id"
            ),
            {
                "p": project_id,
                "b": item_id,
                "s": "running" if running else "queued",
                "a": 1 if running else 0,
            },
        )
    ).scalar_one()
    if running:
        await session.execute(
            text(
                "INSERT INTO run_attempts (run_id, attempt_no, provider) VALUES (:r, 1, 'claude')"
            ),
            {"r": run_id},
        )
    await session.commit()
    return await session.get(Run, run_id)


def _app(session: AsyncSession, tmp_path, *, enabled: bool = False):
    token = tmp_path / "token"
    token.write_text(TOKEN)
    app = create_app(
        Settings(
            api_token_file=str(token), runs_root=str(tmp_path), agent_conversations_enabled=enabled
        )
    )

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_auth_and_orchestrator_idempotency(db_session, tmp_path, monkeypatch) -> None:
    calls = 0

    async def fake_ask(*args, **kwargs):
        nonlocal calls
        calls += 1
        return "answer", [], []

    monkeypatch.setattr(conversations, "ask", fake_ask)
    app = _app(db_session, tmp_path)
    client_id = str(uuid.uuid4())
    async with await _client(app) as client:
        assert (await client.get("/api/v1/conversations/orchestrator")).status_code == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        first = await client.post(
            "/api/v1/conversations/orchestrator/messages",
            headers=headers,
            json={"content": "what is running?", "client_id": client_id},
        )
        second = await client.post(
            "/api/v1/conversations/orchestrator/messages",
            headers=headers,
            json={"content": "what is running?", "client_id": client_id},
        )
    assert first.status_code == second.status_code == 200
    assert calls == 1
    assert len(second.json()["messages"]) == 2


async def test_input_validation(db_session, tmp_path) -> None:
    app = _app(db_session, tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with await _client(app) as client:
        for content in ("", "x" * 16001):
            response = await client.post(
                "/api/v1/conversations/orchestrator/messages",
                headers=headers,
                json={"content": content, "client_id": str(uuid.uuid4())},
            )
            assert response.status_code == 422


async def test_final_receipt_is_imported_after_readiness_closes(
    db_session, tmp_path, monkeypatch
) -> None:
    run = await _seed(db_session, running=True)
    answer_id = uuid.uuid4()

    class Transport:
        @staticmethod
        def conversation_ready(*args):
            return False

        @staticmethod
        def read_messages(*args):
            return [
                {
                    "id": str(answer_id),
                    "role": "assistant",
                    "content": "final",
                    "status": "answered",
                }
            ]

    monkeypatch.setattr(conversations, "_transport", lambda: Transport)
    app = _app(db_session, tmp_path, enabled=True)
    async with await _client(app) as client:
        response = await client.get(
            f"/api/v1/conversations/{run.id}", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["messages"][0]["content"] == "final"


async def test_foreign_receipt_id_cannot_update_other_scope(
    db_session, tmp_path, monkeypatch
) -> None:
    first, second = await _seed(db_session, running=True), await _seed(db_session, running=True)
    user_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO conversation_messages "
            "(id, scope, run_id, attempt_no, role, content, status) "
            "VALUES (:i, :s, :r, 1, 'user', 'secret', 'queued')"
        ),
        {"i": user_id, "s": str(second.id), "r": second.id},
    )
    await db_session.commit()

    class Transport:
        @staticmethod
        def conversation_ready(*args):
            return True

        @staticmethod
        def read_messages(*args):
            return [{"id": str(user_id), "role": "user", "content": "x", "status": "delivered"}]

    monkeypatch.setattr(conversations, "_transport", lambda: Transport)
    app = _app(db_session, tmp_path, enabled=True)
    async with await _client(app) as client:
        await client.get(
            f"/api/v1/conversations/{first.id}", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    status = await db_session.scalar(
        text("SELECT status FROM conversation_messages WHERE id = :i"), {"i": user_id}
    )
    assert status == "queued"


async def test_priority_tool_rejects_bool_bounds_and_extra_keys(db_session) -> None:
    run = await _seed(db_session)
    # The API commits its context snapshot before entering the provider loop.
    await db_session.commit()
    for payload in (
        {"run_id": str(run.id), "priority": True},
        {"run_id": str(run.id), "priority": 32768},
        {"run_id": str(run.id), "priority": 1, "extra": "no"},
        {"run_id": "not-a-uuid", "priority": 1},
    ):
        outcome, direction = await apply_tool(db_session, "prioritize_run", payload)
        assert "rejected" in outcome
        assert direction is None
    assert (await db_session.get(Run, run.id)).priority == 100


async def test_provider_tool_roundtrip_persists_outcome_and_closes_db_before_http(
    db_session, tmp_path, monkeypatch
) -> None:
    run = await _seed(db_session)
    # The API commits its context snapshot before entering the provider loop.
    await db_session.commit()
    key_file = tmp_path / "conversation-key"
    key_file.write_text("test-key")
    requests: list[dict] = []

    class Response:
        def __init__(self, body):
            self._body = body
            self.content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            assert not db_session.in_transaction()
            requests.append(json)
            if len(requests) == 1:
                return Response(
                    {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "prioritize_run",
                                "input": {"run_id": str(run.id), "priority": 12},
                            }
                        ]
                    }
                )
            return Response({"content": [{"type": "text", "text": "priority changed"}]})

    monkeypatch.setattr("werft.orchestrator.conversation.httpx.AsyncClient", lambda **_: Client())

    async def execute_tool(name, data):
        outcome, _ = await apply_tool(db_session, name, data)
        await db_session.commit()
        return outcome

    answer, outcomes, _ = await provider_ask(
        db_session,
        api_key_file=str(key_file),
        model="test-model",
        history=[{"role": "user", "content": "raise priority"}],
        system_context="test context",
        execute_tool=execute_tool,
    )
    assert answer == "priority changed"
    assert outcomes == []
    await db_session.refresh(run)
    assert run.priority == 12
    tool_results = requests[1]["messages"][-1]["content"]
    assert tool_results == [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "priority updated"}
    ]


async def test_distinct_concurrent_orchestrator_messages_are_serialized(
    db_session, tmp_path, monkeypatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_ask(*args, **kwargs):
        entered.set()
        await release.wait()
        return "done", [], []

    monkeypatch.setattr(conversations, "ask", slow_ask)
    app = _app(db_session, tmp_path)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with await _client(app) as client:
        first = asyncio.create_task(
            client.post(
                "/api/v1/conversations/orchestrator/messages",
                headers=headers,
                json={"content": "first", "client_id": str(uuid.uuid4())},
            )
        )
        await entered.wait()
        second = await client.post(
            "/api/v1/conversations/orchestrator/messages",
            headers=headers,
            json={"content": "second", "client_id": str(uuid.uuid4())},
        )
        release.set()
        first_response = await first
    assert first_response.status_code == 200
    assert second.status_code == 409


async def test_outbox_flush_publishes_committed_input_without_http(db_session, tmp_path) -> None:
    run = await _seed(db_session, running=True)
    message = ConversationMessage(
        id=uuid.uuid4(),
        scope=str(run.id),
        run_id=run.id,
        attempt_no=1,
        role="user",
        content="please inspect the tests",
        status="queued",
    )
    db_session.add(message)
    await db_session.commit()
    output = tmp_path / str(run.id) / "outputs"
    output.mkdir(parents=True)
    (tmp_path / str(run.id) / "secrets").mkdir()
    (output / "conversation-ready.json").write_text(json.dumps({"attempt": 1, "available": True}))

    await flush_conversation_outbox(db_session, runs_root=str(tmp_path))

    inbox = json.loads((tmp_path / str(run.id) / "secrets" / "operator_messages.json").read_text())
    assert inbox["attempt"] == 1
    assert inbox["messages"] == [
        {
            "id": str(message.id),
            "content": "please inspect the tests",
            "created_at": message.created_at.isoformat(),
        }
    ]


async def test_outbox_flush_keeps_final_answer_answered_after_ready_closes(
    db_session, tmp_path
) -> None:
    run = await _seed(db_session, running=True)
    message = ConversationMessage(
        id=uuid.uuid4(),
        scope=str(run.id),
        run_id=run.id,
        attempt_no=1,
        role="user",
        content="final question",
        status="queued",
    )
    db_session.add(message)
    await db_session.commit()
    output = tmp_path / str(run.id) / "outputs"
    output.mkdir(parents=True)
    (output / "conversation-ready.json").write_text(json.dumps({"attempt": 1, "available": False}))
    (output / "conversation.jsonl").write_text(
        json.dumps(
            {
                "attempt": 1,
                "id": str(message.id),
                "role": "user",
                "content": "final question",
                "status": "answered",
            }
        )
        + "\n"
    )

    await flush_conversation_outbox(db_session, runs_root=str(tmp_path))
    await db_session.refresh(message)
    assert message.status == "answered"
    assert message.error is None
