"""`/api/v1/runs` against a real DB (SPEC §9 operator surface, thin-loop
minimum: runs list is the first vertical slice) plus `api/auth.py`'s
bearer dependency wired end-to-end through `create_app` — not just the
isolated dependency `test_api_auth.py` covers.

Seeding follows `test_backlog.py`/`test_finalize.py`'s style: raw SQL
inserts, then real ORM instances read back through the session. `created_at`
is set explicitly via `now() - make_interval(...)` (as
`test_finalize.py::seed_open_attempt` does) because Postgres's `now()` is
transaction-start time, not wall-clock-per-statement — two seed calls in
separate transactions are not reliably ordered by real elapsed time alone
at test speed.

`get_session` is always overridden via `app.dependency_overrides`, so none
of these tests ever enters `create_app`'s lifespan — the DB session comes
straight from the `db_session` fixture, exactly as the task brief
prescribes ("use httpx.AsyncClient... with dependency_overrides injecting
the test session").
"""

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.routes import get_session
from werft.app import create_app
from werft.config.settings import Settings
from werft.db.models import Artifact, BacklogItem, Project, ProviderAccount, Run
from werft.db.transitions import transition_run
from werft.domain.runs import RunStatus

# -- seeding ------------------------------------------------------------


async def seed_project(session, *, owner: str = "acme", repo: str | None = None) -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo) "
                "VALUES (:slug, :owner, :repo) RETURNING id"
            ),
            {"slug": f"p-{tag}", "owner": owner, "repo": repo or f"r-{tag}"},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Project, pid)


async def seed_backlog_item(
    session, project: Project, number: int, *, title: str = "an issue"
) -> BacklogItem:
    bid = (
        await session.execute(
            text(
                "INSERT INTO backlog_items "
                "(project_id, github_issue_number, title, github_updated_at) "
                "VALUES (:p, :n, :t, now()) RETURNING id"
            ),
            {"p": project.id, "n": number, "t": title},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(BacklogItem, bid)


async def seed_run(
    session,
    project: Project,
    item: BacklogItem,
    *,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    pr_number: int | None = None,
    parked_reason: str | None = None,
    created_offset_seconds: int = 0,
) -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, attempt_count, "
                "max_attempts, pr_number, parked_reason, created_at) "
                "VALUES (:p, :b, :s, :ac, :ma, :pr, :reason, "
                "now() - make_interval(secs => :offset)) RETURNING id"
            ),
            {
                "p": project.id,
                "b": item.id,
                "s": status,
                "ac": attempt_count,
                "ma": max_attempts,
                "pr": pr_number,
                "reason": parked_reason,
                "offset": created_offset_seconds,
            },
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Run, rid)


async def seed_attempt(
    session,
    run: Run,
    *,
    attempt_no: int = 1,
    outcome: str | None = None,
    provider: str = "claude",
    duration_seconds: int | None = None,
    ended: bool = False,
) -> None:
    await session.execute(
        text(
            "INSERT INTO run_attempts "
            "(run_id, attempt_no, provider, outcome, duration_seconds, started_at, ended_at) "
            "VALUES (:r, :n, :prov, :o, :dur, now(), CASE WHEN :ended THEN now() ELSE NULL END)"
        ),
        {
            "r": run.id,
            "n": attempt_no,
            "prov": provider,
            "o": outcome,
            "dur": duration_seconds,
            "ended": ended,
        },
    )
    await session.commit()


async def update_run_detail_fields(
    session,
    run: Run,
    *,
    branch_name: str | None = None,
    base_sha: str | None = None,
    merge_commit_sha: str | None = None,
    error_message: str | None = None,
    result: dict | None = None,
) -> None:
    """Sets the detail-only columns `RunDetail` surfaces beyond `RunSummary`.
    A plain `UPDATE` — the transition trigger only enforces legality and
    writes a `run_events` row when `status` itself changes (`runs.py`'s
    `runs_enforce_transition`), so this never needs a legal status edge."""
    await session.execute(
        text(
            "UPDATE runs SET branch_name = :branch, base_sha = :base, "
            "merge_commit_sha = :merge_sha, error_message = :err, "
            "result = CAST(:result AS jsonb) WHERE id = :id"
        ),
        {
            "branch": branch_name,
            "base": base_sha,
            "merge_sha": merge_commit_sha,
            "err": error_message,
            "result": json.dumps(result) if result is not None else None,
            "id": run.id,
        },
    )
    await session.commit()


async def seed_artifact(
    session,
    run: Run,
    *,
    path: str = "log.jsonl",
    size: int = 1024,
    content_hash: str | None = "sha256:deadbeef",
) -> Artifact:
    aid = (
        await session.execute(
            text(
                "INSERT INTO artifacts (run_id, path, bytes, content_hash) "
                "VALUES (:r, :p, :b, :h) RETURNING id"
            ),
            {"r": run.id, "p": path, "b": size, "h": content_hash},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Artifact, aid)


async def seed_provider_account(
    session,
    *,
    provider: str = "claude",
    label: str = "primary",
    rolling_window_hours: int = 5,
    ceiling_seconds: int = 18000,
    exhausted_until: str | None = None,
    exhausted_source: str | None = None,
    last_reading_utilization: float | None = None,
    last_reading_source: str | None = None,
    last_reading_at_offset_seconds: int | None = None,
) -> ProviderAccount:
    pid = (
        await session.execute(
            text(
                "INSERT INTO provider_accounts "
                "(provider, label, rolling_window_hours, ceiling_seconds, "
                "exhausted_until, exhausted_source, last_reading_utilization, "
                "last_reading_source, last_reading_at) "
                "VALUES (:prov, :label, :rwh, :ceiling, :exh_until, :exh_source, "
                ":lr_util, :lr_source, "
                "CASE WHEN CAST(:lr_offset AS double precision) IS NULL THEN NULL "
                "ELSE now() - make_interval(secs => CAST(:lr_offset AS double precision)) END) "
                "RETURNING id"
            ),
            {
                "prov": provider,
                "label": label,
                "rwh": rolling_window_hours,
                "ceiling": ceiling_seconds,
                "exh_until": exhausted_until,
                "exh_source": exhausted_source,
                "lr_util": last_reading_utilization,
                "lr_source": last_reading_source,
                "lr_offset": last_reading_at_offset_seconds,
            },
        )
    ).scalar_one()
    await session.commit()
    return await session.get(ProviderAccount, pid)


async def seed_quota_ledger_entry(
    session,
    account: ProviderAccount,
    run: Run,
    *,
    attempt_no: int = 1,
    reserved_wallclock_s: int = 100,
    actual_wallclock_s: int | None = None,
    consumed_offset_seconds: int = 0,
) -> None:
    await session.execute(
        text(
            "INSERT INTO quota_ledger "
            "(provider_account_id, run_id, attempt_no, reserved_wallclock_s, "
            "actual_wallclock_s, consumed_at) "
            "VALUES (:acct, :run, :n, :reserved, :actual, "
            "now() - make_interval(secs => :offset))"
        ),
        {
            "acct": account.id,
            "run": run.id,
            "n": attempt_no,
            "reserved": reserved_wallclock_s,
            "actual": actual_wallclock_s,
            "offset": consumed_offset_seconds,
        },
    )
    await session.commit()


# -- app wiring -----------------------------------------------------------

TOKEN = "s3cr3t-token"


def make_client_app(db_session: AsyncSession, *, token_file: str = ""):
    app = create_app(Settings(api_token_file=token_file))

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    return app


@pytest.fixture
def token_file(tmp_path) -> str:
    path = tmp_path / "api-token"
    path.write_text(TOKEN)
    return str(path)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def test_list_projects_includes_repositories_without_runs(
    db_session, token_file, auth_headers
) -> None:
    first = await seed_project(db_session, owner="operator", repo="alpha")
    second = await seed_project(db_session, owner="operator", repo="beta")
    app = make_client_app(db_session, token_file=token_file)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/api/v1/projects")
        response = await client.get("/api/v1/projects", headers=auth_headers)
    assert unauthorized.status_code == 401
    assert response.status_code == 200
    projects = response.json()
    assert [p["slug"] for p in projects] == sorted([first.slug, second.slug])
    assert {p["repo"] for p in projects} == {"alpha", "beta"}
    assert all(p["owner"] == "operator" for p in projects)
    assert all(p["lifecycle"] == "bootstrap" for p in projects)


# -- shape, ordering, latest_outcome, pr_url ------------------------------


async def test_list_runs_shape_ordering_outcome_and_pr_url(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session, owner="acme", repo="widgets")

    item1 = await seed_backlog_item(db_session, project, 42, title="fix the thing")
    older_run = await seed_run(
        db_session,
        project,
        item1,
        status="running",
        attempt_count=1,
        created_offset_seconds=120,
    )
    await seed_attempt(db_session, older_run, attempt_no=1, outcome=None)

    item2 = await seed_backlog_item(db_session, project, 43, title="second issue")
    newer_run = await seed_run(
        db_session,
        project,
        item2,
        status="awaiting_review",
        attempt_count=2,
        pr_number=7,
        created_offset_seconds=0,
    )
    await seed_attempt(db_session, newer_run, attempt_no=1, outcome="ci_red")
    await seed_attempt(db_session, newer_run, attempt_no=2, outcome="ci_green")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2

    runs = body["runs"]
    assert [r["id"] for r in runs] == [str(newer_run.id), str(older_run.id)]  # created_at DESC

    expected_fields = {
        "id",
        "project_slug",
        "status",
        "issue_number",
        "issue_title",
        "attempt_count",
        "max_attempts",
        "latest_outcome",
        "parked_reason",
        "pr_number",
        "pr_url",
        "created_at",
        "updated_at",
    }
    assert set(runs[0].keys()) == expected_fields

    newest = runs[0]
    assert newest["project_slug"] == project.slug
    assert newest["status"] == "awaiting_review"
    assert newest["issue_number"] == 43
    assert newest["issue_title"] == "second issue"
    assert newest["attempt_count"] == 2
    assert newest["max_attempts"] == 3
    assert newest["latest_outcome"] == "ci_green"  # from the highest attempt_no row
    assert newest["parked_reason"] is None
    assert newest["pr_number"] == 7
    assert newest["pr_url"] == "https://github.com/acme/widgets/pull/7"

    oldest = runs[1]
    assert oldest["issue_number"] == 42
    assert oldest["latest_outcome"] is None
    assert oldest["pr_number"] is None
    assert oldest["pr_url"] is None


async def test_list_runs_latest_outcome_is_null_when_run_has_no_attempts(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    await seed_run(db_session, project, item, status="queued")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", headers=auth_headers)

    body = resp.json()
    assert body["total"] == 1
    assert body["runs"][0]["latest_outcome"] is None


async def test_list_runs_parked_reason_surfaces(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    # "permanent_error" is one of runs.parked_reason's CHECK-constrained
    # values (migration 0001_spine.py) — not free text.
    await seed_run(db_session, project, item, status="parked", parked_reason="permanent_error")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", headers=auth_headers)

    assert resp.json()["runs"][0]["parked_reason"] == "permanent_error"


# -- filters ----------------------------------------------------------------


async def test_status_filter(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item1 = await seed_backlog_item(db_session, project, 1)
    item2 = await seed_backlog_item(db_session, project, 2)
    await seed_run(db_session, project, item1, status="running", created_offset_seconds=10)
    await seed_run(db_session, project, item2, status="merged", created_offset_seconds=0)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", params={"status": "running"}, headers=auth_headers)

    body = resp.json()
    assert body["total"] == 1
    assert body["runs"][0]["status"] == "running"


async def test_project_filter(db_session, token_file, auth_headers) -> None:
    project_a = await seed_project(db_session)
    project_b = await seed_project(db_session)
    item_a = await seed_backlog_item(db_session, project_a, 1)
    item_b = await seed_backlog_item(db_session, project_b, 1)
    await seed_run(db_session, project_a, item_a, created_offset_seconds=10)
    await seed_run(db_session, project_b, item_b, created_offset_seconds=0)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/runs", params={"project": project_a.slug}, headers=auth_headers
        )

    body = resp.json()
    assert body["total"] == 1
    assert body["runs"][0]["project_slug"] == project_a.slug


async def test_limit_offset_and_total_count_all_matches(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    items = [await seed_backlog_item(db_session, project, n) for n in range(1, 4)]
    runs = [
        await seed_run(db_session, project, items[i], created_offset_seconds=offset)
        for i, offset in enumerate([20, 10, 0])
    ]
    # created_offset_seconds=20 is the oldest -> DESC order is [runs[2], runs[1], runs[0]]
    desc_order = [runs[2], runs[1], runs[0]]

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/runs", params={"limit": 1, "offset": 1}, headers=auth_headers
        )

    body = resp.json()
    assert body["total"] == 3  # total ignores limit/offset
    assert len(body["runs"]) == 1
    assert body["runs"][0]["id"] == str(desc_order[1].id)


async def test_pagination_is_stable_across_tied_created_at(
    db_session, token_file, auth_headers
) -> None:
    """`created_at` alone is not a stable sort key: Postgres's `now()` (the
    column default) is transaction-start time, so two rows inserted by the
    same statement share the exact same `created_at`. `Run.id` (uuidv7,
    time-ordered) breaks that tie deterministically; without it, paging one
    row at a time (limit=1) over tied rows can show the same row on two
    pages, or skip one entirely, depending on how Postgres happens to order
    equal timestamps on a given execution.

    Both runs are inserted by one `INSERT ... SELECT` statement sharing a
    single `now()` call, guaranteeing identical `created_at` values rather
    than merely hoping two separate statements land on the same timestamp.
    """
    project = await seed_project(db_session)
    item1 = await seed_backlog_item(db_session, project, 1)
    item2 = await seed_backlog_item(db_session, project, 2)

    inserted_ids = (
        (
            await db_session.execute(
                text(
                    "WITH ts AS (SELECT now() AS created_at) "
                    "INSERT INTO runs "
                    "(project_id, backlog_item_id, status, attempt_count, "
                    "max_attempts, created_at) "
                    "SELECT :p, b, 'queued', 0, 3, ts.created_at "
                    "FROM (VALUES (CAST(:b1 AS uuid)), (CAST(:b2 AS uuid))) AS v(b), ts "
                    "RETURNING id"
                ),
                {"p": project.id, "b1": item1.id, "b2": item2.id},
            )
        )
        .scalars()
        .all()
    )
    await db_session.commit()
    assert len(inserted_ids) == 2
    expected_ids = {str(i) for i in inserted_ids}

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page0 = await client.get(
            "/api/v1/runs", params={"limit": 1, "offset": 0}, headers=auth_headers
        )
        page1 = await client.get(
            "/api/v1/runs", params={"limit": 1, "offset": 1}, headers=auth_headers
        )

    ids0 = {r["id"] for r in page0.json()["runs"]}
    ids1 = {r["id"] for r in page1.json()["runs"]}
    assert len(ids0) == 1
    assert len(ids1) == 1
    assert ids0.isdisjoint(ids1)  # no row repeated across pages
    assert ids0 | ids1 == expected_ids  # no row skipped


# -- pagination bounds --------------------------------------------------------


async def test_negative_limit_is_422(db_session, token_file, auth_headers) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", params={"limit": -1}, headers=auth_headers)
    assert resp.status_code == 422


async def test_limit_above_cap_is_422(db_session, token_file, auth_headers) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", params={"limit": 201}, headers=auth_headers)
    assert resp.status_code == 422


async def test_negative_offset_is_422(db_session, token_file, auth_headers) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", params={"offset": -1}, headers=auth_headers)
    assert resp.status_code == 422


# -- auth wiring end-to-end --------------------------------------------------


async def test_runs_endpoint_401_without_token(db_session, token_file) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs")
    assert resp.status_code == 401


async def test_runs_endpoint_401_with_wrong_token(db_session, token_file) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_runs_endpoint_403_when_token_unconfigured(db_session, auth_headers) -> None:
    app = make_client_app(db_session, token_file="")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs", headers=auth_headers)
    assert resp.status_code == 403


async def test_healthz_stays_open_even_when_token_unconfigured(db_session) -> None:
    app = make_client_app(db_session, token_file="")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# -- lifespan -> route wiring seam, no overrides -----------------------------


async def test_runs_endpoint_through_the_real_lifespan_with_no_overrides(
    migrated_db: str, tmp_path
) -> None:
    """Every other test in this file overrides `get_session` with the
    `db_session` fixture's session directly, and `test_app.py`'s own
    lifespan tests only ever reach `/healthz`. `get_session` (routes.py)
    reads `request.app.state.session_factory`, populated unconditionally by
    the lifespan (app.py) — a typo'd attribute name on either side of that
    seam would leave every other test in the suite green while 500ing in
    production, because nothing else ever exercises the real path between
    them.

    This test enters the real `lifespan_context` (no `dependency_overrides`
    at all) against the real migrated test Postgres and drives one request
    for `/api/v1/runs` through the complete `create_app` wiring, auth
    included.
    """
    token_path = tmp_path / "api-token"
    token_path.write_text(TOKEN)
    settings = Settings(database_url=migrated_db, api_token_file=str(token_path))
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/runs", headers={"Authorization": f"Bearer {TOKEN}"})

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["runs"], list)
    assert isinstance(body["total"], int)


# -- run detail (Task 11 / B2) -----------------------------------------------


async def test_run_detail_shape_and_fields(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session, owner="acme", repo="widgets")
    item = await seed_backlog_item(db_session, project, 42, title="fix the thing")
    run = await seed_run(db_session, project, item, status="queued", pr_number=7)
    await update_run_detail_fields(
        db_session,
        run,
        branch_name="werft/run-abc",
        base_sha="deadbeef",
        merge_commit_sha="c0ffee",
        error_message="boom",
        result={"summary": "ok", "files_changed": 3},
    )
    # Drive one legal transition so a `status_changed` run_events row exists
    # in addition to the `created` row the AFTER INSERT trigger always
    # writes (SPEC §3.2's trigger, not application code).
    moved = await transition_run(
        db_session, run_id=run.id, expected_version=0, new_status=RunStatus.CLAIMED
    )
    assert moved

    await seed_attempt(
        db_session, run, attempt_no=1, outcome="ci_red", duration_seconds=90, ended=True
    )
    await seed_attempt(
        db_session, run, attempt_no=2, outcome="ci_green", duration_seconds=120, ended=True
    )
    await seed_artifact(db_session, run, path="log.jsonl", size=2048, content_hash="sha256:abc")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()

    expected_fields = {
        "id",
        "project_slug",
        "status",
        "issue_number",
        "issue_title",
        "attempt_count",
        "max_attempts",
        "latest_outcome",
        "parked_reason",
        "pr_number",
        "pr_url",
        "created_at",
        "updated_at",
        "branch_name",
        "base_sha",
        "merge_commit_sha",
        "error_message",
        "result",
        "events",
        "attempts",
        "artifacts",
    }
    assert set(body.keys()) == expected_fields

    assert body["id"] == str(run.id)
    assert body["project_slug"] == project.slug
    assert body["status"] == "claimed"
    assert body["issue_number"] == 42
    assert body["issue_title"] == "fix the thing"
    assert body["pr_number"] == 7
    assert body["pr_url"] == "https://github.com/acme/widgets/pull/7"
    assert body["branch_name"] == "werft/run-abc"
    assert body["base_sha"] == "deadbeef"
    assert body["merge_commit_sha"] == "c0ffee"
    assert body["error_message"] == "boom"
    assert body["result"] == {"summary": "ok", "files_changed": 3}
    assert body["latest_outcome"] == "ci_green"  # highest attempt_no

    # events: the AFTER-INSERT trigger's 'created' row, then the
    # transition trigger's 'status_changed' row — chronological order.
    event_types = [e["event_type"] for e in body["events"]]
    assert event_types == ["created", "status_changed"]
    status_changed = body["events"][1]
    assert status_changed["payload"]["from"] == "queued"
    assert status_changed["payload"]["to"] == "claimed"
    assert {"id", "event_type", "payload", "created_at"} <= set(body["events"][0].keys())

    attempts = body["attempts"]
    assert [a["attempt_no"] for a in attempts] == [1, 2]
    assert attempts[0]["outcome"] == "ci_red"
    assert attempts[0]["duration_seconds"] == 90
    assert attempts[0]["ended_at"] is not None
    assert attempts[1]["provider"] == "claude"

    artifacts = body["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["path"] == "log.jsonl"
    assert artifacts[0]["bytes"] == 2048
    # RunDetail's embedded artifact shape omits content_hash — that's the
    # standalone /artifacts endpoint's row, not this one.
    assert set(artifacts[0].keys()) == {"path", "bytes", "collected_at"}


async def test_run_detail_404_for_unknown_id(db_session, token_file, auth_headers) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{uuid.uuid4()}", headers=auth_headers)
    assert resp.status_code == 404


async def test_run_detail_malformed_uuid_is_404_or_422(
    db_session, token_file, auth_headers
) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/runs/not-a-uuid", headers=auth_headers)
    assert resp.status_code in (404, 422)


async def test_run_detail_no_attempts_or_artifacts_is_empty_lists(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item, status="queued")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}", headers=auth_headers)

    body = resp.json()
    assert body["attempts"] == []
    assert body["artifacts"] == []
    assert [e["event_type"] for e in body["events"]] == ["created"]
    assert body["latest_outcome"] is None


# -- quota status (Task 11 / B2) ---------------------------------------------


async def test_quota_status_shape_and_sums(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run1 = await seed_run(db_session, project, item)
    item2 = await seed_backlog_item(db_session, project, 2)
    run2 = await seed_run(db_session, project, item2)
    item3 = await seed_backlog_item(db_session, project, 3)
    run3 = await seed_run(db_session, project, item3)
    item4 = await seed_backlog_item(db_session, project, 4)
    run4 = await seed_run(db_session, project, item4)

    account = await seed_provider_account(
        db_session,
        provider="claude",
        label="primary",
        rolling_window_hours=5,
        ceiling_seconds=10_000,
        exhausted_source=None,
    )

    # In-window, actual set: counts toward `consumed_seconds` only —
    # `actual_wallclock_s IS NOT NULL` excludes it from `reserved_seconds`.
    await seed_quota_ledger_entry(
        db_session,
        account,
        run1,
        reserved_wallclock_s=100,
        actual_wallclock_s=120,
        consumed_offset_seconds=60,
    )
    # In-window, no actual yet: counts toward `reserved_seconds` only (the
    # `consumed`/`reserved` buckets are disjoint — a still-open reservation
    # must land in exactly one, never both, or `headroom_seconds` would
    # subtract it twice).
    await seed_quota_ledger_entry(
        db_session,
        account,
        run2,
        reserved_wallclock_s=50,
        actual_wallclock_s=None,
        consumed_offset_seconds=60,
    )
    # Outside the 5h rolling window, no actual: excluded from
    # `consumed_seconds` (it has no `actual_wallclock_s` to sum anyway) and
    # `reserved_seconds` is not window-scoped — an outstanding reservation
    # still counts regardless of age (SPEC §7's "reserved" is a
    # live-commitment figure, not a windowed utilization figure).
    await seed_quota_ledger_entry(
        db_session,
        account,
        run3,
        reserved_wallclock_s=999,
        actual_wallclock_s=None,
        consumed_offset_seconds=6 * 3600,
    )
    # Outside the window, actual set: excluded from `consumed_seconds` (too
    # old) and from `reserved_seconds` (actual is not null).
    await seed_quota_ledger_entry(
        db_session,
        account,
        run4,
        reserved_wallclock_s=10,
        actual_wallclock_s=500,
        consumed_offset_seconds=6 * 3600,
    )

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/quota", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"accounts"}
    assert len(body["accounts"]) == 1
    acct = body["accounts"][0]

    expected_fields = {
        "provider",
        "label",
        "ceiling_seconds",
        "consumed_seconds",
        "reserved_seconds",
        "headroom_seconds",
        "exhausted_until",
        "exhausted_source",
        "last_reading_utilization",
        "last_reading_source",
        "last_reading_at",
    }
    assert set(acct.keys()) == expected_fields

    assert acct["provider"] == "claude"
    assert acct["label"] == "primary"
    assert acct["ceiling_seconds"] == 10_000
    assert acct["consumed_seconds"] == 120  # run1 only: actual set, in-window
    assert acct["reserved_seconds"] == 50 + 999  # run2 + run3: open, any age
    # Every ledger row counted exactly once: 120 (run1) + 50 (run2) +
    # 999 (run3) = 1169 of committed capacity; run4 contributes nothing
    # (outside the window, actual already resolved).
    assert acct["headroom_seconds"] == 10_000 - 120 - (50 + 999)


async def test_quota_sums_do_not_bleed_between_accounts(
    db_session, token_file, auth_headers
) -> None:
    """Both correlated subqueries filter on
    `provider_account_id == ProviderAccount.id`; a query that dropped that
    predicate would sum every account's ledger rows into every account's
    row. Two accounts, each with its own ledger entries, pins that each
    account's sums reflect only its own rows."""
    project = await seed_project(db_session)
    item_a = await seed_backlog_item(db_session, project, 1)
    run_a = await seed_run(db_session, project, item_a)
    item_b = await seed_backlog_item(db_session, project, 2)
    run_b = await seed_run(db_session, project, item_b)

    account_a = await seed_provider_account(
        db_session, provider="claude", label="account-a", ceiling_seconds=10_000
    )
    account_b = await seed_provider_account(
        db_session, provider="claude", label="account-b", ceiling_seconds=10_000
    )
    await seed_quota_ledger_entry(
        db_session, account_a, run_a, reserved_wallclock_s=10, actual_wallclock_s=200
    )
    await seed_quota_ledger_entry(
        db_session, account_b, run_b, reserved_wallclock_s=300, actual_wallclock_s=None
    )

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/quota", headers=auth_headers)

    accounts = {a["label"]: a for a in resp.json()["accounts"]}
    assert accounts["account-a"]["consumed_seconds"] == 200
    assert accounts["account-a"]["reserved_seconds"] == 0
    assert accounts["account-b"]["consumed_seconds"] == 0
    assert accounts["account-b"]["reserved_seconds"] == 300


async def test_quota_windows_on_each_accounts_own_rolling_window_hours(
    db_session, token_file, auth_headers
) -> None:
    """A hardcoded window (e.g. always 5h) would pass
    `test_quota_status_shape_and_sums` by coincidence. Two accounts with
    different `rolling_window_hours`, each given one same-aged (90 min old)
    ledger entry: the 1h-window account must have already aged it out of
    `consumed_seconds` while the 10h-window account still counts it —
    proof the window comes from each row's own account, not a shared
    constant."""
    project = await seed_project(db_session)
    item_narrow = await seed_backlog_item(db_session, project, 1)
    run_narrow = await seed_run(db_session, project, item_narrow)
    item_wide = await seed_backlog_item(db_session, project, 2)
    run_wide = await seed_run(db_session, project, item_wide)

    narrow = await seed_provider_account(
        db_session,
        provider="claude",
        label="narrow-window",
        rolling_window_hours=1,
        ceiling_seconds=10_000,
    )
    wide = await seed_provider_account(
        db_session,
        provider="claude",
        label="wide-window",
        rolling_window_hours=10,
        ceiling_seconds=10_000,
    )
    ninety_minutes = 90 * 60
    await seed_quota_ledger_entry(
        db_session,
        narrow,
        run_narrow,
        reserved_wallclock_s=10,
        actual_wallclock_s=42,
        consumed_offset_seconds=ninety_minutes,
    )
    await seed_quota_ledger_entry(
        db_session,
        wide,
        run_wide,
        reserved_wallclock_s=10,
        actual_wallclock_s=42,
        consumed_offset_seconds=ninety_minutes,
    )

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/quota", headers=auth_headers)

    accounts = {a["label"]: a for a in resp.json()["accounts"]}
    assert accounts["narrow-window"]["consumed_seconds"] == 0  # 90min > 1h window
    assert accounts["wide-window"]["consumed_seconds"] == 42  # 90min < 10h window


async def test_quota_empty_account_has_zero_consumed_and_reserved(
    db_session, token_file, auth_headers
) -> None:
    await seed_provider_account(db_session, provider="claude", label="empty", ceiling_seconds=500)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/quota", headers=auth_headers)

    accounts = {a["label"]: a for a in resp.json()["accounts"]}
    empty = accounts["empty"]
    assert empty["consumed_seconds"] == 0
    assert empty["reserved_seconds"] == 0
    assert empty["headroom_seconds"] == 500


async def test_quota_headroom_floors_at_zero(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    account = await seed_provider_account(
        db_session, provider="claude", label="tight", ceiling_seconds=100
    )
    await seed_quota_ledger_entry(
        db_session, account, run, reserved_wallclock_s=500, actual_wallclock_s=500
    )

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/quota", headers=auth_headers)

    accounts = {a["label"]: a for a in resp.json()["accounts"]}
    tight = accounts["tight"]
    assert tight["consumed_seconds"] == 500
    assert tight["headroom_seconds"] == 0  # never negative


async def test_quota_exhausted_and_last_reading_fields_pass_through(
    db_session, token_file, auth_headers
) -> None:
    await seed_provider_account(
        db_session,
        provider="claude",
        label="watched",
        exhausted_source="provider_header",
        last_reading_utilization=87.5,
        last_reading_source="usage_api",
        last_reading_at_offset_seconds=30,
    )
    # exhausted_until needs a real timestamptz; seed via raw SQL offset like
    # the other datetime columns in this file.
    await db_session.execute(
        text(
            "UPDATE provider_accounts SET exhausted_until = now() + make_interval(mins => 20) "
            "WHERE label = 'watched'"
        )
    )
    await db_session.commit()

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/quota", headers=auth_headers)

    accounts = {a["label"]: a for a in resp.json()["accounts"]}
    watched = accounts["watched"]
    assert watched["exhausted_until"] is not None
    assert watched["exhausted_source"] == "provider_header"
    assert watched["last_reading_utilization"] == 87.5
    assert watched["last_reading_source"] == "usage_api"
    assert watched["last_reading_at"] is not None


# -- artifact listing (Task 11 / B2) -----------------------------------------


async def test_artifacts_endpoint_returns_rows(db_session, token_file, auth_headers) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="a.log", size=10, content_hash="sha256:aaa")
    await seed_artifact(db_session, run, path="b.log", size=20, content_hash="sha256:bbb")

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}/artifacts", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"artifacts"}
    rows = body["artifacts"]
    assert len(rows) == 2
    assert {r["path"] for r in rows} == {"a.log", "b.log"}
    by_path = {r["path"]: r for r in rows}
    assert by_path["a.log"]["bytes"] == 10
    assert by_path["a.log"]["content_hash"] == "sha256:aaa"
    assert set(rows[0].keys()) == {"path", "bytes", "collected_at", "content_hash"}


async def test_artifacts_endpoint_empty_for_run_with_no_artifacts(
    db_session, token_file, auth_headers
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}/artifacts", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == {"artifacts": []}


async def test_artifacts_endpoint_404_for_unknown_run(db_session, token_file, auth_headers) -> None:
    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{uuid.uuid4()}/artifacts", headers=auth_headers)
    assert resp.status_code == 404


# -- auth wiring reaches the new endpoints too -------------------------------


async def test_new_b2_endpoints_require_auth(db_session, token_file) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)

    app = make_client_app(db_session, token_file=token_file)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        detail_resp = await client.get(f"/api/v1/runs/{run.id}")
        quota_resp = await client.get("/api/v1/quota")
        artifacts_resp = await client.get(f"/api/v1/runs/{run.id}/artifacts")

    assert detail_resp.status_code == 401
    assert quota_resp.status_code == 401
    assert artifacts_resp.status_code == 401
