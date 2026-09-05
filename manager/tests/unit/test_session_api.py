import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from werft.api.session import LOG_CHUNK_BYTES, _read_log


def _log_path(root: Path, run_id: UUID) -> Path:
    path = root / str(run_id) / "outputs" / "log.jsonl"
    path.parent.mkdir(parents=True)
    return path


def test_log_initial_tail_and_append_cursor(tmp_path: Path) -> None:
    run_id = uuid4()
    path = _log_path(tmp_path, run_id)
    path.write_bytes(b"a" * (LOG_CHUNK_BYTES + 10))
    initial = _read_log(str(tmp_path), run_id, attempt_no=2, offset=None, generation=None)
    assert initial["available"] and initial["reset"] and initial["truncated"]
    assert len(initial["content"]) == LOG_CHUNK_BYTES
    with path.open("ab") as handle:
        handle.write(b"next")
    appended = _read_log(
        str(tmp_path),
        run_id,
        attempt_no=2,
        offset=initial["next_offset"],
        generation=initial["generation"],
    )
    assert appended["content"] == "next"
    assert not appended["reset"]


def test_log_same_size_replacement_and_new_attempt_reset(tmp_path: Path) -> None:
    run_id = uuid4()
    path = _log_path(tmp_path, run_id)
    path.write_text("first", encoding="utf-8")
    initial = _read_log(str(tmp_path), run_id, attempt_no=1, offset=None, generation=None)
    replacement = path.with_suffix(".new")
    replacement.write_text("other", encoding="utf-8")
    os.replace(replacement, path)
    replaced = _read_log(
        str(tmp_path), run_id, attempt_no=1, offset=5, generation=initial["generation"]
    )
    assert replaced["reset"] and replaced["content"] == "other"
    new_attempt = _read_log(
        str(tmp_path), run_id, attempt_no=2, offset=5, generation=replaced["generation"]
    )
    assert new_attempt["reset"] and new_attempt["content"] == "other"


def test_log_rejects_symlink_and_missing(tmp_path: Path) -> None:
    run_id = uuid4()
    assert (
        _read_log(str(tmp_path), run_id, attempt_no=None, offset=None, generation=None)["reason"]
        == "output_not_available"
    )
    path = _log_path(tmp_path, run_id)
    target = tmp_path / "target.log"
    target.write_text("secret", encoding="utf-8")
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation requires a Windows developer privilege")
    assert (
        _read_log(str(tmp_path), run_id, attempt_no=None, offset=None, generation=None)["reason"]
        == "unsafe_output"
    )


def test_log_rejects_symlinked_outputs_directory(tmp_path: Path) -> None:
    run_id = uuid4()
    run_dir = tmp_path / str(run_id)
    run_dir.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "log.jsonl").write_text("secret", encoding="utf-8")
    try:
        (run_dir / "outputs").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires a Windows developer privilege")
    assert (
        _read_log(str(tmp_path), run_id, attempt_no=None, offset=None, generation=None)["reason"]
        == "unsafe_output"
    )


def test_log_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on Windows")
    run_id = uuid4()
    path = _log_path(tmp_path, run_id)
    os.mkfifo(path)
    assert (
        _read_log(str(tmp_path), run_id, attempt_no=None, offset=None, generation=None)["reason"]
        == "unsafe_output"
    )
