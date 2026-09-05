#!/usr/bin/env python3
"""Smoke-test Claude Code 2.1.220's JSONL input against the Werft bridge.

Run from the repository root (the command below intentionally uses the pinned
runner image, not a developer's installed Claude Code):

  docker run --rm --network none -v "$PWD:/repo:ro" -w /tmp \
    werft-runner-base:2026-08-01 python3.12 /repo/scripts/check-conversation-cli.py

The server is in this process and Docker networking is disabled.  The fixture
uses a dummy API key and an Anthropic Messages SSE response; no credentials or
model requests leave the container.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runners" / "adapter"))

from werft_adapter.main import run_cli  # noqa: E402


class _SseHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - HTTPServer callback name
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        type(self).requests.append(body)
        turn = len(type(self).requests)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": f"msg_{turn}", "type": "message", "role": "assistant",
                "model": "fixture", "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0},
            }}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": f"fixture reply {turn}"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        for event, data in events:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def main() -> None:
    server = HTTPServer(("127.0.0.1", 0), _SseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outputs = root / "outputs"
            outputs.mkdir()
            inbox = root / "operator_messages.json"
            followup_id = str(uuid4())
            inbox.write_text(json.dumps({"attempt": 1, "messages": [{
                "id": followup_id, "content": "Follow up from the operator",
            }]}), encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "ANTHROPIC_API_KEY": "fixture-key-not-a-secret",
                "CI": "true",
                "TERM": "dumb",
            })
            argv = [
                "claude", "-p", "--bare", "--tools", "", "--permission-mode", "default",
                "--model", "fixture", "--input-format", "stream-json", "--output-format",
                "stream-json", "--verbose", "--replay-user-messages",
            ]
            code, stderr = run_cli(
                argv, env, log_path=str(outputs / "log.jsonl"), ceiling_seconds=30,
                cwd=str(root), conversation={
                    "prompt": "Initial fixture task", "attempt": 1, "outputs": str(outputs),
                    "inbox": str(inbox), "grace": 1.0,
                },
            )
            records = _records(outputs / "conversation.jsonl")
            statuses = [r["status"] for r in records if r["id"] == followup_id]
            replies = [r["content"] for r in records if r["role"] == "assistant"]
            if code != 0:
                raise AssertionError(f"Claude exited {code}: {stderr}")
            # 2.1.220 makes a title-generation request before each actual agent
            # turn, then makes the two task requests.  The bridge sees only the
            # latter two `result` messages.
            if len(_SseHandler.requests) != 4:
                raise AssertionError(
                    f"expected title + task requests for two turns (4), got "
                    f"{len(_SseHandler.requests)}; stderr={stderr!r}"
                )
            if statuses != ["delivered", "answered"]:
                raise AssertionError(f"follow-up UUID was not echoed and answered: {statuses!r}")
            if replies != ["fixture reply 2", "fixture reply 4"]:
                raise AssertionError(f"unexpected bridge replies: {replies!r}")
            if not all(request.get("stream") is True for request in _SseHandler.requests):
                raise AssertionError("Claude did not request streaming Messages responses")
            print("PASS: Claude 2.1.220 accepted JSONL input, replayed the follow-up UUID, and bridge recorded answered")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
