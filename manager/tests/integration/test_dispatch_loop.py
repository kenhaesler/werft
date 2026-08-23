"""The tick's dispatch half (plan decision D13's sweep order, D1's driver
registry and bounded drain) against a real Postgres.

Postgres is real, Docker is faked, and `attend_run` is faked too: what this
module pins is the *loop's* behaviour — how many claims a tick makes, when it
stops, which runs get a driver task, that a driver is never spawned twice, that
a driver's failure never reaches the tick, that the sweeps skip the runs a live
driver owns, and that a shutdown drains drivers without killing containers.
`tests/integration/test_driver.py` owns what a driver actually does.

The fake driver is installed on `loop_module.attend_run` (and the claim spy on
`loop_module.claim_next`), which is exactly how `loop.py` resolves them —
module globals at call time.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fakes import FakeDocker
from tests.integration.test_loop import FakeRepoOps, SpyAlertSink
from werft.config.dispatch import DispatchConfigCache
from werft.config.settings import Settings
from werft.db.models import Run, RunEvent
from werft.observe.alerts import NullAlertSink
from werft.orchestrator import loop as loop_module
from werft.orchestrator.loop import DispatchServices, Orchestrator
from werft.quota.ledger import LedgerQuota

__all__ = ["FakeDocker", "FakeRepoOps", "SpyAlertSink"]

#: The per-claim reservation every fixture project asks for, so a ceiling of
#: `slots * RESERVATION` admits exactly `slots` claims.
RESERVATION = 1800
DIGEST = "werft-runner-elastic@sha256:" + "d" * 64
NEW_DIGEST = "werft-runner-elastic@sha256:" + "e" * 64


# --- fixture ------------------------------------------------------------------


@pytest.fixture
async def loop_fixture(migrated_db, db_session, tmp_path, monkeypatch):
    """One project, `queued` claimable runs, an account whose ceiling admits
    exactly `ceiling_slots` of them, and an `Orchestrator` wired with
    `DispatchServices`."""
    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    driver = SimpleNamespace(
        attended=[], block=None, raises=None, started=asyncio.Event(), running=0
    )

    async def fake_attend_run(deps, run_id):
        driver.attended.append(run_id)
        driver.started.set()
        if driver.raises is not None:
            raise driver.raises
        if driver.block is not None:
            driver.running += 1
            try:
                await driver.block.wait()
            finally:
                driver.running -= 1

    monkeypatch.setattr(loop_module, "attend_run", fake_attend_run)

    real_claim_next = loop_module.claim_next
    dispatch_spy = SimpleNamespace(raise_once=None, calls=0)

    async def spying_claim_next(session, **kwargs):
        dispatch_spy.calls += 1
        if dispatch_spy.raise_once is not None:
            error, dispatch_spy.raise_once = dispatch_spy.raise_once, None
            raise error
        return await real_claim_next(session, **kwargs)

    monkeypatch.setattr(loop_module, "claim_next", spying_claim_next)

    built: list = []

    async def clean_slate() -> None:
        """The tick is the one caller that looks at the *whole* table: its
        wake, lease and attend sweeps have no project filter at all, by design
        (a crashed manager's rows are exactly the ones nobody remembers). So
        any row another module left behind — `test_dispatch_claim`'s racer
        fixtures run on their own engine and outlive `db_session`'s truncation
        — is a real candidate here, and would be claimed, parked or attended
        instead of this fixture's own. Start from an empty database rather than
        filtering, so the sweeps under test keep the unfiltered reach they have
        in production."""
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "TRUNCATE artifacts, quota_ledger, provider_accounts, run_attempts,"
                    " run_events, runs, backlog_items, project_events, projects CASCADE"
                )
            )

    async def build(
        *,
        queued: int = 0,
        ceiling_slots: int = 10,
        max_concurrent: int = 10,
        max_claims_per_tick: int = 4,
        drain_seconds: float = 0.2,
        dispatch: bool = True,
        alerts=None,
    ):
        if not built:  # only the first build in a test; a second adds to it
            await clean_slate()
        tag = uuid.uuid4().hex[:8]
        slug = f"p{tag}"
        project_id = (
            await db_session.execute(
                text(
                    "INSERT INTO projects (slug, github_owner, github_repo, unattended_branch)"
                    " VALUES (:s, 'ken', :r, 'unattended') RETURNING id"
                ),
                {"s": slug, "r": f"repo{tag}"},
            )
        ).scalar_one()
        run_ids = []
        for number in range(1, queued + 1):
            run_ids.append(
                await _insert_run(db_session, project_id, number=number, status="queued")
            )
        account_label = f"a{tag}"
        await db_session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :l, 5, :c)"
            ),
            {"l": account_label, "c": ceiling_slots * RESERVATION},
        )
        await db_session.commit()

        config_file = tmp_path / f"dispatch-{tag}.json"
        _write_dispatch_file(config_file, slug, DIGEST)
        settings = Settings(
            runs_root=str(tmp_path / "runs"),
            dispatch_config_file=str(config_file),
            lease_seconds=120,
            max_concurrent_runs=max_concurrent,
            dispatch_max_claims_per_tick=max_claims_per_tick,
            driver_drain_seconds=drain_seconds,
        )
        quota = LedgerQuota(label=account_label, typical_reservation_seconds=RESERVATION)
        docker = FakeDocker()
        services = (
            DispatchServices(
                docker=docker,
                auth=None,
                spec=None,
                config=DispatchConfigCache(str(config_file)),
                quota=quota,
            )
            if dispatch
            else None
        )
        orchestrator = Orchestrator(
            factory,
            lambda project: FakeRepoOps(),
            lambda project: FakeRepoOps(),
            alerts=alerts or NullAlertSink(),
            quota=quota,
            settings=settings,
            dispatch=services,
        )
        seeded = SimpleNamespace(
            project_id=project_id,
            slug=slug,
            run_ids=run_ids,
            factory=factory,
            settings=settings,
            config_file=config_file,
            session=db_session,
        )
        fakes = SimpleNamespace(driver=driver, dispatch=dispatch_spy, docker=docker, quota=quota)
        built.append((orchestrator, seeded, fakes))
        return orchestrator, seeded, fakes

    try:
        yield build
    finally:
        if driver.block is not None:
            driver.block.set()
        for orchestrator, _seeded, _fakes in built:
            await orchestrator.drain_drivers()
        await engine.dispose()


# --- seeding / readers --------------------------------------------------------


def _write_dispatch_file(path, slug: str, image_digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "projects": {
                    slug: {
                        "image_digest": image_digest,
                        "model": "claude-sonnet-4-6",
                        "timeout_seconds": RESERVATION,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


async def _insert_run(
    session,
    project_id,
    *,
    number: int,
    status: str,
    priority: int = 100,
    container_id: str | None = None,
    lease_in: timedelta | None = None,
    next_attempt_at: datetime | None = None,
) -> uuid.UUID:
    """Raw-SQL seeding at whatever status a test needs — the `BEFORE UPDATE`
    transition trigger never fires on insert."""
    item_id = (
        await session.execute(
            text(
                "INSERT INTO backlog_items (project_id, github_issue_number, title, body,"
                " github_updated_at) VALUES (:p, :n, 'make it work', 'the body', now())"
                " RETURNING id"
            ),
            {"p": project_id, "n": number},
        )
    ).scalar_one()
    return (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, priority, provider,"
                " container_id, lease_expires_at, next_attempt_at)"
                " VALUES (:p, :i, :s, :prio, 'claude', :cid, :lease,"
                " COALESCE(:nat, now() - interval '1 minute')) RETURNING id"
            ),
            {
                "p": project_id,
                "i": item_id,
                "s": status,
                "prio": priority,
                "cid": container_id,
                "lease": None if lease_in is None else datetime.now(UTC) + lease_in,
                "nat": next_attempt_at,
            },
        )
    ).scalar_one()


async def seed_run(seeded, **kwargs) -> uuid.UUID:
    number = 900 + len(seeded.run_ids) + uuid.uuid4().int % 90
    run_id = await _insert_run(seeded.session, seeded.project_id, number=number, **kwargs)
    await seeded.session.commit()
    return run_id


async def count_status(seeded, status: str) -> int:
    async with seeded.factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Run)
                .where(Run.project_id == seeded.project_id, Run.status == status)
            )
        ).scalar_one()


async def fetch(seeded, run_id) -> Run:
    async with seeded.factory() as session:
        return await session.get(Run, run_id)


async def dispatch_phases(seeded, run_id) -> list[str]:
    async with seeded.factory() as session:
        rows = (
            (
                await session.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "dispatch")
                    .order_by(RunEvent.id)
                )
            )
            .scalars()
            .all()
        )
    return [row.payload["phase"] for row in rows]


async def expire_deadline(seeded, run_id) -> None:
    async with seeded.factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET hard_deadline_at = :t WHERE id = :r"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "r": run_id},
        )


async def expire_lease(seeded, run_id) -> None:
    async with seeded.factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET lease_expires_at = :t WHERE id = :r"),
            {"t": datetime.now(UTC) - timedelta(minutes=5), "r": run_id},
        )


# --- the tick's bound ----------------------------------------------------------


async def test_the_tick_claims_up_to_the_bound_and_stops_at_the_first_refusal(loop_fixture):
    """D13: the ceiling is global, so a refusal means nothing else fits either —
    and continuing would only convert the queue into `blocked_quota` rows the
    wake sweep has to walk back.

    `max_claims_per_tick=5` is what makes this a test of the *stop* rather than
    of the bound: a sweep that kept going after the refusal would have the
    budget to block the fifth row too, leaving nothing `queued`."""
    orchestrator, seeded, fakes = await loop_fixture(
        queued=5, ceiling_slots=3, max_concurrent=10, max_claims_per_tick=5
    )
    await orchestrator.tick_once()
    assert await count_status(seeded, "claimed") + await count_status(seeded, "running") == 3
    assert await count_status(seeded, "blocked_quota") == 1  # the refusal that stopped the sweep
    assert await count_status(seeded, "queued") == 1


async def test_the_concurrency_cap_counts_live_drivers_not_just_this_tick(loop_fixture):
    orchestrator, seeded, fakes = await loop_fixture(queued=5, ceiling_slots=10, max_concurrent=2)
    fakes.driver.block = asyncio.Event()
    await orchestrator.tick_once()
    assert orchestrator.live_driver_count == 2
    await orchestrator.tick_once()
    assert orchestrator.live_driver_count == 2  # no headroom for more
    assert await count_status(seeded, "queued") == 3
    fakes.driver.block.set()
    await orchestrator.drain_drivers()


async def test_the_concurrency_cap_counts_a_restarts_leftovers_before_it_adopts_them(loop_fixture):
    """The first tick after a restart: the registry is empty, but N in-flight
    rows with valid leases are still this VM's work — nothing requeues them,
    and D13 runs dispatch *before* the attend sweep re-adopts them. A
    registry-only budget would admit a full fresh batch here and then adopt the
    leftovers on top of it, in the same tick."""
    orchestrator, seeded, fakes = await loop_fixture(queued=3, ceiling_slots=10, max_concurrent=2)
    fakes.driver.block = asyncio.Event()
    leftover = await seed_run(
        seeded, status="running", container_id="c1", lease_in=timedelta(minutes=5)
    )

    await orchestrator.tick_once()

    in_flight = await count_status(seeded, "claimed") + await count_status(seeded, "running")
    assert in_flight == 2  # the leftover plus exactly one new claim, never 2 + 1
    assert orchestrator.live_driver_count == 2
    assert leftover in orchestrator.live_driver_runs
    assert await count_status(seeded, "queued") == 2
    fakes.driver.block.set()
    await orchestrator.drain_drivers()


# --- the attend sweep is the only spawn path ------------------------------------


async def test_the_attend_sweep_is_the_only_spawn_path_and_adopts_a_restarts_leftovers(
    loop_fixture,
):
    """D1: after a restart the registry is empty and the attend sweep re-adopts
    `claimed`/`running` rows with the same code that runs every 15 s."""
    orchestrator, seeded, fakes = await loop_fixture(queued=0)
    run_id = await seed_run(
        seeded, status="running", container_id="c1", lease_in=timedelta(minutes=5)
    )
    await orchestrator.tick_once()
    assert fakes.driver.attended == [run_id]


async def test_a_second_tick_does_not_spawn_a_second_driver_for_the_same_run(loop_fixture):
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    fakes.driver.block = asyncio.Event()
    await orchestrator.tick_once()
    await orchestrator.tick_once()
    assert fakes.driver.attended.count(fakes.driver.attended[0]) == 1
    fakes.driver.block.set()
    await orchestrator.drain_drivers()


async def test_a_finished_driver_leaves_the_registry(loop_fixture):
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    await orchestrator.tick_once()
    await orchestrator.drain_drivers()
    assert orchestrator.live_driver_count == 0


async def test_a_driver_that_raises_is_logged_and_never_kills_the_tick(loop_fixture):
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    fakes.driver.raises = RuntimeError("boom")
    await orchestrator.tick_once()
    await orchestrator.drain_drivers()
    await orchestrator.tick_once()  # must not raise
    assert orchestrator.live_driver_count == 0


async def test_a_claim_failure_never_kills_the_tick(loop_fixture):
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    fakes.dispatch.raise_once = RuntimeError("boom")
    await orchestrator.tick_once()  # `_run_unit` logs and moves on
    assert await count_status(seeded, "queued") == 1  # the failed unit rolled back


# --- arbitration with live drivers ----------------------------------------------


async def test_the_sweeps_skip_runs_a_live_driver_owns(loop_fixture):
    """D12: an expired lease is not enough — the row must also be one no live
    driver owns. The absence of a `lease_expired` event is the load-bearing
    assertion: a sweep that acted would requeue the row, and the same tick's
    dispatch would then hand it straight back as `claimed`."""
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    fakes.driver.block = asyncio.Event()
    await orchestrator.tick_once()
    run_id = next(iter(orchestrator.live_driver_runs))
    await expire_lease(seeded, run_id)
    await orchestrator.tick_once()
    assert (await fetch(seeded, run_id)).status in ("claimed", "running")
    assert await dispatch_phases(seeded, run_id) == ["claimed"]
    fakes.driver.block.set()
    await orchestrator.drain_drivers()


async def test_the_tick_expires_a_run_past_its_hard_deadline(loop_fixture):
    """The deadline sweep's wiring, and D12's one exception: it is gated on the
    registry alone, never on the lease — the lease here is still valid."""
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    await orchestrator.tick_once()
    run_id = fakes.driver.attended[0]
    await orchestrator.drain_drivers()
    await expire_deadline(seeded, run_id)

    await orchestrator.tick_once()

    assert "deadline_killed" in await dispatch_phases(seeded, run_id)


async def test_the_tick_reaps_a_canceled_runs_container(loop_fixture):
    """D10's sweep, wired: the row keeps its `container_id` after the cancel,
    and this is the one component that owns Docker for it."""
    orchestrator, seeded, fakes = await loop_fixture(queued=0)
    run_id = await seed_run(seeded, status="canceled", container_id="c1")
    fakes.docker.containers = [{"Id": "c1", "Labels": {"werft.run_id": str(run_id)}}]

    await orchestrator.tick_once()

    assert "remove_container:c1" in fakes.docker.calls
    # T8: evidence collection runs ahead of the `reaped` marker, inside the
    # same reap.
    assert await dispatch_phases(seeded, run_id) == ["artifacts", "reaped"]


async def test_the_lease_sweep_acts_on_a_run_no_driver_owns(loop_fixture):
    """The other half of the rule: the same expired lease, with the registry
    empty (what a restart leaves behind), is the sweep's to recover."""
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    fakes.driver.block = asyncio.Event()
    await orchestrator.tick_once()
    run_id = next(iter(orchestrator.live_driver_runs))
    fakes.driver.block.set()
    await orchestrator.drain_drivers()
    await expire_lease(seeded, run_id)

    await orchestrator.tick_once()

    assert (await fetch(seeded, run_id)).status in ("claimed", "running")  # re-claimed, not stuck
    assert fakes.driver.attended.count(run_id) == 2  # requeued, claimed again, re-attended


# --- order (D13) -----------------------------------------------------------------


async def test_the_wakes_run_before_dispatch_so_a_freshly_eligible_run_claims_this_tick(
    loop_fixture,
):
    orchestrator, seeded, fakes = await loop_fixture(queued=0)
    run_id = await seed_run(
        seeded,
        status="blocked_quota",
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await orchestrator.tick_once()
    assert (await fetch(seeded, run_id)).status in ("claimed", "running")


async def test_the_lease_sweep_runs_before_dispatch_so_freed_headroom_is_visible(loop_fixture):
    """A ceiling with exactly one slot, one abandoned `claimed` run holding it,
    one queued run: the same tick must return the reservation and then claim."""
    orchestrator, seeded, fakes = await loop_fixture(queued=1, ceiling_slots=1, max_concurrent=1)
    fakes.driver.block = asyncio.Event()
    await orchestrator.tick_once()  # claims + reserves the only slot
    abandoned_id = next(iter(orchestrator.live_driver_runs))
    fakes.driver.block.set()
    await orchestrator.drain_drivers()  # the manager "died": registry empty
    await expire_lease(seeded, abandoned_id)
    fresh_id = await seed_run(seeded, status="queued", priority=500)  # claimed first

    await orchestrator.tick_once()

    assert await count_status(seeded, "claimed") + await count_status(seeded, "running") == 1
    assert (await fetch(seeded, fresh_id)).status in ("claimed", "running")
    assert (await fetch(seeded, abandoned_id)).status == "queued"


# --- the pre-T7 orchestrator ------------------------------------------------------


async def test_dispatch_does_nothing_at_all_without_dispatch_services(loop_fixture):
    """The pre-T7 `Orchestrator` construction must keep working unchanged."""
    plain, seeded, fakes = await loop_fixture(queued=3, dispatch=False)
    await plain.tick_once()
    assert await count_status(seeded, "queued") == 3
    assert fakes.driver.attended == []
    assert plain.live_driver_count == 0


# --- shutdown ---------------------------------------------------------------------


async def test_stop_drains_live_drivers_within_the_budget_and_leaves_containers_running(
    loop_fixture,
):
    """D1: a manager restart must not kill a 60-minute agent run."""
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    fakes.driver.block = asyncio.Event()  # never released
    stop = asyncio.Event()
    task = asyncio.create_task(orchestrator.run(stop))
    await fakes.driver.started.wait()
    stop.set()

    await asyncio.wait_for(task, timeout=5)

    assert orchestrator.live_driver_count == 0
    assert not any(c.startswith("kill:") for c in fakes.docker.calls)
    assert (await fetch(seeded, fakes.driver.attended[0])).status in ("claimed", "running")


# --- config (D3) -------------------------------------------------------------------


async def test_the_dispatch_config_is_re_read_once_per_sweep(loop_fixture):
    """D3: an image rebuild takes effect without a manager restart."""
    orchestrator, seeded, fakes = await loop_fixture(queued=1)
    _write_dispatch_file(seeded.config_file, seeded.slug, NEW_DIGEST)
    await orchestrator.tick_once()
    assert (await fetch(seeded, fakes.driver.attended[0])).runner_image_digest == NEW_DIGEST


# --- disk threshold: dispatch-gate half (T8, plan decision D9) --------------------
#
# The alert's rising-edge/re-arm bookkeeping is pinned in
# `test_loop.py`'s plain (dispatch-less) construction; these two need a real
# `DispatchServices` — a claimable `queued` row plus concurrency headroom —
# to prove the gate actually withholds (and later releases) a claim rather
# than merely firing the alert.


async def test_disk_over_threshold_blocks_dispatch_and_fires_alert_once(loop_fixture, monkeypatch):
    monkeypatch.setattr(loop_module, "_disk_percent_used", lambda path: 95.0)
    alerts = SpyAlertSink()
    orchestrator, seeded, fakes = await loop_fixture(
        queued=1, ceiling_slots=10, max_concurrent=10, alerts=alerts
    )

    await orchestrator.tick_once()

    assert alerts.disk_threshold_calls == [95.0]
    assert await count_status(seeded, "queued") == 1
    assert await count_status(seeded, "claimed") + await count_status(seeded, "running") == 0
    assert fakes.dispatch.calls == 0  # claim_next never even reached


async def test_disk_probe_failure_fails_open_and_dispatch_proceeds(loop_fixture, monkeypatch):
    monkeypatch.setattr(loop_module, "_disk_percent_used", lambda path: None)
    alerts = SpyAlertSink()
    orchestrator, seeded, fakes = await loop_fixture(
        queued=1, ceiling_slots=10, max_concurrent=10, alerts=alerts
    )

    await orchestrator.tick_once()

    assert alerts.disk_threshold_calls == []
    assert await count_status(seeded, "claimed") + await count_status(seeded, "running") == 1
    assert await count_status(seeded, "queued") == 0
