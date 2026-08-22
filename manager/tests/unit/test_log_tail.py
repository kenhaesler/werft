import json
import os

from werft.providers.claude import parse_stream
from werft.runner.outputs import read_log_tail


def test_the_tail_keeps_whole_lines_and_ends_on_the_result_envelope(tmp_path):
    """The result envelope is the LAST line; `log.jsonl` is agent-written and
    unbounded, so the reader is tail-bounded and must never emit a truncated
    JSON fragment as if it were a line (decision 16)."""
    path = tmp_path / "log.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(5000):
            handle.write(json.dumps({"type": "assistant", "i": i, "pad": "x" * 200}) + "\n")
        handle.write(json.dumps({"type": "result", "subtype": "success", "is_error": False}) + "\n")

    lines = read_log_tail(str(tmp_path), max_bytes=64 * 1024)

    assert 0 < len(lines) < 5001
    assert all(json.loads(line) for line in lines)  # no partial first line
    assert json.loads(lines[-1])["type"] == "result"
    assert parse_stream(lines).result == {"type": "result", "subtype": "success", "is_error": False}


def test_a_small_file_is_returned_whole(tmp_path):
    (tmp_path / "log.jsonl").write_text('{"type":"result","subtype":"success"}\n', encoding="utf-8")
    assert read_log_tail(str(tmp_path), max_bytes=1 << 20) == [
        '{"type":"result","subtype":"success"}'
    ]


def test_a_missing_file_is_empty(tmp_path):
    assert read_log_tail(str(tmp_path)) == []


def test_a_directory_named_log_jsonl_is_empty_not_an_exception(tmp_path):
    """The tree is agent-writable; `read_result` already applies exactly this
    `lstat` + `S_ISREG` discipline and this must not be weaker."""
    os.makedirs(tmp_path / "log.jsonl")
    assert read_log_tail(str(tmp_path)) == []


def test_invalid_utf8_is_replaced_not_raised(tmp_path):
    (tmp_path / "log.jsonl").write_bytes(b'{"type":"result"}\n\xff\xfe\n')
    assert read_log_tail(str(tmp_path))
