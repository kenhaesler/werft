"""Turn-boundary operator messages for Claude's documented JSONL input mode.

The manager owns the read-only inbox. Messages stay queued until the current
turn ends; a user echo or a result is the evidence of delivery. No raw stdin
text, implicit interruption, or cross-attempt continuation is used.
"""

import contextlib
import json
import os
import threading
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from werft_adapter.atomic import write_json_atomic

UNREADABLE = (OSError, ValueError, TypeError, KeyError)


class ConversationInput:
    def __init__(self, process, *, prompt, attempt, outputs, inbox, redact, grace=2.0):
        self.process = process
        self.prompt = prompt
        self.attempt = attempt
        self.outputs = outputs
        self.inbox = inbox
        self.redact = redact
        self.grace = grace
        self.stop = threading.Event()
        self.turn_done = threading.Event()
        self.lock = threading.Lock()
        self.seen = set()
        self.active = None
        self.thread = threading.Thread(target=self._feed, daemon=True)

    def _ready(self, value):
        write_json_atomic(
            os.path.join(self.outputs, "conversation-ready.json"),
            {"attempt": self.attempt, "available": value},
        )

    def _emit(self, *, id, role, content, status):
        content = self.redact(content)
        if len(content) > 16000:
            content = content[:15960] + "\n[Response truncated; see session output.]"
        item = {
            "id": id,
            "role": role,
            "content": content,
            "status": status,
            "attempt": self.attempt,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with open(os.path.join(self.outputs, "conversation.jsonl"), "a", encoding="utf-8") as log:
            log.write(json.dumps(item) + "\n")

    def _send(self, content, id):
        packet = {
            "type": "user",
            "uuid": id,
            "session_id": "",
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": content},
        }
        self.process.stdin.write(json.dumps(packet) + "\n")
        self.process.stdin.flush()

    def _pending(self):
        try:
            with open(self.inbox, encoding="utf-8") as handle:
                payload = json.loads(handle.read(4 * 1024 * 1024 + 1))
            if payload.get("attempt") != self.attempt:
                return []
            pending = []
            for item in payload.get("messages", []):
                UUID(item["id"])
                if (
                    item["id"] not in self.seen
                    and isinstance(item.get("content"), str)
                    and 0 < len(item["content"]) <= 16000
                ):
                    pending.append(item)
            return pending
        except UNREADABLE:
            return []

    def start(self):
        self._ready(True)
        self.thread.start()

    def _feed(self):
        try:
            self._send(self.prompt, str(uuid4()))
            while not self.stop.is_set():
                if not self.turn_done.wait(0.1):
                    continue
                deadline = time.monotonic() + self.grace
                pending = self._pending()
                while not pending and not self.stop.is_set() and time.monotonic() < deadline:
                    self.stop.wait(0.1)
                    pending = self._pending()
                if not pending or self.stop.is_set():
                    break
                with self.lock:
                    self.active = pending[0]
                    self.seen.add(self.active["id"])
                    self.turn_done.clear()
                    self._send(self.active["content"], self.active["id"])
        except (OSError, ValueError):
            with self.lock:
                if self.active:
                    self._emit(
                        **{key: self.active[key] for key in ("id", "content")},
                        role="user",
                        status="failed",
                    )
        finally:
            self._ready(False)
            with contextlib.suppress(OSError, ValueError):
                self.process.stdin.close()

    def on_line(self, line):
        try:
            item = json.loads(line)
        except ValueError:
            return
        if not isinstance(item, dict):
            return
        with self.lock:
            if item.get("type") == "user" and self.active and item.get("uuid") == self.active["id"]:
                self._emit(
                    id=self.active["id"],
                    role="user",
                    content=self.active["content"],
                    status="delivered",
                )
            if item.get("type") == "result":
                failed = bool(item.get("is_error"))
                if self.active:
                    self._emit(
                        id=self.active["id"],
                        role="user",
                        content=self.active["content"],
                        status="failed" if failed else "answered",
                    )
                response = item.get("result")
                if isinstance(response, str) and response:
                    self._emit(
                        id=str(uuid4()), role="assistant", content=response, status="answered"
                    )
                self.active = None
                self.turn_done.set()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self._ready(False)
