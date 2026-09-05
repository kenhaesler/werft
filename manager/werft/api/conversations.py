"""Authenticated, durable conversations with the orchestrator and live runs."""

import contextlib
import importlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.routes import get_session
from werft.db.models import ConversationMessage, Run
from werft.orchestrator.conversation import ConversationUnavailable, apply_tool, ask, context

conversations_router = APIRouter()
_INVALID_RUNNER_RECORD = (KeyError, ValueError, TypeError)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=16000)
    client_id: UUID


def _out(message: ConversationMessage) -> dict:
    value = {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "created_at": message.created_at,
    }
    if message.error:
        value["error"] = message.error
    return value


async def _scope_run(session: AsyncSession, scope: str) -> Run | None:
    if scope == "orchestrator":
        return None
    try:
        run_id = UUID(scope)
    except ValueError as exc:
        raise HTTPException(404, "unknown conversation scope") from exc
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "unknown conversation scope")
    return run


def _transport():
    # Kept dynamic so manager-only deployments stay bootable before the runner
    # transport is installed.
    try:
        return importlib.import_module("werft.runner.conversation")
    except ImportError:
        return None


async def _reconcile_run(
    session: AsyncSession, request: Request, run: Run
) -> tuple[bool, str | None]:
    transport = _transport()
    incoming = []
    if transport is not None and run.attempt_count:
        # A completed adapter deliberately flips readiness off. Its final JSONL
        # still belongs to this attempt and must be imported before availability
        # is evaluated.
        with contextlib.suppress(OSError):
            incoming = transport.read_messages(
                request.app.state.settings.runs_root, str(run.id), run.attempt_count
            )
    for record in incoming:
        try:
            message_id = UUID(str(record["id"]))
            role, content = record["role"], record["content"]
        except _INVALID_RUNNER_RECORD:
            continue
        existing = await session.get(ConversationMessage, message_id)
        if existing is not None:
            if (
                existing.scope == str(run.id)
                and existing.attempt_no == run.attempt_count
                and existing.role == "user"
                and record.get("status")
                in {
                    "delivered",
                    "answered",
                    "failed",
                }
            ):
                existing.status = record["status"]
            continue
        if role not in {"assistant", "system"} or not isinstance(content, str):
            continue
        await session.execute(
            insert(ConversationMessage)
            .values(
                id=message_id,
                scope=str(run.id),
                run_id=run.id,
                attempt_no=run.attempt_count,
                role=role,
                content=content[:16000],
                status="answered" if role == "assistant" else "delivered",
                error=record.get("error"),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
    await session.commit()
    ready = bool(
        request.app.state.settings.agent_conversations_enabled
        and transport is not None
        and run.status == "running"
        and run.attempt_count
        and transport.conversation_ready(
            request.app.state.settings.runs_root, str(run.id), run.attempt_count
        )
    )
    if run.status != "running" and run.attempt_count:
        await session.execute(
            update(ConversationMessage)
            .where(
                ConversationMessage.scope == str(run.id),
                ConversationMessage.attempt_no <= run.attempt_count,
                ConversationMessage.role == "user",
                ConversationMessage.status.in_(["queued", "delivered"]),
            )
            .values(status="failed", error="runner_conversation_closed")
        )
        await session.commit()
    return (True, None) if ready else (False, "runner_conversation_unavailable")


async def _publish_pending(session: AsyncSession, request: Request, run: Run) -> None:
    transport = _transport()
    if transport is None or not request.app.state.settings.agent_conversations_enabled:
        return
    pending = (
        await session.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.scope == str(run.id),
                ConversationMessage.role == "user",
                ConversationMessage.attempt_no == run.attempt_count,
                ConversationMessage.status == "queued",
            )
            .order_by(ConversationMessage.created_at)
        )
    ).all()
    if not pending:
        return
    try:
        transport.publish_messages(
            request.app.state.settings.runs_root,
            str(run.id),
            run.attempt_count,
            [
                {"id": str(x.id), "content": x.content, "created_at": x.created_at.isoformat()}
                for x in pending
            ],
        )
    except OSError as exc:
        for message in pending:
            message.status, message.error = "failed", str(exc)[:1000]
        await session.commit()


async def _response(session: AsyncSession, request: Request, scope: str, run: Run | None) -> dict:
    available, reason = True, None
    if run is not None:
        available, reason = await _reconcile_run(session, request, run)
        if available:
            await _publish_pending(session, request, run)
    else:
        settings = request.app.state.settings
        import os

        await session.execute(
            update(ConversationMessage)
            .where(
                ConversationMessage.scope == "orchestrator",
                ConversationMessage.status == "queued",
                ConversationMessage.created_at < datetime.now(UTC) - timedelta(minutes=5),
            )
            .values(
                status="failed",
                error="The response was interrupted. Check recorded actions before sending again.",
            )
        )
        await session.commit()

        available = bool(
            settings.conversation_model
            and settings.conversation_api_key_file
            and os.path.isfile(settings.conversation_api_key_file)
        )
        reason = None if available else "conversation_credentials_unavailable"
    messages = list(
        reversed(
            (
                await session.scalars(
                    select(ConversationMessage)
                    .where(ConversationMessage.scope == scope)
                    .order_by(desc(ConversationMessage.created_at), desc(ConversationMessage.id))
                    .limit(200)
                )
            ).all()
        )
    )
    return {
        "messages": [_out(m) for m in messages],
        "available": available,
        "unavailable_reason": reason,
    }


@conversations_router.get("/conversations/{scope}")
async def get_conversation(
    scope: str,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict:
    run = await _scope_run(session, scope)
    if run is not None:
        scope = str(run.id)
    return await _response(session, request, scope, run)


@conversations_router.post("/conversations/{scope}/messages")
async def post_message(
    scope: str,
    body: MessageIn,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict:
    if not body.content.strip():
        raise HTTPException(422, "A message cannot be empty.")
    run = await _scope_run(session, scope)
    if run is not None:
        scope = str(run.id)
    existing = await session.scalar(
        select(ConversationMessage).where(
            ConversationMessage.scope == scope, ConversationMessage.client_id == body.client_id
        )
    )
    # A successful idempotent replay is a read. Failed orchestrator requests
    # deliberately use the same client id as their retry key.
    if existing is not None:
        if existing.content != body.content:
            raise HTTPException(409, "This message ID was already used for different content.")
        return await _response(session, request, scope, run)
    if run is not None:
        available, _ = await _reconcile_run(session, request, run)
        if not available:
            raise HTTPException(409, "This agent session is not accepting messages.")
    else:
        # Short transaction lock serializes claiming the shared orchestrator,
        # never the provider request. The queued row is the durable busy flag.
        await session.execute(text("SELECT pg_advisory_xact_lock(9042101)"))
        pending = await session.scalar(
            select(ConversationMessage)
            .where(
                ConversationMessage.scope == scope,
                ConversationMessage.status == "queued",
                ConversationMessage.created_at > datetime.now(UTC) - timedelta(minutes=5),
            )
            .limit(1)
        )
        if pending:
            if pending.client_id == body.client_id:
                await session.commit()
                return await _response(session, request, scope, None)
            raise HTTPException(409, "Werft is answering another message. Wait for its reply.")
    if existing is None:
        message = ConversationMessage(
            scope=scope,
            client_id=body.client_id,
            run_id=run.id if run else None,
            attempt_no=run.attempt_count if run else None,
            role="user",
            content=body.content,
            status="queued",
        )
        session.add(message)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await _response(session, request, scope, run)
    if run is not None:
        await _publish_pending(session, request, run)
        return await _response(session, request, scope, run)
    # Commit before external I/O. A provider failure turns this one request into
    # a durable failed message, so no user input is silently stranded queued.
    history = (
        await session.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.scope == scope)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(30)
        )
    ).all()
    # The snapshot query opens a transaction. Finish it before the potentially
    # 30-second provider request; tool application starts a fresh transaction.
    system_context = await context(session)
    await session.commit()

    async def execute_tool(name: str, arguments: dict) -> str:
        outcome, direction = await apply_tool(session, name, arguments)
        if direction:
            target = await session.get(Run, UUID(direction["run_id"]))
            available, _ = await _reconcile_run(session, request, target)
            if not available:
                outcome = "Direction rejected: this agent is not accepting messages."
            else:
                queued = ConversationMessage(
                    scope=str(target.id),
                    run_id=target.id,
                    attempt_no=target.attempt_count,
                    role="user",
                    content=direction["content"],
                    status="queued",
                )
                session.add(queued)
                await session.commit()
                await _publish_pending(session, request, target)
                await session.refresh(queued)
                outcome = (
                    f"Direction queued for task {target.id}; awaiting the agent's acknowledgement."
                    if queued.status == "queued"
                    else f"Direction could not be delivered to task {target.id}."
                )
        session.add(
            ConversationMessage(scope=scope, role="system", content=outcome, status="answered")
        )
        await session.commit()
        return outcome

    try:
        answer, outcomes, steering = await ask(
            session,
            api_key_file=request.app.state.settings.conversation_api_key_file,
            model=request.app.state.settings.conversation_model,
            history=[
                {"role": m.role, "content": m.content}
                for m in reversed(history)
                if m.role in {"user", "assistant"}
            ],
            system_context=system_context,
            execute_tool=execute_tool,
        )
        session.add(
            ConversationMessage(scope=scope, role="assistant", content=answer, status="answered")
        )
        for outcome in outcomes:
            session.add(
                ConversationMessage(scope=scope, role="system", content=outcome, status="answered")
            )
        for direction in steering:
            target = await session.get(Run, UUID(direction["run_id"]))
            if target is not None and target.status == "running":
                session.add(
                    ConversationMessage(
                        scope=str(target.id),
                        run_id=target.id,
                        attempt_no=target.attempt_count,
                        role="user",
                        content=direction["content"],
                        status="queued",
                    )
                )
        await session.execute(
            update(ConversationMessage)
            .where(
                ConversationMessage.scope == scope, ConversationMessage.client_id == body.client_id
            )
            .values(status="answered")
        )
        await session.commit()
        for direction in steering:
            target = await session.get(Run, UUID(direction["run_id"]))
            if target is not None:
                await _publish_pending(session, request, target)
    except ConversationUnavailable as exc:
        await session.rollback()
        await session.execute(
            update(ConversationMessage)
            .where(
                ConversationMessage.scope == scope, ConversationMessage.client_id == body.client_id
            )
            .values(status="failed", error=str(exc))
        )
        await session.commit()
    except Exception:
        await session.rollback()
        await session.execute(
            update(ConversationMessage)
            .where(
                ConversationMessage.scope == scope, ConversationMessage.client_id == body.client_id
            )
            .values(status="failed", error="provider_request_failed")
        )
        await session.commit()
    return await _response(session, request, scope, None)
