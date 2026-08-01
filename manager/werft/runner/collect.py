"""Host-side reader for agent-writable output trees (SPEC §4.3/§8, [CD M8]).

The manager holds every secret; the tree it is reading was writable by a root
agent. So this module never traverses a non-regular file, never follows a link,
and bounds what it reads — which is what turns "symlink a directory to /" from a
disk-fill into a bounded no-op.

Explicitly prohibited, because they are the shortcut reached for under pressure:
**never `cp -r`, never `rsync -a`, never `tar -C`** — every one of them follows
links or preserves modes.

Two properties are deliberate and were both wrong in the first draft:

* **Eviction is largest-first** (SPEC §8), not encounter order. A hostile agent
  chooses filenames and therefore chooses walk order, so encounter-order
  eviction would let it decide which evidence survives.
* **A vanishing or unreadable entry is skipped, never fatal.** The tree is
  writable by the agent right up to container death; one `OSError` must not
  abort collection and cost the run its entire evidence trail.
"""

import os
import shutil
import stat
from dataclasses import dataclass, field

MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class CollectedArtifact:
    rel_path: str
    size_bytes: int


@dataclass(frozen=True)
class DroppedEntry:
    rel_path: str
    #: 'not_regular' | 'too_large' | 'total_cap' | 'unreadable'
    reason: str
    size_bytes: int = 0


@dataclass
class CollectionReport:
    artifacts: list[CollectedArtifact] = field(default_factory=list)
    dropped: list[DroppedEntry] = field(default_factory=list)
    bytes_total: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class _Candidate:
    rel_path: str
    full_path: str
    size_bytes: int


def _scan(src_root: str, report: CollectionReport, max_file_bytes: int) -> list[_Candidate]:
    """Walk once, collecting regular-file candidates. Never follows a link."""
    candidates: list[_Candidate] = []

    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        # os.walk lists symlinked directories in dirnames; followlinks=False stops
        # recursion, but prune explicitly so the intent is on the page and a
        # symlinked directory is recorded rather than silently ignored.
        kept: list[str] = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            try:
                is_dir = stat.S_ISDIR(os.lstat(full).st_mode)
            except OSError:
                report.dropped.append(DroppedEntry(os.path.relpath(full, src_root), "unreadable"))
                continue
            if is_dir:
                kept.append(name)
            else:
                report.dropped.append(DroppedEntry(os.path.relpath(full, src_root), "not_regular"))
        dirnames[:] = kept

        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src_root)
            try:
                info = os.lstat(full)  # lstat: never resolves the link
            except OSError:
                # The agent could delete or replace an entry mid-walk. Skipping
                # one entry must never cost the run its whole evidence trail.
                report.dropped.append(DroppedEntry(rel, "unreadable"))
                continue

            if not stat.S_ISREG(info.st_mode):
                report.dropped.append(DroppedEntry(rel, "not_regular"))
                continue
            if info.st_size > max_file_bytes:
                report.dropped.append(DroppedEntry(rel, "too_large", info.st_size))
                report.truncated = True
                continue
            candidates.append(_Candidate(rel, full, info.st_size))

    return candidates


def collect_outputs(
    src_dir: str,
    dest_dir: str,
    *,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> CollectionReport:
    """Copy regular files from an agent-writable tree into Werft-owned storage."""
    report = CollectionReport()
    src_root = os.path.realpath(src_dir)
    os.makedirs(dest_dir, exist_ok=True)

    candidates = _scan(src_root, report, max_file_bytes)

    # SPEC §8: "over-cap drops largest-first". Copying smallest-first and dropping
    # whatever no longer fits is exactly that, and it is deterministic — the agent
    # cannot influence which evidence survives by choosing filenames.
    candidates.sort(key=lambda c: (c.size_bytes, c.rel_path))

    for candidate in candidates:
        if report.bytes_total + candidate.size_bytes > max_total_bytes:
            report.dropped.append(
                DroppedEntry(candidate.rel_path, "total_cap", candidate.size_bytes)
            )
            report.truncated = True
            continue

        target = os.path.join(dest_dir, candidate.rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            fd = os.open(candidate.full_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            report.dropped.append(DroppedEntry(candidate.rel_path, "unreadable"))
            continue

        try:
            with os.fdopen(fd, "rb") as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
        except OSError:
            report.dropped.append(DroppedEntry(candidate.rel_path, "unreadable"))
            continue

        os.chmod(target, 0o644)  # modes stripped: collected evidence is never executable
        written = os.path.getsize(target)
        report.artifacts.append(CollectedArtifact(candidate.rel_path, written))
        report.bytes_total += written

    return report
