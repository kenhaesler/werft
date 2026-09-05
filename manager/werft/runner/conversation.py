"""Bounded, attempt-scoped operator input over the existing read-only secrets mount.

Runner output is untrusted evidence, never an authorization source. Directory-fd
traversal prevents a replaced output directory or symlink from reading host files.
"""

import contextlib
import json
import os
import stat
import threading
from pathlib import Path
from uuid import UUID, uuid4

MAX_BYTES = 4 * 1024 * 1024
_PUBLISH_LOCK = threading.Lock()


@contextlib.contextmanager
def _directory(root: str, run_id: str, child: str):
    run_id = str(UUID(str(run_id)))
    fds: list[int] = []
    try:
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fds.append(os.open(root, flags))
            fds.append(os.open(run_id, flags, dir_fd=fds[-1]))
            fds.append(os.open(child, flags, dir_fd=fds[-1]))
            yield fds[-1], None
        else:
            base = Path(root).resolve(strict=True)
            target = base / run_id / child
            for part in (base / run_id, target):
                if part.is_symlink() or part.is_junction():
                    raise OSError("Unsafe conversation directory")
            target.resolve(strict=True).relative_to(base)
            yield None, target
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _read(root: str, run_id: str, filename: str) -> bytes:
    with _directory(root, run_id, "outputs") as (directory, path):
        target = filename if directory is not None else str(path / filename)
        if directory is None and Path(target).is_symlink():
            raise OSError("Unsafe conversation file")
        fd = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory,
        )
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise OSError("Conversation output exceeds limits")
            return handle.read(MAX_BYTES)


def conversation_ready(runs_root: str, run_id: str, attempt: int) -> bool:
    try:
        data = json.loads(_read(runs_root, run_id, "conversation-ready.json"))
        return data.get("attempt") == attempt and data.get("available") is True
    except OSError, ValueError, AttributeError:
        return False


def publish_messages(runs_root: str, run_id: str, attempt: int, messages: list[dict]) -> None:
    with _PUBLISH_LOCK:
        _publish_messages(runs_root, run_id, attempt, messages)


def _publish_messages(runs_root: str, run_id: str, attempt: int, messages: list[dict]) -> None:
    if not conversation_ready(runs_root, run_id, attempt):
        raise OSError("This agent session is not accepting messages")
    with _directory(runs_root, run_id, "secrets") as (directory, path):
        name = f".operator-{uuid4()}.tmp"
        temp = name if directory is not None else str(path / name)
        target = (
            "operator_messages.json"
            if directory is not None
            else str(path / "operator_messages.json")
        )
        previous = []
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            with os.fdopen(fd, "rb") as handle:
                payload = json.loads(handle.read(MAX_BYTES + 1))
            if payload.get("attempt") == attempt:
                previous = payload["messages"]
        except FileNotFoundError:
            pass
        # A stale database snapshot may republish an older subset. Never remove
        # already-published messages; the adapter deduplicates IDs per attempt.
        merged = {item["id"]: item for item in previous}
        for item in messages:
            merged.setdefault(item["id"], item)
        data = json.dumps({"attempt": attempt, "messages": list(merged.values())}).encode("utf-8")
        if len(data) > MAX_BYTES:
            raise OSError("Conversation queue exceeds limits")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=directory)


def read_messages(runs_root: str, run_id: str, attempt: int) -> list[dict]:
    try:
        raw = _read(runs_root, run_id, "conversation.jsonl")
    except OSError:
        return []
    records = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("attempt") != attempt:
                continue
            UUID(item["id"])
            if item.get("role") not in {"user", "assistant"}:
                continue
            if item.get("status") not in {"delivered", "answered", "failed"}:
                continue
            if not isinstance(item.get("content"), str) or len(item["content"]) > 16000:
                continue
            records.append(item)
        except ValueError, KeyError, TypeError:
            continue
    return records
