import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from werft.runner.conversation import conversation_ready, publish_messages, read_messages

sys.path.insert(0, str(Path(__file__).parents[3] / "runners" / "adapter"))
from werft_adapter.main import run_cli  # noqa: E402


def layout(tmp_path):
    run_id = str(uuid4())
    folder = tmp_path / run_id
    (folder / "outputs").mkdir(parents=True)
    (folder / "secrets").mkdir()
    (folder / "outputs" / "conversation-ready.json").write_text(
        json.dumps({"attempt": 1, "available": True})
    )
    return run_id, folder


def test_messages_are_attempt_scoped_and_published_atomically(tmp_path):
    run_id, folder = layout(tmp_path)
    message = {"id": str(uuid4()), "content": "Use blue accents"}
    publish_messages(str(tmp_path), run_id, 1, [message])
    assert json.loads((folder / "secrets" / "operator_messages.json").read_text()) == {
        "attempt": 1,
        "messages": [message],
    }
    with pytest.raises(OSError):
        publish_messages(str(tmp_path), run_id, 2, [message])
    another = {"id": str(uuid4()), "content": "Keep the layout"}
    publish_messages(str(tmp_path), run_id, 1, [message, another])
    publish_messages(str(tmp_path), run_id, 1, [message])
    assert (
        len(json.loads((folder / "secrets" / "operator_messages.json").read_text())["messages"])
        == 2
    )
    assert not list((folder / "secrets").glob("*.tmp"))


def test_reader_filters_invalid_and_other_attempt_records(tmp_path):
    run_id, folder = layout(tmp_path)
    valid = {
        "id": str(uuid4()),
        "role": "assistant",
        "status": "answered",
        "content": "Updated",
        "attempt": 1,
    }
    (folder / "outputs" / "conversation.jsonl").write_text(
        "invalid\n" + json.dumps(valid) + "\n" + json.dumps({**valid, "attempt": 2})
    )
    assert read_messages(str(tmp_path), run_id, 1) == [valid]


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow boundary")
def test_untrusted_output_directory_cannot_link_to_host(tmp_path):
    run_id = str(uuid4())
    (tmp_path / run_id).mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "conversation-ready.json").write_text('{"attempt":1,"available":true}')
    (tmp_path / run_id / "outputs").symlink_to(outside, target_is_directory=True)
    assert not conversation_ready(str(tmp_path), run_id, 1)
    assert read_messages(str(tmp_path), run_id, 1) == []


def test_adapter_delivers_once_at_turn_boundary_and_records_real_reply(tmp_path):
    run_id, folder = layout(tmp_path)
    message = {"id": str(uuid4()), "content": "Use BLUE_SECRET accents"}
    publish_messages(str(tmp_path), run_id, 1, [message, message])
    script = tmp_path / "fake_cli.py"
    script.write_text("""import json, sys
for line in sys.stdin:
    packet = json.loads(line)
    print(json.dumps(packet), flush=True)
    print(json.dumps({"type":"result", "result": "Reply: " + packet["message"]["content"],
                      "is_error":False}), flush=True)
""")
    code, _ = run_cli(
        [sys.executable, str(script)],
        dict(os.environ),
        log_path=str(folder / "outputs" / "log.jsonl"),
        ceiling_seconds=10,
        cwd=str(tmp_path),
        secrets=["BLUE_SECRET"],
        conversation={
            "prompt": "Initial task",
            "attempt": 1,
            "outputs": str(folder / "outputs"),
            "inbox": str(folder / "secrets" / "operator_messages.json"),
            "grace": 0.1,
        },
    )
    assert code == 0
    records = read_messages(str(tmp_path), run_id, 1)
    statuses = [item["status"] for item in records if item["id"] == message["id"]]
    assert statuses == ["delivered", "answered"]
    replies = [item["content"] for item in records if item["role"] == "assistant"]
    assert len(replies) == 2
    assert "BLUE_SECRET" not in json.dumps(records)
    assert not conversation_ready(str(tmp_path), run_id, 1)


def test_adapter_does_not_replay_a_previous_attempt_inbox(tmp_path):
    run_id, folder = layout(tmp_path)
    publish_messages(str(tmp_path), run_id, 1, [{"id": str(uuid4()), "content": "Old direction"}])
    script = tmp_path / "echo.py"
    script.write_text("""import json, sys
for line in sys.stdin:
    packet = json.loads(line)
    print(json.dumps({"type":"result", "result":packet["message"]["content"]}), flush=True)
""")
    run_cli(
        [sys.executable, str(script)],
        dict(os.environ),
        log_path=str(folder / "outputs" / "log.jsonl"),
        ceiling_seconds=10,
        cwd=str(tmp_path),
        conversation={
            "prompt": "New attempt",
            "attempt": 2,
            "outputs": str(folder / "outputs"),
            "inbox": str(folder / "secrets" / "operator_messages.json"),
            "grace": 0.1,
        },
    )
    assert [item["content"] for item in read_messages(str(tmp_path), run_id, 2)] == ["New attempt"]
