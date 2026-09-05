"""Authenticated per-run session runtime and bounded raw-output endpoints."""

import asyncio
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.routes import get_session
from werft.db.models import Run, RunAttempt

session_router = APIRouter()
LOG_CHUNK_BYTES = 64 * 1024


def _unavailable(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "content": "",
        "next_offset": None,
        "generation": None,
        "reset": False,
        "truncated": False,
        "has_more": False,
    }


def _read_from_fd(
    fd: int, *, attempt_no: int | None, offset: int | None, generation: str | None
) -> dict:
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        return _unavailable("unsafe_output")
    current_generation = f"{attempt_no or 0:x}-{file_stat.st_dev:x}-{file_stat.st_ino:x}"
    size = file_stat.st_size
    reset = generation is not None and generation != current_generation
    if offset is None or generation is None or reset or offset > size:
        start, reset = max(0, size - LOG_CHUNK_BYTES), True
    else:
        start = offset
    end = min(size, start + LOG_CHUNK_BYTES)
    os.lseek(fd, start, os.SEEK_SET)
    parts: list[bytes] = []
    remaining = end - start
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    content = b"".join(parts).decode("utf-8", errors="replace")
    return {
        "available": True,
        "reason": None,
        "content": content,
        "next_offset": start + len(b"".join(parts)),
        "generation": current_generation,
        "reset": reset,
        "truncated": start > 0,
        "has_more": end < size,
    }


def _read_log_posix(
    runs_root: str,
    run_id: UUID,
    *,
    attempt_no: int | None,
    offset: int | None,
    generation: str | None,
) -> dict:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fds: list[int] = []
    try:
        fds.append(os.open(runs_root, directory_flags))
        fds.append(os.open(str(run_id), directory_flags, dir_fd=fds[-1]))
        fds.append(os.open("outputs", directory_flags, dir_fd=fds[-1]))
        fds.append(os.open("log.jsonl", file_flags, dir_fd=fds[-1]))
        return _read_from_fd(fds[-1], attempt_no=attempt_no, offset=offset, generation=generation)
    except FileNotFoundError:
        return _unavailable("output_not_available")
    except OSError:
        return _unavailable("unsafe_output")
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _read_log_windows(
    runs_root: str,
    run_id: UUID,
    *,
    attempt_no: int | None,
    offset: int | None,
    generation: str | None,
) -> dict:
    path = Path(runs_root) / str(run_id) / "outputs" / "log.jsonl"
    try:
        for candidate in (path.parent.parent, path.parent, path):
            entry = candidate.lstat()
            if getattr(entry, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                return _unavailable("unsafe_output")
        before = path.stat()
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            opened = os.fstat(fd)
            if not os.path.samestat(before, opened):
                return _unavailable("unsafe_output")
            return _read_from_fd(fd, attempt_no=attempt_no, offset=offset, generation=generation)
        finally:
            os.close(fd)
    except FileNotFoundError:
        return _unavailable("output_not_available")
    except OSError:
        return _unavailable("unsafe_output")


def _read_log(
    runs_root: str,
    run_id: UUID,
    *,
    attempt_no: int | None,
    offset: int | None,
    generation: str | None,
) -> dict:
    if os.name == "nt":
        return _read_log_windows(
            runs_root, run_id, attempt_no=attempt_no, offset=offset, generation=generation
        )
    return _read_log_posix(
        runs_root, run_id, attempt_no=attempt_no, offset=offset, generation=generation
    )


async def _run_or_404(session: AsyncSession, run_id: UUID) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


async def _latest_attempt(session: AsyncSession, run_id: UUID) -> RunAttempt | None:
    return await session.scalar(
        select(RunAttempt)
        .where(RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
    )


@session_router.get("/runs/{run_id}/runtime")
async def runtime(
    run_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict:  # noqa: B008
    run = await _run_or_404(session, run_id)
    attempt = await _latest_attempt(session, run_id)
    activity = request.app.state.activity.snapshot()
    return {
        "generated_at": datetime.now(UTC),
        "manager_available": activity["available"],
        "attended": activity["available"] and run_id in activity["live_driver_run_ids"],
        "attempt_no": attempt.attempt_no if attempt else None,
        "run": {
            "run_id": run.id,
            "status": run.status,
            "provider": attempt.provider if attempt else run.provider,
            "container_id": run.container_id,
            "attempt_started_at": attempt.started_at if attempt else None,
            "last_heartbeat_at": run.last_heartbeat_at,
            "lease_expires_at": run.lease_expires_at,
            "hard_deadline_at": run.hard_deadline_at,
            "next_attempt_at": run.next_attempt_at,
            "parked_reason": run.parked_reason,
            "updated_at": run.updated_at,
        },
    }


@session_router.get("/runs/{run_id}/log")
async def log(
    run_id: UUID,
    request: Request,
    offset: int | None = Query(None, ge=0),
    generation: str | None = Query(None, max_length=200),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict:  # noqa: B008
    await _run_or_404(session, run_id)
    attempt = await _latest_attempt(session, run_id)
    return await asyncio.to_thread(
        _read_log,
        request.app.state.settings.runs_root,
        run_id,
        attempt_no=attempt.attempt_no if attempt else None,
        offset=offset,
        generation=generation,
    )
