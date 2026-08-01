"""The reader must survive a hostile output tree (SPEC §4.3/§8, [CD M8])."""

import os

import pytest

from werft.runner.collect import collect_outputs


@pytest.fixture
def trees(tmp_path):
    src = tmp_path / "outputs"
    dest = tmp_path / "collected"
    src.mkdir()
    return src, dest


def symlink_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink creation not permitted on this platform")


def test_regular_files_are_copied(trees):
    src, dest = trees
    (src / "result.json").write_text('{"status": "success"}')
    (src / "nested").mkdir()
    (src / "nested" / "log.jsonl").write_text("line\n")

    report = collect_outputs(str(src), str(dest))

    assert {a.rel_path.replace(os.sep, "/") for a in report.artifacts} == {
        "result.json",
        "nested/log.jsonl",
    }
    assert (dest / "result.json").read_text() == '{"status": "success"}'
    assert report.truncated is False
    assert report.dropped == []


def test_symlink_to_a_host_secret_copies_zero_bytes(trees, tmp_path):
    """Issue #20 acceptance: a symlink-attack tree yields zero bytes copied."""
    src, dest = trees
    secret = tmp_path / "github-app.pem"
    secret.write_text("PRIVATE KEY MATERIAL")
    symlink_or_skip(src / "report.html", secret)

    report = collect_outputs(str(src), str(dest))

    assert report.artifacts == []
    assert report.bytes_total == 0
    assert [d.reason for d in report.dropped] == ["not_regular"]
    assert not (dest / "report.html").exists()


def test_symlinked_directory_is_not_descended(trees, tmp_path):
    """`ln -s / big` must not turn collection into a filesystem walk."""
    src, dest = trees
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "secret.txt").write_text("x" * 1000)
    symlink_or_skip(src / "big", outside, directory=True)
    (src / "ok.txt").write_text("fine")

    report = collect_outputs(str(src), str(dest))

    assert [a.rel_path for a in report.artifacts] == ["ok.txt"]
    assert any(d.reason == "not_regular" for d in report.dropped)
    assert not (dest / "big").exists()


def test_fifo_is_skipped_not_opened(trees):
    src, dest = trees
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    os.mkfifo(src / "pipe")  # opening this would block forever
    (src / "ok.txt").write_text("fine")

    report = collect_outputs(str(src), str(dest))

    assert [a.rel_path for a in report.artifacts] == ["ok.txt"]
    assert [d.reason for d in report.dropped] == ["not_regular"]


def test_oversized_file_is_dropped_and_recorded(trees):
    src, dest = trees
    (src / "huge.bin").write_bytes(b"x" * 2048)
    (src / "small.txt").write_text("ok")

    report = collect_outputs(str(src), str(dest), max_file_bytes=1024)

    assert [a.rel_path for a in report.artifacts] == ["small.txt"]
    assert report.truncated is True
    dropped = [d for d in report.dropped if d.rel_path == "huge.bin"]
    assert dropped and dropped[0].reason == "too_large" and dropped[0].size_bytes == 2048


def test_running_total_cap_stops_collection_and_names_what_was_dropped(trees):
    """SPEC §8: over-cap drops are recorded — evidence that says 'truncated here'."""
    src, dest = trees
    for i in range(5):
        (src / f"f{i}.bin").write_bytes(b"x" * 400)

    report = collect_outputs(str(src), str(dest), max_total_bytes=1000, max_file_bytes=10_000)

    assert report.bytes_total <= 1000
    assert report.truncated is True
    assert len(report.artifacts) == 2
    assert len(report.dropped) == 3
    assert all(d.reason == "total_cap" for d in report.dropped)
    assert all(d.size_bytes == 400 for d in report.dropped)


def test_over_cap_eviction_is_largest_first_not_walk_order(trees):
    """SPEC §8: "over-cap drops largest-first".

    Encounter-order eviction would hand the choice to the agent, which picks the
    filenames and therefore the walk order. Names here are deliberately ordered
    so that walk order and size order disagree.
    """
    src, dest = trees
    (src / "a_huge.bin").write_bytes(b"x" * 900)  # first in walk order, largest
    (src / "b_small.bin").write_bytes(b"x" * 100)
    (src / "c_small.bin").write_bytes(b"x" * 100)

    report = collect_outputs(str(src), str(dest), max_total_bytes=1000, max_file_bytes=10_000)

    kept = {a.rel_path for a in report.artifacts}
    assert kept == {"b_small.bin", "c_small.bin"}, "the two small files must survive"
    assert [d.rel_path for d in report.dropped] == ["a_huge.bin"]
    assert report.truncated is True


def test_eviction_is_deterministic_regardless_of_name(trees):
    """Equal-size files evict by a stable rule, not by whatever the agent named them."""
    src, dest = trees
    for name in ("zzz.bin", "aaa.bin", "mmm.bin"):
        (src / name).write_bytes(b"x" * 400)

    first = collect_outputs(str(src), str(dest), max_total_bytes=800, max_file_bytes=10_000)
    second = collect_outputs(str(src), str(dest), max_total_bytes=800, max_file_bytes=10_000)

    assert [a.rel_path for a in first.artifacts] == [a.rel_path for a in second.artifacts]


def test_an_unreadable_entry_does_not_abort_the_whole_collection(trees, monkeypatch):
    """A root agent can delete or swap an entry mid-walk. Losing one file must not
    cost the run its entire evidence trail."""
    src, dest = trees
    (src / "good.txt").write_text("keep me")
    (src / "vanishes.txt").write_text("gone by the time we stat it")

    real_lstat = os.lstat

    def flaky_lstat(path, *args, **kwargs):
        if str(path).endswith("vanishes.txt"):
            raise OSError(2, "No such file or directory")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", flaky_lstat)
    report = collect_outputs(str(src), str(dest))

    assert [a.rel_path for a in report.artifacts] == ["good.txt"]
    assert any(d.rel_path == "vanishes.txt" and d.reason == "unreadable" for d in report.dropped)


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX modes are not real on Windows (only the read-only bit)"
)
def test_modes_are_stripped(trees):
    src, dest = trees
    path = src / "exec.sh"
    path.write_text("#!/bin/sh\n")
    os.chmod(path, 0o777)

    collect_outputs(str(src), str(dest))

    mode = os.stat(dest / "exec.sh").st_mode & 0o777
    assert mode == 0o644, "collected evidence is never executable"


def test_empty_tree_is_fine(trees):
    src, dest = trees
    report = collect_outputs(str(src), str(dest))
    assert report.artifacts == []
    assert report.bytes_total == 0
    assert report.truncated is False
