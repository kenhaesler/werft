"""The reader must survive a hostile output tree (SPEC §4.3/§8, [CD M8])."""

import os

import pytest

from werft.runner.collect import TreeSource, collect_outputs, collect_trees


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


def test_collect_trees_prefixes_and_merges(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "x.txt").write_bytes(b"aaaa")
    b = tmp_path / "b" / "nested"
    b.mkdir(parents=True)
    (b / "y.txt").write_bytes(b"bb")
    dest = tmp_path / "dest"
    report = collect_trees(
        [TreeSource(str(a), "outputs"), TreeSource(str(tmp_path / "b"), "test-results")],
        str(dest),
    )
    assert {c.rel_path for c in report.artifacts} == {"outputs/x.txt", "test-results/nested/y.txt"}
    assert (dest / "outputs" / "x.txt").read_bytes() == b"aaaa"
    assert report.bytes_total == 6 and not report.truncated


def test_collect_trees_missing_source_is_silent(tmp_path):
    dest = tmp_path / "dest"
    report = collect_trees([TreeSource(str(tmp_path / "absent"), "outputs")], str(dest))
    assert report.artifacts == [] and report.dropped == [] and not report.truncated


def test_collect_trees_budget_spans_sources_largest_first(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "big.bin").write_bytes(b"x" * 60)
    b = tmp_path / "b"
    b.mkdir()
    (b / "small.bin").write_bytes(b"x" * 30)
    report = collect_trees(
        [TreeSource(str(a), "s1"), TreeSource(str(b), "s2")],
        str(tmp_path / "dest"),
        max_total_bytes=80,
    )
    # global largest-first drop: the 60-byte file loses regardless of source order
    assert [c.rel_path for c in report.artifacts] == ["s2/small.bin"]
    assert [(d.rel_path, d.reason) for d in report.dropped] == [("s1/big.bin", "total_cap")]
    assert report.truncated


def test_collect_trees_overwrite_is_charged_only_its_delta(tmp_path):
    # D4: a retry re-collection of the same files must never be dropped as total_cap
    a = tmp_path / "a"
    a.mkdir()
    (a / "same.bin").write_bytes(b"x" * 70)
    dest = tmp_path / "dest"
    first = collect_trees([TreeSource(str(a), "s1")], str(dest), max_total_bytes=80)
    assert not first.truncated
    again = collect_trees(
        [TreeSource(str(a), "s1")],
        str(dest),
        max_total_bytes=80,
        existing={"s1/same.bin": 70},
    )
    assert [c.rel_path for c in again.artifacts] == ["s1/same.bin"]
    assert again.dropped == [] and not again.truncated


def test_collect_trees_dropped_entries_are_prefixed_too(tmp_path):
    """DroppedEntry rel_paths must carry the source prefix, same as artifacts."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "huge.bin").write_bytes(b"x" * 100)
    dest = tmp_path / "dest"
    report = collect_trees([TreeSource(str(a), "outputs")], str(dest), max_file_bytes=10)
    assert report.artifacts == []
    assert [d.rel_path for d in report.dropped] == ["outputs/huge.bin"]
    assert [d.reason for d in report.dropped] == ["too_large"]


def test_collect_trees_symlinked_source_root_is_dropped_not_followed(tmp_path):
    """Three of `EVIDENCE_SOURCES`' roots are *agent-created* directories inside
    the rw workspace, so `ln -s ../secrets .werft-artifacts` would redirect the
    whole walk into the manager's own secrets — collected before
    `remove_secrets` runs, into an HTTP-served store. The root is `lstat`ed
    (never resolved), so a link is a bounded no-op that leaves a drop behind.
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "git_token.txt").write_bytes(b"ghp_supersecret")

    root = tmp_path / "workspace" / ".werft-artifacts"
    root.parent.mkdir(parents=True)
    symlink_or_skip(root, secrets, directory=True)

    dest = tmp_path / "dest"
    report = collect_trees([TreeSource(str(root), "werft-artifacts")], str(dest))

    assert report.artifacts == []
    assert report.bytes_total == 0
    assert [(d.rel_path, d.reason) for d in report.dropped] == [("werft-artifacts", "not_regular")]
    assert not any(dest.rglob("*"))


def test_collect_trees_source_root_that_is_a_regular_file_is_dropped(tmp_path):
    root = tmp_path / "outputs"
    root.write_bytes(b"not a directory")
    dest = tmp_path / "dest"

    report = collect_trees([TreeSource(str(root), "outputs")], str(dest))

    assert report.artifacts == []
    assert [(d.rel_path, d.reason) for d in report.dropped] == [("outputs", "not_regular")]


def test_collect_trees_a_dropped_root_does_not_stop_its_siblings(tmp_path):
    good = tmp_path / "outputs"
    good.mkdir()
    (good / "log.jsonl").write_bytes(b"ab")
    bad = tmp_path / "test-results"
    bad.write_bytes(b"x")

    report = collect_trees(
        [TreeSource(str(bad), "test-results"), TreeSource(str(good), "outputs")],
        str(tmp_path / "dest"),
    )

    assert [c.rel_path for c in report.artifacts] == ["outputs/log.jsonl"]
    assert [(d.rel_path, d.reason) for d in report.dropped] == [("test-results", "not_regular")]


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX modes are not real on Windows (only the read-only bit)"
)
def test_collect_trees_creates_the_destination_0700(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    (src / "nested").mkdir()
    (src / "nested" / "x.txt").write_bytes(b"x")
    dest = tmp_path / "dest"

    collect_trees([TreeSource(str(src), "outputs")], str(dest))

    assert dest.stat().st_mode & 0o777 == 0o700
    assert (dest / "outputs").stat().st_mode & 0o077 == 0
