"""Host-side reader for agent-writable output trees (SPEC §4.3/§8, [CD M8]).

The manager holds every secret; the tree it is reading was writable by a root
agent. So this module never traverses a non-regular file, never follows a link,
and enforces the size cap as a running total *during* the walk — which is what
turns "symlink a directory to /" from a disk-fill into a bounded no-op.

Explicitly prohibited, because they are the shortcut reached for under pressure:
**never `cp -r`, never `rsync -a`, never `tar -C`** — every one of them follows
links or preserves modes.

SPEC §8: over-cap drops largest-first and what was dropped is recorded as an
event — "evidence that silently misses is worse than evidence that says
'truncated here'".
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
    reason: str  # 'not_regular' | 'too_large' | 'total_cap' | 'outside_root'
    size_bytes: int = 0


@dataclass
class CollectionReport:
    artifacts: list[CollectedArtifact] = field(default_factory=list)
    dropped: list[DroppedEntry] = field(default_factory=list)
    bytes_total: int = 0
    truncated: bool = False


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

    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        # Never descend a symlinked directory: os.walk lists links to directories
        # in dirnames, and followlinks=False only stops recursion into them if we
        # do not re-add them — prune explicitly so the intent is on the page.
        kept_dirs = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if stat.S_ISDIR(os.lstat(full).st_mode):
                kept_dirs.append(name)
            else:
                report.dropped.append(DroppedEntry(os.path.relpath(full, src_root), "not_regular"))
        dirnames[:] = kept_dirs

        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src_root)
            info = os.lstat(full)  # lstat: never resolves the link

            if not stat.S_ISREG(info.st_mode):
                report.dropped.append(DroppedEntry(rel, "not_regular"))
                continue
            if info.st_size > max_file_bytes:
                report.dropped.append(DroppedEntry(rel, "too_large", info.st_size))
                report.truncated = True
                continue
            if report.bytes_total + info.st_size > max_total_bytes:
                report.dropped.append(DroppedEntry(rel, "total_cap", info.st_size))
                report.truncated = True
                continue

            target = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                report.dropped.append(DroppedEntry(rel, "not_regular"))
                continue
            try:
                with os.fdopen(fd, "rb") as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink)
            finally:
                pass
            os.chmod(target, 0o644)  # modes stripped
            written = os.path.getsize(target)
            report.artifacts.append(CollectedArtifact(rel, written))
            report.bytes_total += written

    return report
