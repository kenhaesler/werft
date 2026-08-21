"""HTTP routes (SPEC §9 operator surface): runs list, run detail, quota
status, artifact listing, and the six-endpoint mutation set — review
accept/reject, run cancel/requeue, project onboard, and the manual
lifecycle flip (Task 12/B3's CLOSED write set; see `_run_summary_query`'s
callers below for the exact list — artifact *serving* is the one surface
still outstanding).

Two routers, wired separately by the composition root in `app.py`:
`healthz_router` carries no auth dependency (the watchdog must always be
able to reach it, token configured or not); `api_router` is mounted under
`/api/v1` with the bearer-auth dependency attached at `include_router`
time, once `app.py` has read the token file.
"""

import os
import re
import stat
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import quote
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.schemas import (
    ArtifactOut,
    ArtifactsResponse,
    ArtifactSummary,
    FlipRequest,
    OnboardRequest,
    ProjectOut,
    QuotaAccount,
    QuotaResponse,
    RunAttemptOut,
    RunDetail,
    RunEventOut,
    RunsListResponse,
    RunSummary,
)
from werft.db.models import (
    Artifact,
    BacklogItem,
    Project,
    ProviderAccount,
    QuotaLedgerEntry,
    Run,
    RunAttempt,
    RunEvent,
)
from werft.db.transitions import transition_run
from werft.domain.errors import PermanentError
from werft.domain.projects import ProjectLifecycle
from werft.domain.runs import TERMINAL_STATUSES, ParkedReason, RunStatus
from werft.orchestrator.ci_watch import flip_project
from werft.orchestrator.merge_flow import advance_merging
from werft.orchestrator.onboard import duplicate_project_message, onboard_project

logger = structlog.get_logger(__name__)

healthz_router = APIRouter()


@healthz_router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """DB session dependency: opens a session from the `async_sessionmaker`
    the composition root built once, in `app.py`'s lifespan, and stashed on
    `app.state` (SPEC §1 one-engine guarantee — this never builds its own
    engine). Integration tests bypass it entirely via
    `app.dependency_overrides[get_session]`, injecting the `db_session`
    fixture's session directly."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


api_router = APIRouter()


def _pr_url(owner: str, repo: str, pr_number: int | None) -> str | None:
    if pr_number is None:
        return None
    return f"https://github.com/{owner}/{repo}/pull/{pr_number}"


def _latest_outcome_subquery():
    """The `run_attempts.outcome` of the row with the highest `attempt_no`
    for a correlated `Run` (a scalar subquery, not a join — a run can have
    several attempts and both `list_runs` and `get_run` need exactly one
    row per run). Null when the run has no attempts yet, or when its latest
    attempt hasn't reported an outcome."""
    return (
        select(RunAttempt.outcome)
        .where(RunAttempt.run_id == Run.id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
        .correlate(Run)
        .scalar_subquery()
    )


def _run_summary_query():
    """The `RunSummary` column set plus its two joins, unfiltered — shared
    by `list_runs` (paged, optionally filtered) and `_get_run_summary`
    below (one row, by id) so both ever define the shape exactly once."""
    latest_outcome = _latest_outcome_subquery()
    return (
        select(
            Run.id,
            Project.slug.label("project_slug"),
            Run.status,
            BacklogItem.github_issue_number.label("issue_number"),
            BacklogItem.title.label("issue_title"),
            Run.attempt_count,
            Run.max_attempts,
            latest_outcome.label("latest_outcome"),
            Run.parked_reason,
            Run.pr_number,
            Project.github_owner,
            Project.github_repo,
            Run.created_at,
            Run.updated_at,
        )
        .join(Project, Run.project_id == Project.id)
        .join(BacklogItem, Run.backlog_item_id == BacklogItem.id)
    )


def _row_to_run_summary(row) -> RunSummary:
    return RunSummary(
        id=row.id,
        project_slug=row.project_slug,
        status=row.status,
        issue_number=row.issue_number,
        issue_title=row.issue_title,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        latest_outcome=row.latest_outcome,
        parked_reason=row.parked_reason,
        pr_number=row.pr_number,
        pr_url=_pr_url(row.github_owner, row.github_repo, row.pr_number),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@api_router.get("/runs", response_model=RunsListResponse)
async def list_runs(
    status: str | None = None,
    project: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunsListResponse:
    """`GET /api/v1/runs` — `RunSummary` rows ordered `created_at DESC, id
    DESC` (SPEC §9; the `id` tiebreak keeps pagination stable across rows
    that share a `created_at`). `pr_url` is derived from the project's
    `github_owner`/`github_repo` plus `pr_number`, never stored.
    """
    base = _run_summary_query()
    if status is not None:
        base = base.where(Run.status == status)
    if project is not None:
        base = base.where(Project.slug == project)

    count_result = await session.execute(select(func.count()).select_from(base.subquery()))
    total = count_result.scalar_one()

    # `created_at` alone is not a stable sort key: Postgres's `now()` (the
    # column default) is transaction-start time, so two runs inserted in
    # separate statements within the same transaction — or two concurrent
    # transactions — can land on the exact same timestamp. `Run.id` is a
    # uuidv7 (time-ordered), so it breaks ties deterministically without
    # reordering rows that already differ by `created_at`.
    page = base.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit).offset(offset)
    rows = (await session.execute(page)).all()

    runs = [_row_to_run_summary(row) for row in rows]
    return RunsListResponse(runs=runs, total=total)


async def _get_run_or_404(session: AsyncSession, run_id: UUID) -> None:
    """Raises 404 when `run_id` has no `runs` row. Shared by the two
    `/runs/{id}/...` sub-resource routes so a nonexistent run's artifacts
    404 rather than silently answering with an empty list — a request for
    a subresource of nothing is itself an unknown resource, not an empty
    one."""
    exists = (await session.execute(select(Run.id).where(Run.id == run_id))).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="run not found")


@api_router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunDetail:
    """`GET /api/v1/runs/{id}` — `RunSummary`'s fields plus the detail-only
    columns and the three related-row collections (SPEC §9). Returned
    directly, not wrapped in an envelope. A syntactically invalid UUID
    never reaches this function: FastAPI/pydantic validate the `run_id:
    UUID` path param first and answer 422 on its own.
    """
    latest_outcome = _latest_outcome_subquery()
    stmt = (
        select(
            Run.id,
            Project.slug.label("project_slug"),
            Run.status,
            BacklogItem.github_issue_number.label("issue_number"),
            BacklogItem.title.label("issue_title"),
            Run.attempt_count,
            Run.max_attempts,
            latest_outcome.label("latest_outcome"),
            Run.parked_reason,
            Run.pr_number,
            Project.github_owner,
            Project.github_repo,
            Run.created_at,
            Run.updated_at,
            Run.branch_name,
            Run.base_sha,
            Run.merge_commit_sha,
            Run.error_message,
            Run.result,
        )
        .join(Project, Run.project_id == Project.id)
        .join(BacklogItem, Run.backlog_item_id == BacklogItem.id)
        .where(Run.id == run_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")

    # Chronological order: the `id` identity column is a strictly
    # increasing bigint, so it doubles as an insertion-order tiebreak
    # without a second sort key — the same reasoning `list_runs` applies to
    # `Run.id` for `created_at` ties.
    event_rows = (
        await session.execute(
            select(RunEvent.id, RunEvent.event_type, RunEvent.payload, RunEvent.created_at)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.id.asc())
        )
    ).all()
    attempt_rows = (
        await session.execute(
            select(
                RunAttempt.attempt_no,
                RunAttempt.provider,
                RunAttempt.outcome,
                RunAttempt.duration_seconds,
                RunAttempt.started_at,
                RunAttempt.ended_at,
            )
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_no.asc())
        )
    ).all()
    artifact_rows = (
        await session.execute(
            select(Artifact.path, Artifact.bytes, Artifact.collected_at)
            .where(Artifact.run_id == run_id)
            .order_by(Artifact.path.asc())
        )
    ).all()

    return RunDetail(
        id=row.id,
        project_slug=row.project_slug,
        status=row.status,
        issue_number=row.issue_number,
        issue_title=row.issue_title,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        latest_outcome=row.latest_outcome,
        parked_reason=row.parked_reason,
        pr_number=row.pr_number,
        pr_url=_pr_url(row.github_owner, row.github_repo, row.pr_number),
        created_at=row.created_at,
        updated_at=row.updated_at,
        branch_name=row.branch_name,
        base_sha=row.base_sha,
        merge_commit_sha=row.merge_commit_sha,
        error_message=row.error_message,
        result=row.result,
        events=[
            RunEventOut(
                id=e.id, event_type=e.event_type, payload=e.payload, created_at=e.created_at
            )
            for e in event_rows
        ],
        attempts=[
            RunAttemptOut(
                attempt_no=a.attempt_no,
                provider=a.provider,
                outcome=a.outcome,
                duration_seconds=a.duration_seconds,
                started_at=a.started_at,
                ended_at=a.ended_at,
            )
            for a in attempt_rows
        ],
        artifacts=[
            ArtifactSummary(path=art.path, bytes=art.bytes, collected_at=art.collected_at)
            for art in artifact_rows
        ],
    )


@api_router.get("/runs/{run_id}/artifacts", response_model=ArtifactsResponse)
async def list_artifacts(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> ArtifactsResponse:
    """`GET /api/v1/runs/{id}/artifacts` — the full artifact rows (SPEC §8:
    metadata in DB, bytes on disk; this endpoint serves the metadata only —
    file serving is a later task). 404 when the run itself doesn't exist;
    an empty list is a valid answer for a real run with no collected
    artifacts yet.
    """
    await _get_run_or_404(session, run_id)

    rows = (
        await session.execute(
            select(Artifact.path, Artifact.bytes, Artifact.collected_at, Artifact.content_hash)
            .where(Artifact.run_id == run_id)
            .order_by(Artifact.path.asc())
        )
    ).all()
    artifacts = [
        ArtifactOut(
            path=row.path,
            bytes=row.bytes,
            collected_at=row.collected_at,
            content_hash=row.content_hash,
        )
        for row in rows
    ]
    return ArtifactsResponse(artifacts=artifacts)


def _artifact_containment_ok(base_dir: Path, candidate: Path) -> bool:
    """SPEC §8's containment re-check: `candidate`'s fully-resolved real
    path (`os.path.realpath` — every `..`/`.` collapsed, every symlink,
    including in a *parent* directory, followed) must stay under
    `base_dir`'s own real path.

    Deliberately answers only "does this escape the tree", not "open this
    path" — the caller runs its own `os.lstat` on `candidate` *unresolved*
    for that, precisely because resolving it here would erase the one
    distinction that second check exists to make: a symlink whose target
    happens to sit inside `base_dir` passes containment (nothing here
    escapes), but must still 404 as a symlink. Returning the resolved path
    from this function and letting the caller `lstat` *that* would silently
    defeat the symlink check — `os.path.realpath` follows the very
    symlink `os.lstat` is supposed to catch, so by the time `lstat` ran
    it'd be inspecting the (regular-file) target, not the link.

    `Path.__truediv__` (how `candidate` gets built) silently discards the
    left operand when the right one is itself absolute (a POSIX leading
    `/`, or a Windows drive like `C:\\`) — an absolute `rel_path` doesn't
    need special-casing here either: the join still produces *some* path,
    `realpath` still resolves it, and the prefix check below still rejects
    it exactly like a `../` escape would. One check catches both shapes.
    """
    real_base = os.path.realpath(base_dir)
    real_candidate = os.path.realpath(candidate)
    try:
        return os.path.commonpath([real_base, real_candidate]) == real_base
    except ValueError:
        # Windows: commonpath raises when the two paths don't share a
        # drive — an absolute rel_path naming a different drive than
        # artifacts_root is exactly the escape this check exists to catch,
        # just reported as an exception instead of a plain mismatch.
        return False


_HEADER_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f"\\<>]')


def _ascii_fallback_filename(name: str) -> str:
    """The quoted-string `filename` fallback (SPEC §8): strips ASCII
    control characters — CR/LF above all, which would otherwise split the
    header into two — double quotes and backslashes (the quoted-string
    escape character), and `<`/`>` (so a script tag embedded in a
    collected artifact's filename can never round-trip into this header
    intact), then drops any remaining non-ASCII bytes outright — the
    RFC 5987 `filename*` parameter below carries the faithful UTF-8 name
    for clients that understand it.
    """
    stripped = _HEADER_UNSAFE_CHARS.sub("", name)
    ascii_only = stripped.encode("ascii", "ignore").decode("ascii")
    return ascii_only or "artifact"


def _content_disposition_header(filename: str) -> str:
    """SPEC §8's exact artifact response header:
    `attachment; filename="<ascii-sanitized>"; filename*=UTF-8''<urlencoded>`.
    Both parts are derived from the same untrusted, collector-supplied
    filename, so both must independently be safe against header injection —
    `quote(..., safe="")` percent-encodes every byte outside RFC 3986's
    unreserved set, including the quotes/control characters/angle brackets
    the ASCII fallback strips outright.
    """
    ascii_name = _ascii_fallback_filename(filename)
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


@api_router.get("/runs/{run_id}/artifacts/{artifact_path:path}")
async def get_artifact_file(
    run_id: UUID,
    artifact_path: str,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> Response:
    """`GET /api/v1/runs/{id}/artifacts/{path}` — the artifact's raw bytes
    (SPEC §8: "the evidence surface is a stored-XSS surface" — an artifact
    is a file an *attempt* produced, so its name and bytes are as untrusted
    as anything else that attempt touched; this route exists so a browser
    never renders one inline).

    The `artifacts` row is the index, not the filesystem: a `(run_id,
    path)` pair with no matching row 404s before any `os.*` call at all, so
    a path that was never collected — or one a request forged purely to
    probe the filesystem — can't even reach the containment check below. A
    row that *does* exist still gets re-checked against its resolved, real
    path (`os.path.realpath`) staying under this run's own `artifacts/`
    directory: the DB is trusted to say *whether* a path was collected,
    never trusted, on its own, to say *where on disk* it's safe to open (a
    row is only ever written by the collector, but defense in depth costs
    nothing here and the brief calls for it explicitly). Separately, the
    artifact path's own final component is `os.lstat`-ed *unresolved*
    (never `os.stat`, and never the containment check's already-resolved
    path — see `_artifact_containment_ok`'s docstring for why that
    distinction matters): a symlink there 404s regardless of where it
    points, even a target that would itself have passed containment.
    """
    row = (
        await session.execute(
            select(Artifact.path).where(Artifact.run_id == run_id, Artifact.path == artifact_path)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")

    artifacts_root: str = request.app.state.artifacts_root
    base_dir = Path(artifacts_root) / str(run_id) / "artifacts"
    candidate = base_dir / artifact_path
    if not _artifact_containment_ok(base_dir, candidate):
        raise HTTPException(status_code=404, detail="artifact not found")

    # `candidate`, unresolved — never the containment check's realpath'd
    # value, which would have already dereferenced a symlink at this exact
    # spot and made this check unable to see it (see
    # `_artifact_containment_ok`'s docstring).
    try:
        file_stat = os.lstat(candidate)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise HTTPException(status_code=404, detail="artifact not found")

    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    filename = PurePosixPath(artifact_path).name
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _content_disposition_header(filename),
    }
    return Response(content=data, media_type="application/octet-stream", headers=headers)


@api_router.get("/quota", response_model=QuotaResponse)
async def get_quota(
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> QuotaResponse:
    """`GET /api/v1/quota` — one row per `provider_accounts` row (SPEC §7:
    consumed/reserved/ceiling/headroom/exhaustion/last-reading, each its own
    field — the operator is never asked to do arithmetic).

    `consumed_seconds` and `reserved_seconds` are disjoint buckets over the
    *same* `quota_ledger` rows, partitioned on `actual_wallclock_s IS
    [NOT] NULL` — every ledger row is counted in exactly one of the two, so
    `headroom_seconds = ceiling - consumed - reserved` never double-counts
    a row. (An earlier version had `consumed_seconds` fall back to
    `reserved_wallclock_s` via `COALESCE` for still-open rows — that made
    an in-window, unresolved reservation land in *both* sums and subtract
    twice from headroom; review caught it, this is the fix.)

    `consumed_seconds` sums `actual_wallclock_s` for ledger entries that
    have reported one, within the account's own `rolling_window_hours`.
    `reserved_seconds` is a *live-commitment* figure, not a windowed one:
    it sums `reserved_wallclock_s` for every entry that has no
    `actual_wallclock_s` yet, regardless of age — an attempt that started
    outside the rolling window and never resolved is still an outstanding
    claim on capacity. `headroom_seconds` is `ceiling - consumed - reserved`,
    floored at zero rather than surfaced negative. Whether an in-window,
    *still-open* reservation should also nudge `consumed_seconds` (a
    "how much of my window am I about to spend" reading) is a possible
    refinement left to T7, which owns the reservation lifecycle end to
    end; this endpoint only reads what's already in the ledger.
    """
    # `make_interval`'s positional signature is (years, months, weeks, days,
    # hours, mins, secs); zeros fill the leading params so the fifth
    # position lands on `rolling_window_hours`, matching
    # `test_api_runs.py`'s own `make_interval(secs => :offset)` seeding
    # idiom one column over.
    window_floor = func.now() - func.make_interval(0, 0, 0, 0, ProviderAccount.rolling_window_hours)

    consumed_subq = (
        select(func.coalesce(func.sum(QuotaLedgerEntry.actual_wallclock_s), 0))
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.actual_wallclock_s.is_not(None))
        .where(QuotaLedgerEntry.consumed_at > window_floor)
        .correlate(ProviderAccount)
        .scalar_subquery()
    )
    reserved_subq = (
        select(func.coalesce(func.sum(QuotaLedgerEntry.reserved_wallclock_s), 0))
        .where(QuotaLedgerEntry.provider_account_id == ProviderAccount.id)
        .where(QuotaLedgerEntry.actual_wallclock_s.is_(None))
        .correlate(ProviderAccount)
        .scalar_subquery()
    )

    stmt = select(
        ProviderAccount.provider,
        ProviderAccount.label,
        ProviderAccount.ceiling_seconds,
        consumed_subq.label("consumed_seconds"),
        reserved_subq.label("reserved_seconds"),
        ProviderAccount.exhausted_until,
        ProviderAccount.exhausted_source,
        ProviderAccount.last_reading_utilization,
        ProviderAccount.last_reading_source,
        ProviderAccount.last_reading_at,
    ).order_by(ProviderAccount.provider, ProviderAccount.label)

    rows = (await session.execute(stmt)).all()

    accounts = [
        QuotaAccount(
            provider=row.provider,
            label=row.label,
            ceiling_seconds=row.ceiling_seconds,
            consumed_seconds=row.consumed_seconds,
            reserved_seconds=row.reserved_seconds,
            headroom_seconds=max(
                row.ceiling_seconds - row.consumed_seconds - row.reserved_seconds, 0
            ),
            exhausted_until=row.exhausted_until,
            exhausted_source=row.exhausted_source,
            last_reading_utilization=row.last_reading_utilization,
            last_reading_source=row.last_reading_source,
            last_reading_at=row.last_reading_at,
        )
        for row in rows
    ]
    return QuotaResponse(accounts=accounts)


# -- mutations (SPEC §9, the closed write set — Task 12/B3) ------------------
#
# Every run mutation below follows the same shape: fetch-or-404, check the
# precondition *in Python* before ever attempting the CAS, then
# `transition_run`. Checking the precondition first means the only way the
# CAS itself can still lose is a genuine concurrent race (another request
# moved the row between the read and the `UPDATE`) — the DB trigger's own
# "illegal run status transition" exception is never reached either way,
# because a version-matched-but-status-wrong row can't occur (this code path
# is the only writer of `runs.status`, and status/version always move
# together), and a lost race means zero rows match the `WHERE ... AND
# version = ...`, so the trigger never fires at all. That is what keeps a
# wrong-state or raced request a 409, never an unhandled 500 from a raised
# DB exception.


async def _get_run_summary(session: AsyncSession, run_id: UUID) -> RunSummary:
    """Re-reads one run in the exact `RunSummary` shape `list_runs` returns
    (controller ruling: every run mutation answers with the refreshed run,
    unwrapped, same shape as the list) — a plain column `select`, not
    `session.get`, so it always reflects what was just committed rather than
    a possibly-stale identity-mapped `Run` instance."""
    row = (await session.execute(_run_summary_query().where(Run.id == run_id))).one()
    return _row_to_run_summary(row)


def _project_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        slug=project.slug,
        owner=project.github_owner,
        repo=project.github_repo,
        lifecycle=project.lifecycle,
        onboarded_at=project.onboarded_at,
        created_at=project.created_at,
    )


async def _get_run_for_mutation(session: AsyncSession, run_id: UUID) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@api_router.post("/runs/{run_id}/review/accept", response_model=RunSummary)
async def accept_review(
    run_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/review/accept` — CAS `awaiting_review ->
    merging` (SPEC §9), 409 on the wrong current state or a lost CAS race.

    On a won CAS, drives one best-effort inline `advance_merging` tick
    (controller ruling: run mutations are GitHub-less-safe) — skipped
    entirely when `app.state.ops_for` is `None` (no GitHub creds
    configured), and every error out of it is swallowed (logged, never
    raised): the 30 s poller tick is the actual guarantee this accept only
    tries to shortcut, so a failure here must never turn an accepted review
    into a 500.

    That kick runs under `app.state.merging_lock` — the orchestrator's own
    `merging_lock`, published by the composition root (`app.py`). The CAS
    above commits `merging` before the kick starts, so the in-process
    poller's `_advance_all_merging` discovery query can already see this
    row while the kick is still inside `get_pr`/`squash_merge`: without the
    shared lock the same row reaches two concurrent `squash_merge` calls,
    and the loser's 405 (`MergeBlocked`) parks a run whose PR actually
    merged. The lock is held across the commit as well as the GitHub calls,
    exactly as `_advance_all_merging` holds it across `_run_unit`, so the
    row is never visible as `merging` to a rival discovery query after this
    kick has already decided its outcome.

    Winning the lock second is the other half of that contract, and it is
    why the re-read is followed by a status guard rather than used as-is.
    `advance_merging` now guards its own entry on `status == 'merging'`
    (`merge_flow.py`), so this re-read is defence in depth rather than the only
    thing standing between a rival sweep and a clobbered `merge_commit_sha`.
    It stays because it also saves the GitHub round trip the guard would
    otherwise pay for, and because the re-read is what makes the CAS inside
    `advance_merging` see this row's current version at all. A sweep that
    held the lock first may already have landed the merge (`merging -> merged`, with the real
    `merge_commit_sha`): re-driving the decision table on that row reads a
    now-`merged` PR, takes the merged-out-of-band branch, and CASes
    `merged -> merged` with `merge_commit_sha=None` — which *wins*, because
    the transition trigger only checks legality when the status actually
    changes, so a legitimately merged run would silently lose its merge
    commit sha. Where the sweep instead moved the row to `awaiting_ci` (the
    oracle-gated sha-mismatch path), re-driving would attempt a merge on a
    run that has yet to re-earn green.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status != RunStatus.AWAITING_REVIEW.value:
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not awaiting_review")

    ok = await transition_run(
        session, run_id=run_id, expected_version=run.version, new_status=RunStatus.MERGING
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    ops_for = request.app.state.ops_for
    if ops_for is not None:
        try:
            async with request.app.state.merging_lock:
                # Re-read inside the lock: a poller sweep that won the lock
                # first may already have advanced this run, and
                # `advance_merging`'s CAS is checked against this row's
                # version.
                run = await session.get(Run, run_id, populate_existing=True)
                if run is not None and run.status == RunStatus.MERGING.value:
                    project = await session.get(Project, run.project_id)
                    await advance_merging(
                        session, ops_for(project), run, project, alerts=request.app.state.alerts
                    )
                    await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("api.accept_advance_merging_failed", run_id=str(run_id), exc_info=True)

    return await _get_run_summary(session, run_id)


@api_router.post("/runs/{run_id}/review/reject", response_model=RunSummary)
async def reject_review(
    run_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/review/reject` — CAS `awaiting_review ->
    parked` with `parked_reason='review_rejected'` (SPEC §9), then fires
    `alerts.run_parked` — only after the CAS has won, the same "alert after
    the write, never before" discipline `merge_flow._park` and
    `ci_watch.advance_awaiting_ci` use.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status != RunStatus.AWAITING_REVIEW.value:
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not awaiting_review")

    ok = await transition_run(
        session,
        run_id=run_id,
        expected_version=run.version,
        new_status=RunStatus.PARKED,
        extra={"parked_reason": ParkedReason.REVIEW_REJECTED.value},
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    project = await session.get(Project, run.project_id)
    await request.app.state.alerts.run_parked(
        project.slug, run_id, ParkedReason.REVIEW_REJECTED.value
    )

    return await _get_run_summary(session, run_id)


@api_router.post("/runs/{run_id}/cancel", response_model=RunSummary)
async def cancel_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/cancel` — CAS `<non-terminal> -> canceled`
    (SPEC §9; SPEC §3.2's canceled edges are every non-terminal status), 409
    if the run is already terminal. Cleanup (closing any open PR, deleting
    the run branch) happens out-of-band via the tick sweep's
    `cleanup_terminal` — this endpoint only flips the status.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"run is already {run.status}")

    ok = await transition_run(
        session, run_id=run_id, expected_version=run.version, new_status=RunStatus.CANCELED
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    return await _get_run_summary(session, run_id)


@api_router.post("/runs/{run_id}/requeue", response_model=RunSummary)
async def requeue_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> RunSummary:
    """`POST /api/v1/runs/{id}/requeue` — CAS `parked -> queued`, resetting
    `attempt_count` to 0 and `next_attempt_at` to now (plan Behavioral
    decision 7: a human explicitly granting a fresh retry budget — not a
    resume of the one that was already spent), and clearing `parked_reason`.
    409 from any state but `parked`.

    `parked_reason` is cleared here because this endpoint is the only exit
    from `parked` back into the working states, and nothing downstream ever
    nulls the column: every other writer sets it, at a park. Left behind, it
    is reported by `/runs` and `/runs/{id}` — and rendered by the dashboard's
    own "Parked reason" column — for a run that is queued, running, or even
    merged, until some later park happens to overwrite it. `error_message`
    is deliberately *not* cleared: it is the run's rolling last-error field,
    written on every failure path and left in place across the `failed ->
    queued` retry too, so nulling it only here would make the two retry
    paths disagree about what a requeued run remembers.
    """
    run = await _get_run_for_mutation(session, run_id)
    if run.status != RunStatus.PARKED.value:
        raise HTTPException(status_code=409, detail=f"run is {run.status}, not parked")

    ok = await transition_run(
        session,
        run_id=run_id,
        expected_version=run.version,
        new_status=RunStatus.QUEUED,
        extra={"attempt_count": 0, "next_attempt_at": func.now(), "parked_reason": None},
    )
    if not ok:
        raise HTTPException(status_code=409, detail="run state changed concurrently")
    await session.commit()

    return await _get_run_summary(session, run_id)


@api_router.post("/projects/onboard", response_model=ProjectOut, status_code=201)
async def onboard(
    body: OnboardRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> ProjectOut:
    """`POST /api/v1/projects/onboard` — SPEC §6.3's one-time bootstrap
    setup, driven through `orchestrator/onboard.py`'s `onboard_project`.
    Requires GitHub creds (controller ruling: unlike the run mutations,
    onboard cannot proceed without them) — 503 when `app.state.ops_for`/
    `admin_ops_for` are unset. `onboard_project`'s `PermanentError` maps to
    409 for a duplicate slug/repo, 422 for an unreachable installation
    (main branch/repo not found).

    A *concurrent* duplicate answers 409 too, by the other route: two
    onboards of the same slug/repo both pass `onboard_project`'s duplicate
    `SELECT` — taken before its GitHub round trips — and the loser's INSERT
    violates `projects`' unique constraints instead. That `IntegrityError`
    is caught here and answered with the same 409 body as the sequential
    duplicate, rather than escaping as an unhandled 500 (nothing in this app
    registers an exception handler). The losing transaction rolls back, so
    nothing partial survives, and every GitHub call it already made is
    idempotent — the winner's project is unaffected.

    No `Project` row exists yet for `app.state.ops_for`'s per-project cache
    key to key off of — a throwaway `SimpleNamespace` carrying just the
    owner/repo the operator supplied plus a fresh id stands in; the
    factories only ever read those three attributes off whatever they're
    given (see `app.py::_ops_factory`/`_admin_ops_factory`).
    """
    ops_for = request.app.state.ops_for
    admin_ops_for = request.app.state.admin_ops_for
    if ops_for is None or admin_ops_for is None:
        raise HTTPException(status_code=503, detail="GitHub App not configured")

    target = SimpleNamespace(id=uuid4(), github_owner=body.owner, github_repo=body.repo)
    try:
        project = await onboard_project(
            session,
            ops_for(target),
            admin_ops_for(target),
            slug=body.slug,
            owner=body.owner,
            repo=body.repo,
        )
    except PermanentError as exc:
        await session.rollback()
        message = str(exc)
        status_code = 409 if "already onboarded" in message else 422
        raise HTTPException(status_code=status_code, detail=message) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=duplicate_project_message(body.slug, body.owner, body.repo)
        ) from exc

    await session.commit()
    return _project_out(project)


@api_router.post("/projects/{project_id}/flip", response_model=ProjectOut)
async def flip(
    project_id: UUID,
    body: FlipRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008 - FastAPI's DI pattern
) -> ProjectOut:
    """`POST /api/v1/projects/{id}/flip` — the manual repair flip (SPEC
    §3.1), either direction, delegating to `ci_watch.flip_project` (the same
    idempotent guard the automatic doctrine-#1 flip uses). Requires GitHub
    creds (503 when unconfigured, same controller ruling as onboard); a
    no-op guard miss (project already at the requested `to` lifecycle) is
    409, never a silent 200.
    """
    admin_ops_for = request.app.state.admin_ops_for
    if admin_ops_for is None:
        raise HTTPException(status_code=503, detail="GitHub App not configured")

    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    to = ProjectLifecycle(body.to)
    flipped = await flip_project(
        session, admin_ops_for(project), project, to=to, alerts=request.app.state.alerts
    )
    if not flipped:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"project already {to.value}")
    await session.commit()

    project = await session.get(Project, project_id, populate_existing=True)
    return _project_out(project)
