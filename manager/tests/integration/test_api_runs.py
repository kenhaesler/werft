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

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.routes import get_session
from werft.app import create_app
from werft.config.settings import Settings
from werft.db.models import BacklogItem, Project, Run

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
) -> None:
    await session.execute(
        text(
            "INSERT INTO run_attempts (run_id, attempt_no, provider, outcome, started_at) "
            "VALUES (:r, :n, :prov, :o, now())"
        ),
        {"r": run.id, "n": attempt_no, "prov": provider, "o": outcome},
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
