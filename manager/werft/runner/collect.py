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

import contextlib
import os
import shutil
import stat
from dataclasses import dataclass, field

MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024

#: Every directory Werft creates under `artifacts_root` is as private as
#: `workspace.create_run_dirs` makes the run dir — collected evidence is
#: served only through the authenticated API, never off the filesystem.
DIR_MODE = 0o700


def _harden_dir(path: str) -> None:
    """Mirror `workspace._harden`. POSIX-only: Windows ignores these bits and
    the dev box is not the deployment target. `makedirs(mode=...)` alone is not
    enough — it is masked by the umask and, crucially, CPython recurses for the
    intermediate levels *without* passing the mode at all (only the leaf gets
    it), so those come out `0o777 & ~umask` — 0755 on the rig."""
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        os.chmod(path, DIR_MODE)


def _harden_parents(dest_dir: str, rel_path: str) -> None:
    """Harden every directory level `rel_path` needs beneath `dest_dir`.

    A single `chmod` on the deepest parent is not enough: for
    `outputs/nested/x.txt`, `os.makedirs` creates `dest/outputs` as an
    *intermediate* level at 0755 and only `dest/outputs/nested` at 0700 — and
    one world-traversable step is all it takes to reach the evidence below it.
    Walking the parts is O(depth) per file against trees that are a handful of
    levels deep, which is far cheaper than the copy it accompanies.

    The Windows no-op lives in `_harden_dir`, the one place that knows about
    modes, so the level walk itself stays testable on the dev box.
    """
    parts = rel_path.replace("/", os.sep).split(os.sep)[:-1]  # drop the filename
    current = dest_dir
    for part in parts:
        current = os.path.join(current, part)
        _harden_dir(current)


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
class TreeSource:
    src_dir: str
    dest_prefix: str  # non-empty, no slashes at the edges, e.g. "outputs"


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


def _copy_candidates(
    candidates: list[_Candidate],
    dest_dir: str,
    report: CollectionReport,
    max_total_bytes: int,
    *,
    existing: dict[str, int] | None = None,
) -> None:
    """Copy pre-sorted candidates into `dest_dir`, enforcing the total-bytes cap.

    `candidates` must already be sorted smallest-first (see callers): copying
    smallest-first and dropping whatever no longer fits is what makes SPEC §8's
    "over-cap drops largest-first" true, and it is deterministic — the agent
    cannot influence which evidence survives by choosing filenames.

    `existing` (D4) credits bytes a caller already has on disk at a `rel_path`
    (e.g. a prior collection pass) so a same-content retry is never dropped as
    `total_cap` for bytes that were already counted. `report.bytes_total` only
    ever accumulates bytes written by *this* pass.
    """
    working_existing = dict(existing) if existing else {}
    effective_total = sum(working_existing.values())

    for candidate in candidates:
        prospective = (
            effective_total - working_existing.get(candidate.rel_path, 0) + candidate.size_bytes
        )
        if prospective > max_total_bytes:
            report.dropped.append(
                DroppedEntry(candidate.rel_path, "total_cap", candidate.size_bytes)
            )
            report.truncated = True
            continue

        target = os.path.join(dest_dir, candidate.rel_path)
        os.makedirs(os.path.dirname(target), mode=DIR_MODE, exist_ok=True)
        _harden_parents(dest_dir, candidate.rel_path)
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
        effective_total = prospective
        working_existing.pop(candidate.rel_path, None)


def collect_outputs(
    src_dir: str,
    dest_dir: str,
    *,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> CollectionReport:
    """Copy regular files from an agent-writable tree into Werft-owned storage.

    `src_dir` is resolved with `realpath` here, which is only safe because this
    entry point's root is **manager-created** (`lifecycle.py` makes the run's
    `outputs/` before the container ever starts) and therefore cannot itself be
    a link the agent planted. `collect_trees` — whose roots are agent-created
    directories inside the rw workspace — deliberately does *not* resolve, and
    lstat-checks each root instead.
    """
    report = CollectionReport()
    src_root = os.path.realpath(src_dir)
    os.makedirs(dest_dir, mode=DIR_MODE, exist_ok=True)
    _harden_dir(dest_dir)

    candidates = _scan(src_root, report, max_file_bytes)

    # SPEC §8: "over-cap drops largest-first". See `_copy_candidates`.
    candidates.sort(key=lambda c: (c.size_bytes, c.rel_path))

    _copy_candidates(candidates, dest_dir, report, max_total_bytes)

    return report


def collect_trees(
    sources: list[TreeSource],
    dest_dir: str,
    *,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
    existing: dict[str, int] | None = None,
) -> CollectionReport:
    """Collect several agent-writable trees into one destination under one budget.

    Each source's files land under `f"{dest_prefix}/{rel}"` (forward slashes,
    always — this keeps `Artifact.path` portable between a Windows dev box and
    the Linux rig, and matches the serving route's `{artifact_path:path}`
    semantics). A missing `src_dir` is silent: an `OSError` from the root's
    `lstat` is skipped, so there is nothing to prefix or drop.

    **The source roots are never resolved.** Three of them are agent-created
    directories inside the rw workspace, so `ln -s ../secrets .werft-artifacts`
    (or an absolute link to any host path) would redirect the whole walk into
    the manager's own secrets — and collection runs *before* `remove_secrets`,
    with the sweep path having possibly never revoked at all. Each root is
    therefore `lstat`ed (which does not resolve the final component) and
    dropped as `not_regular` unless it is a real directory.

    All candidates from all sources are pooled and sorted once
    (`key=(size_bytes, rel_path)`) before the single copy pass, so the largest
    file across the *whole* evidence set — not just its own source — is what
    gets evicted first when the shared budget runs out.
    """
    report = CollectionReport()
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    _harden_dir(dest_dir)

    all_candidates: list[_Candidate] = []
    for source in sources:
        src_root = source.src_dir  # never resolved: see the docstring
        try:
            root_info = os.lstat(src_root)
        except OSError:
            continue  # a missing source stays a silent no-op
        if not stat.S_ISDIR(root_info.st_mode):
            # lstat does not resolve, so a symlinked root fails this check even
            # when it points at a directory: the walk never starts.
            report.dropped.append(DroppedEntry(source.dest_prefix, "not_regular"))
            continue

        dropped_before = len(report.dropped)
        candidates = _scan(src_root, report, max_file_bytes)

        for i in range(dropped_before, len(report.dropped)):
            dropped = report.dropped[i]
            prefixed = f"{source.dest_prefix}/{dropped.rel_path.replace(os.sep, '/')}"
            report.dropped[i] = DroppedEntry(prefixed, dropped.reason, dropped.size_bytes)

        for candidate in candidates:
            prefixed_rel = f"{source.dest_prefix}/{candidate.rel_path.replace(os.sep, '/')}"
            all_candidates.append(
                _Candidate(prefixed_rel, candidate.full_path, candidate.size_bytes)
            )

    all_candidates.sort(key=lambda c: (c.size_bytes, c.rel_path))

    _copy_candidates(all_candidates, dest_dir, report, max_total_bytes, existing=existing)

    return report
