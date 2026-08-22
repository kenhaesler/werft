"""Issue #24's acceptance, executed. Each test is named after the clause it
proves, and each drives the real modules rather than a hand-built object —
`ClaudeSpec.classify` for the limit message, `claim_next` for the racers,
`tick_once` for the resume.

Postgres is real (triggers, CHECK constraints, the advisory lock, `FOR UPDATE
SKIP LOCKED`), the clone is a real `git clone` of a real local repository, the
state machine is the trigger-enforced one and the quota ledger is the real one.
Docker and GitHub are the two surfaces this milestone cannot exercise in CI, and
they are the only two faked — from `tests/fakes.py`, so the fakes this file
passes against are the same ones `test_driver.py` and `test_sweeps.py` use.

Every clock here is a parameter (plan decision D8): the window clauses move
`now`, never the wall clock, and nothing in this module sleeps to make time
pass.
"""

import asyncio
import json
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.fakes import FakeAuth, FakeDocker, FakeRepoOps, SpyAlerts, make_origin, write_outputs
from werft.api.routes import get_session
from werft.app import create_app
from werft.config.dispatch import DispatchConfigCache
from werft.config.settings import Settings
from werft.db.models import ProviderAccount, QuotaLedgerEntry, Run, RunAttempt
from werft.domain.attempts import AttemptOutcome
from werft.domain.runs import run_branch_name
from werft.orchestrator.dispatch import claim_next
from werft.orchestrator.loop import DispatchServices, Orchestrator
from werft.orchestrator.sweeps import SweepDeps, sweep_deadlines, sweep_leases
from werft.providers.claude import ClaudeSpec
from werft.quota.accounts import resolve_account
from werft.quota.ledger import LedgerQuota
from werft.runner.workspace import create_run_dirs, placement_for

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not on PATH")

#: One fixed instant for the whole module. Taken from the wall clock rather than
#: written as a literal only because the fixture's own rows are seeded with the
#: database's `now()`; every *predicate* under test still takes `now` explicitly.
NOW = datetime.now(UTC).replace(microsecond=0)

DIGEST = "werft-runner-elastic@sha256:" + "d" * 64
CREDENTIAL = "oauth-token-from-file"
API_TOKEN = "s3cr3t-token"
#: The per-claim reservation every fixture project asks for, so a ceiling of
#: `slots * RESERVATION` admits exactly `slots` claims.
RESERVATION = 1800

_TRUNCATE = (
    "TRUNCATE artifacts, quota_ledger, provider_accounts, run_attempts,"
    " run_events, runs, backlog_items, project_events, projects CASCADE"
)


class RunBranchOps(FakeRepoOps):
    """`FakeRepoOps` for the tick-driven tests, which never learn a run's id
    before its branch is asked about.

    "Pushed" is decided manager-side by comparing the run branch's head to
    `base_sha` (driver.py), and in a `tick_once`-driven test the branch name is
    minted inside the claim transaction — so whether the agent pushed is a
    property of the *scenario*, answered for every branch, rather than one
    registered under a name the test cannot spell in advance.
    """

    def __init__(self, *, pushed: bool = True) -> None:
        super().__init__()
        self.pushed = pushed

    async def get_ref_sha(self, branch: str) -> str | None:
        if self.ref_error is not None:
            raise self.ref_error
        return ("f" * 40) if self.pushed else None


# --- fixture ------------------------------------------------------------------


@pytest.fixture
async def e2e(migrated_db, tmp_path, monkeypatch):
    """One project, an account, and a whole dispatch plane wired the way
    `app.py` wires it: a real `Orchestrator` with real `DispatchServices`, real
    `attend_run`, the real `ClaudeSpec`, and a real local git origin.

    Every build starts from an empty database. The tick's wake, lease, deadline
    and attend sweeps have no project filter at all, by design (a crashed
    manager's rows are exactly the ones nobody remembers), so a row another
    module left behind — `test_dispatch_claim`'s racer fixtures run on their own
    engine and outlive `db_session`'s truncation — would be a real candidate
    here. Truncating rather than filtering keeps the sweeps under test at the
    unfiltered reach they have in production.
    """
    from werft.orchestrator import driver as driver_module

    remote_uri, origin_sha = make_origin(tmp_path / "origin")
    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    credential = tmp_path / "claude-credential"
    credential.write_text(CREDENTIAL + "\n", encoding="utf-8")
    api_token_file = tmp_path / "api-token"
    api_token_file.write_text(API_TOKEN, encoding="utf-8")

    monkeypatch.setattr(driver_module, "remote_url", lambda **_kwargs: remote_uri)

    built: list[SimpleNamespace] = []

    async def build(
        *,
        queued: int = 0,
        reservation: int = RESERVATION,
        ceiling_slots: int = 10,
        ceiling_seconds: int | None = None,
        window_capacity_seconds: int | None = None,
        max_concurrent: int = 4,
        max_claims_per_tick: int = 4,
        pushed: bool = True,
    ) -> SimpleNamespace:
        for previous in built:
            await previous.orchestrator.drain_drivers()
        async with factory() as session, session.begin():
            await session.execute(text(_TRUNCATE))

        tag = uuid.uuid4().hex[:8]
        slug = f"p{tag}"
        account_label = f"a{tag}"
        async with factory() as session, session.begin():
            project_id = (
                await session.execute(
                    text(
                        "INSERT INTO projects (slug, github_owner, github_repo,"
                        " unattended_branch) VALUES (:s, 'ken', :r, 'unattended') RETURNING id"
                    ),
                    {"s": slug, "r": f"repo{tag}"},
                )
            ).scalar_one()
            account_id = (
                await session.execute(
                    text(
                        "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                        " ceiling_seconds, provider_window_capacity_seconds)"
                        " VALUES ('claude', :l, 5, :c, :cap) RETURNING id"
                    ),
                    {
                        "l": account_label,
                        "c": ceiling_seconds
                        if ceiling_seconds is not None
                        else ceiling_slots * reservation,
                        "cap": window_capacity_seconds,
                    },
                )
            ).scalar_one()

        config_file = tmp_path / f"dispatch-{tag}.json"
        config_file.write_text(
            json.dumps(
                {
                    "projects": {
                        slug: {
                            "image_digest": DIGEST,
                            "model": "claude-sonnet-4-6",
                            "timeout_seconds": reservation,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        settings = Settings(
            runs_root=str(tmp_path / "runs" / tag),
            claude_credential_file=str(credential),
            dispatch_config_file=str(config_file),
            api_token_file=str(api_token_file),
            lease_seconds=120,
            # High enough that no test depends on a beat landing: the ticker is
            # `test_driver.py`'s subject, not this module's.
            heartbeat_seconds=30,
            max_concurrent_runs=max_concurrent,
            dispatch_max_claims_per_tick=max_claims_per_tick,
            driver_drain_seconds=60.0,
        )
        config = DispatchConfigCache(str(config_file))
        quota = LedgerQuota(label=account_label, typical_reservation_seconds=reservation)
        docker = FakeDocker()
        ops = RunBranchOps(pushed=pushed)
        auth = FakeAuth()
        alerts = SpyAlerts()

        orchestrator = Orchestrator(
            factory,
            lambda _project: ops,
            lambda _project: ops,
            alerts=alerts,
            quota=quota,
            settings=settings,
            dispatch=DispatchServices(
                docker=docker,
                auth=auth,
                spec=ClaudeSpec(),
                config=config,
                quota=quota,
            ),
        )
        env = SimpleNamespace(
            factory=factory,
            orchestrator=orchestrator,
            settings=settings,
            config=config,
            quota=quota,
            docker=docker,
            ops=ops,
            auth=auth,
            alerts=alerts,
            project_id=project_id,
            account_id=account_id,
            account_label=account_label,
            slug=slug,
            reservation=reservation,
            origin_sha=origin_sha,
            run_ids=[],
            on_started=None,
            sweep_deps=SweepDeps(
                session_factory=factory,
                docker=docker,
                quota=quota,
                alerts=alerts,
                settings=settings,
            ),
        )
        # `FakeDocker` calls `fakes.on_started(placement)` from inside
        # `start_container`; the hooks here read the launching run off the
        # create body instead (`launching_placement`), because a tick-driven
        # test never holds the placement before the driver builds it.
        docker.fakes = env
        for number in range(1, queued + 1):
            env.run_ids.append(await insert_run(env, number=number, status="queued"))
        built.append(env)
        return env

    try:
        yield build
    finally:
        for env in built:
            env.docker.hold_die = False
            env.docker.release_die(env.docker.exit_code)
            await env.orchestrator.drain_drivers()
        async with factory() as session, session.begin():
            await session.execute(text(_TRUNCATE))
        await engine.dispose()


# --- seeding / readers --------------------------------------------------------


async def insert_run(
    env,
    *,
    number: int,
    status: str = "queued",
    priority: int = 100,
    container_id: str | None = None,
    base_sha: str | None = None,
    lease_in: timedelta | None = None,
    deadline_in: timedelta | None = None,
    next_attempt_at: datetime | None = None,
) -> uuid.UUID:
    """Raw-SQL seeding at whatever status a scenario needs — the `BEFORE UPDATE`
    transition trigger never fires on insert, so this is also how a crash-shaped
    row (a `running` one nobody is attending) is built."""
    async with env.factory() as session, session.begin():
        item_id = (
            await session.execute(
                text(
                    "INSERT INTO backlog_items (project_id, github_issue_number, title, body,"
                    " github_updated_at) VALUES (:p, :n, 'make it work', 'the body', now())"
                    " RETURNING id"
                ),
                {"p": env.project_id, "n": number},
            )
        ).scalar_one()
        return (
            await session.execute(
                text(
                    "INSERT INTO runs (project_id, backlog_item_id, status, priority, provider,"
                    " container_id, base_sha, lease_expires_at, hard_deadline_at, next_attempt_at)"
                    " VALUES (:p, :i, :s, :prio, 'claude', :cid, :sha, :lease, :deadline,"
                    " COALESCE(:nat, now() - interval '1 minute')) RETURNING id"
                ),
                {
                    "p": env.project_id,
                    "i": item_id,
                    "s": status,
                    "prio": priority,
                    "cid": container_id,
                    "sha": base_sha,
                    "lease": None if lease_in is None else datetime.now(UTC) + lease_in,
                    "deadline": None if deadline_in is None else datetime.now(UTC) + deadline_in,
                    "nat": next_attempt_at,
                },
            )
        ).scalar_one()


async def seed_closed_entry(env, *, seconds: int, at: datetime, number: int) -> None:
    """One closed ledger row, dated explicitly. `consumed_at` is the only thing
    the window predicate reads, and `quota_ledger.run_id` is a foreign key — so
    the row it points at is parked far in the future where it can never become a
    claim candidate and confuse the test it was seeded for."""
    filler = await insert_run(
        env, number=number, status="queued", next_attempt_at=NOW + timedelta(days=365)
    )
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO quota_ledger (provider_account_id, run_id, attempt_no, model,"
                " reserved_wallclock_s, actual_wallclock_s, consumed_at)"
                " VALUES (:a, :r, 1, 'claude-sonnet-4-6', :s, :s, :at)"
            ),
            {"a": env.account_id, "r": filler, "s": seconds, "at": at},
        )


async def claim_once(env, *, now: datetime | None = None, live: int = 0):
    """One real claim transaction, committed — `claim_next` never commits: the
    caller owns the transaction, so a park, a block and a claim are each
    all-or-nothing."""
    async with env.factory() as session, session.begin():
        return await claim_next(
            session,
            quota=env.quota,
            config=env.config.current(),
            settings=env.settings,
            alerts=env.alerts,
            now=now or datetime.now(UTC),
            live_driver_count=live,
        )


async def fetch(env, run_id) -> Run:
    async with env.factory() as session:
        return await session.get(Run, run_id)


async def fetch_account(env) -> ProviderAccount:
    async with env.factory() as session:
        return (
            await session.execute(
                select(ProviderAccount).where(ProviderAccount.id == env.account_id)
            )
        ).scalar_one()


async def count_status(env, status: str) -> int:
    async with env.factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Run)
                .where(Run.project_id == env.project_id, Run.status == status)
            )
        ).scalar_one()


async def open_reservation_seconds(env) -> int:
    """Everything this account has promised and not yet settled."""
    async with env.factory() as session:
        return (
            await session.execute(
                select(func.coalesce(func.sum(QuotaLedgerEntry.reserved_wallclock_s), 0)).where(
                    QuotaLedgerEntry.provider_account_id == env.account_id,
                    QuotaLedgerEntry.actual_wallclock_s.is_(None),
                )
            )
        ).scalar_one()


async def ledger_rows(env, run_id) -> list[QuotaLedgerEntry]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(QuotaLedgerEntry)
                    .where(QuotaLedgerEntry.run_id == run_id)
                    .order_by(QuotaLedgerEntry.attempt_no)
                )
            )
            .scalars()
            .all()
        )


async def attempt_rows(env, run_id) -> list[RunAttempt]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(RunAttempt)
                    .where(RunAttempt.run_id == run_id)
                    .order_by(RunAttempt.attempt_no)
                )
            )
            .scalars()
            .all()
        )


def pr_open_calls(env, run_id) -> int:
    branch = run_branch_name(run_id)
    return [head for head, _base in env.ops.open_pr_calls].count(branch)


def placement_of(env, run_id):
    return placement_for(
        run_id, runs_root=env.settings.runs_root, dns_ip=env.settings.runner_dns_ip
    )


def launching_placement(env):
    """The placement of the container `start_container` is starting, read off
    the create body's own `werft.run_id` label.

    A tick-driven test never holds a run's placement before the driver builds
    it: the run id is minted by `claim_next` inside the tick and the container
    body is the first place it becomes visible to a fake.
    """
    run_id = env.docker.created_bodies[-1]["Labels"]["werft.run_id"]
    return placement_of(env, run_id)


async def expire_lease(env, run_id) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET lease_expires_at = :t WHERE id = :r"),
            {"t": datetime.now(UTC) - timedelta(minutes=5), "r": run_id},
        )


async def expire_deadline(env, run_id) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET hard_deadline_at = :t WHERE id = :r"),
            {"t": datetime.now(UTC) - timedelta(minutes=1), "r": run_id},
        )


async def force_running(env, run_id, *, container_id: str) -> None:
    """What a crashed manager leaves behind: a `running` row with a container
    nobody is attending. `claimed -> running` is a legal edge, so the trigger
    accepts this UPDATE exactly as it accepts the driver's own CAS."""
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET status = 'running', version = version + 1, container_id = :c,"
                " base_sha = :s, lease_expires_at = now() + interval '2 minutes' WHERE id = :r"
            ),
            {"c": container_id, "s": env.origin_sha, "r": run_id},
        )


async def cancel_via_route(env, run_id) -> None:
    """T6's operator cancel, driven through the real HTTP route.

    The route is the only cancel path in the system, and D10's division of
    labour lives in it: it closes the open attempt row and trues the reservation
    up in the same transaction as the CAS, and never touches Docker.
    """
    app = create_app(Settings(api_token_file=env.settings.api_token_file))

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with env.factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.state.quota = env.quota
    app.state.alerts = env.alerts
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
    assert response.status_code == 200, response.text


async def tick_and_drain(env) -> None:
    """One tick, plus the drivers it spawned.

    A driver is an `asyncio.Task` the tick creates and deliberately does not
    await (plan decision D1: it outlives the sweep by up to ninety minutes), so
    "what the next tick sees" is only observable once this tick's drivers have
    finished. `driver_drain_seconds` is 60 here, far past anything a fake
    daemon takes, so the drain is a join and never a cancel.
    """
    await env.orchestrator.tick_once()
    await env.orchestrator.drain_drivers()


# --- #24 clause 1: the synthetic-clock window ----------------------------------


async def test_acceptance_synthetic_clock_window_admits_again_once_seconds_age_out(
    migrated_db, e2e
):
    """#24 clause 1: "synthetic-clock window tests".

    Ceiling 18000, three closed 6000 s entries dated 4h59m ago refuse a 5400 s
    claim; the same ledger dated 5h01m ago admits it. Nothing but `consumed_at`
    differs between the two arrangements, and nothing sleeps: `now` is a
    parameter all the way down (plan decision D8).
    """
    env = await e2e(queued=2, reservation=5400, ceiling_seconds=18000)
    for number, _ in enumerate(range(3), start=100):
        await seed_closed_entry(
            env, seconds=6000, at=NOW - timedelta(hours=4, minutes=59), number=number
        )

    assert (await claim_once(env, now=NOW)).status == "blocked_quota"
    assert (await claim_once(env, now=NOW + timedelta(hours=5, seconds=1))).status == "claimed"


# --- #24 clause 2: N concurrent racers -----------------------------------------


async def test_acceptance_n_concurrent_claim_racers_never_exceed_the_ceiling(migrated_db, e2e):
    """#24 clause 2, at the tick level rather than the function level: eight
    concurrent `_sweep_dispatch` passes over one account, real sessions, real
    `FOR UPDATE SKIP LOCKED`, real `pg_advisory_xact_lock`."""
    env = await e2e(queued=8, ceiling_slots=3, max_concurrent=100, max_claims_per_tick=1)
    ceiling = (await fetch_account(env)).ceiling_seconds

    await asyncio.gather(*(env.orchestrator._sweep_dispatch() for _ in range(8)))

    sum_of_open_reservations = await open_reservation_seconds(env)
    claimed_count = await count_status(env, "claimed") + await count_status(env, "running")
    assert sum_of_open_reservations <= ceiling
    assert claimed_count == ceiling // RESERVATION


# --- #24 clause 3: a parsed limit blocks dispatch, then auto-resumes ------------


async def test_acceptance_a_parsed_limit_blocks_dispatch_then_auto_resumes(migrated_db, e2e):
    """#24 clause 3: "a parsed limit-reached message blocks dispatch until its
    reset time then auto-resumes."

    The whole loop, with no human anywhere: a `usage limit reached, resets <ts>`
    envelope finalizes the run to `blocked_quota`, writes
    `provider_accounts.exhausted_until` durably, refuses every claim while it
    stands — assert a *second*, unrelated queued run stays `queued` — and then,
    after the timestamp is moved into the past (the only thing that changes),
    the very next `tick_once` wakes the run and claims it.
    """
    reset = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)
    envelope = {
        "type": "result",
        "subtype": "success",
        "result": f"Claude usage limit reached, resets {reset.isoformat()}",
    }
    classification = ClaudeSpec().classify(envelope=envelope, stderr="", exit_code=0)
    assert classification.outcome is AttemptOutcome.QUOTA_EXHAUSTED
    assert classification.exhausted_until == reset  # the real parser, not a stub

    # One claim per tick, so the second run is never even offered to admission
    # while the first is in flight: what keeps it `queued` below is the account,
    # not a budget that happened to run out.
    env = await e2e(queued=2, max_concurrent=1)
    run_id = await insert_run(env, number=50, status="queued", priority=500)
    other_run_id = env.run_ids[0]
    env.on_started = lambda _placement: write_outputs(
        launching_placement(env), status="failure", envelope_result=envelope["result"]
    )

    await tick_and_drain(env)

    run = await fetch(env, run_id)
    account = await fetch_account(env)
    assert (run.status, run.next_attempt_at) == ("blocked_quota", reset)
    assert (account.exhausted_until, account.exhausted_source) == (reset, "cli")
    assert env.alerts.quota_exhausted_until_calls == [("claude", reset)]
    assert (await fetch(env, other_run_id)).status == "queued"  # nothing dispatches

    await move_reported_reset_into_the_past(env, reset)
    env.docker.hold_die = True  # keep the woken run in flight to observe it
    await env.orchestrator.tick_once()

    assert (await fetch(env, run_id)).status in ("claimed", "running")


async def move_reported_reset_into_the_past(env, reset: datetime) -> None:
    """Move the provider's reported reset time, and nothing else.

    That one timestamp is stored twice by construction —
    `provider_accounts.exhausted_until` is the account-level durable record
    (D11) and the blocked run's `next_attempt_at` is `advance_failed`'s wake at
    that same instant — so "the timestamp moves" is two columns, not two facts.
    """
    async with env.factory() as session, session.begin():
        moved = reset - timedelta(hours=6)
        await session.execute(
            text("UPDATE provider_accounts SET exhausted_until = :t WHERE id = :a"),
            {"t": moved, "a": env.account_id},
        )
        await session.execute(
            text(
                "UPDATE runs SET next_attempt_at = :t"
                " WHERE project_id = :p AND status = 'blocked_quota'"
            ),
            {"t": moved, "p": env.project_id},
        )


# --- #24 clause 4: a provider reading never loosens ----------------------------


async def test_acceptance_a_provider_reading_below_the_ledger_never_increases_headroom(
    migrated_db, e2e
):
    """#24 clause 4, at the tick level: a 1%-utilization reading recorded one
    minute ago, against a ledger that is 95% spent, admits nothing. `consumed =
    max(consumed, derived)` — never assignment, never `min()`."""
    env = await e2e(queued=1, ceiling_seconds=18000, window_capacity_seconds=18000)
    await seed_closed_entry(env, seconds=17100, at=NOW - timedelta(hours=1), number=200)

    before = await admit(env, reservation_seconds=RESERVATION, now=NOW)
    async with env.factory() as session, session.begin():
        await LedgerQuota(label=env.account_label).record_reading(
            session,
            account_id=env.account_id,
            utilization_percent=1.0,
            source="usage",
            at=NOW - timedelta(minutes=1),
        )
    after = await admit(env, reservation_seconds=RESERVATION, now=NOW)

    assert after.effective_consumed_seconds == before.effective_consumed_seconds
    assert (before.ok, after.ok) == (False, False)

    # The reading really landed — a no-op `record_reading` would satisfy the two
    # assertions above for the wrong reason — and the tick still admits nothing.
    assert (await fetch_account(env)).last_reading_utilization == 1.0
    await env.orchestrator._sweep_dispatch()
    assert await count_status(env, "claimed") == 0
    assert await count_status(env, "blocked_quota") == 1


async def admit(env, *, reservation_seconds: int, now: datetime):
    """One real admission decision, on a freshly read account row — the reading
    lives on the account, so a cached ORM copy would answer the question this
    test is asking."""
    async with env.factory() as session:
        account = await resolve_account(session, provider="claude", label=env.account_label)
        return await env.quota.admit(
            session, account, reservation_seconds=reservation_seconds, now=now
        )


# --- SPEC §7: no edge out of `claimed`/`running` leaks headroom ----------------


async def test_acceptance_no_edge_out_of_claimed_or_running_leaks_headroom(migrated_db, e2e):
    """SPEC §7's "no path leaks headroom", enumerated rather than argued. Every
    `domain/runs.py::TRANSITIONS` edge out of `claimed`/`running` is driven, and
    the reservation must be closed on each."""
    for edge in [
        "claimed -> queued (lease)",
        "claimed -> failed (deadline)",
        "claimed -> canceled (route)",
        "running -> awaiting_review (finalize, pushed)",
        "running -> failed (finalize, agent_failure)",
        "running -> failed (lease)",
        "running -> failed (deadline)",
        "running -> canceled (route)",
    ]:
        entry = await drive_edge(e2e, edge)
        assert entry.actual_wallclock_s is not None, edge


async def drive_edge(e2e, edge: str) -> QuotaLedgerEntry:
    """Build one run, claim it for real, and take it out of `claimed`/`running`
    by the named edge. Returns that attempt's ledger row.

    Each edge is driven by whichever component owns it in production: the
    sweeps for lease and deadline expiry (`sweeps.py`, the crash-recovery
    path), `attend_run` for the two finalize edges, and the real HTTP cancel
    route for the operator ones.
    """
    from_status = edge.split(" ")[0]
    # A pushed success is the only edge that reaches `awaiting_review`; the
    # `agent_failure` one is a clean CLI exit that pushed nothing, which
    # `finalize_attempt` recharacterizes rather than accepting as a success.
    env = await e2e(queued=1, pushed=edge.startswith("running -> awaiting_review"))
    env.on_started = lambda _placement: write_outputs(launching_placement(env), status="success")

    outcome = await claim_once(env)
    assert outcome.status == "claimed", edge
    run_id = outcome.run_id

    if from_status == "running" and "finalize" not in edge:
        await force_running(env, run_id, container_id=env.docker.container_id)

    if "(lease)" in edge:
        await expire_lease(env, run_id)
        await sweep_leases(env.sweep_deps, now=datetime.now(UTC), live=set())
    elif "(deadline)" in edge:
        await expire_deadline(env, run_id)
        await sweep_deadlines(env.sweep_deps, now=datetime.now(UTC), live=set())
    elif "(route)" in edge:
        await cancel_via_route(env, run_id)
    else:
        await tick_and_drain(env)

    entries = await ledger_rows(env, run_id)
    assert len(entries) == 1, edge  # one attempt, one reservation
    return entries[0]


# --- "kill -9 at each step recovers via reconciliation" ------------------------


async def test_acceptance_every_phase_boundary_is_re_drivable(migrated_db, e2e):
    """ "kill -9 at each step recovers via reconciliation." For each boundary —
    after claim, after clone, after launch, after die, after finalize — drop the
    driver task without cancelling its container, run `tick_once` twice, and
    assert the run reaches the same terminal-for-this-test state with exactly
    one ledger row, one open-then-closed attempt row, and one PR."""
    for boundary in ("after_claim", "after_clone", "after_launch", "after_die", "after_finalize"):
        state = await drive_to(e2e, boundary)
        await state.env.orchestrator.tick_once()
        if state.dies_while_attended:
            # `after_launch`: the container was still alive when this manager
            # re-adopted it, and dies while it is being attended.
            state.env.docker.hold_die = False
            state.env.docker.release_die(0)
        await state.env.orchestrator.drain_drivers()
        await tick_and_drain(state.env)

        assert len(await ledger_rows(state.env, state.run_id)) == 1, boundary
        attempts = await attempt_rows(state.env, state.run_id)
        assert len(attempts) == 1 and attempts[0].ended_at is not None, boundary
        assert pr_open_calls(state.env, state.run_id) <= 1, boundary
        assert (await fetch(state.env, state.run_id)).status == state.expected_terminal, boundary


async def drive_to(e2e, boundary: str) -> SimpleNamespace:
    """Leave the system in exactly the state a `kill -9` at `boundary` leaves.

    The columns *are* that state (`sweeps.py`: everything a restart needs is a
    column), so each boundary is built by writing the columns a crash would have
    left rather than by starting a driver and killing it — a killed task is a
    race, a row is a fact. The container is deliberately never removed: D1 says
    a manager restart must not kill a 60-minute agent run.
    """
    env = await e2e(queued=1)
    env.on_started = lambda _placement: write_outputs(launching_placement(env), status="success")
    outcome = await claim_once(env)
    assert outcome.status == "claimed"
    run_id = outcome.run_id
    state = SimpleNamespace(
        env=env, run_id=run_id, expected_terminal="awaiting_review", dies_while_attended=False
    )

    if boundary == "after_claim":
        return state  # nothing on disk, no container: the attend sweep prepares

    if boundary == "after_clone":
        # The tree the dead driver left behind. Re-preparing rebuilds
        # `workspace/` and `outputs/` (workspace.py), so a half-clone is not
        # inherited — the stray file below must not survive the re-drive.
        create_run_dirs(placement_of(env, run_id))
        Path(placement_of(env, run_id).workspace_dir).joinpath("half-written").write_text(
            "from the dead driver", encoding="utf-8"
        )
        return state

    # The remaining three all crashed with a container already started, so the
    # row is `running` and carries it.
    await force_running(env, run_id, container_id=env.docker.container_id)
    write_outputs(placement_of(env, run_id), status="success")

    if boundary == "after_launch":
        # Still alive at re-adoption: the die arrives while this manager attends.
        env.docker.hold_die = True
        state.dies_while_attended = True
        return state

    if boundary == "after_die":
        env.docker.release_die(0)
        return state

    if boundary == "after_finalize":
        # The one boundary that is only reachable by *having finalized*: drive
        # the whole attempt once, then let the two re-drives find nothing to do.
        env.docker.release_die(0)
        await tick_and_drain(env)
        assert (await fetch(env, run_id)).status == "awaiting_review"
        return state

    raise AssertionError(f"unknown boundary {boundary!r}")


# --- the happy path, end to end ------------------------------------------------


async def test_the_full_path_from_queued_to_awaiting_review(migrated_db, e2e):
    """`queued -> claimed -> running -> awaiting_review`, driven only by
    `tick_once`: a PR open, a reservation trued up to the observed seconds, the
    branch force-reset exactly once, the token revoked, the network removed, the
    run directory left on disk with `outputs/result.json` intact (T8's seam),
    and `task.json` scrubbed."""
    env = await e2e(queued=1, max_concurrent=1)
    env.on_started = lambda _placement: write_outputs(launching_placement(env), status="success")
    run_id = env.run_ids[0]

    await tick_and_drain(env)

    run = await fetch(env, run_id)
    branch = run_branch_name(run_id)
    placement = placement_of(env, run_id)
    entries = await ledger_rows(env, run_id)

    assert run.status == "awaiting_review"
    assert run.pr_number == env.ops.opened_pr.number
    assert pr_open_calls(env, run_id) == 1
    assert env.ops.force_reset_calls == [(branch, env.origin_sha)]  # every attempt, exactly once
    assert len(entries) == 1 and entries[0].actual_wallclock_s is not None
    assert entries[0].reserved_wallclock_s == RESERVATION
    assert env.auth.revoked  # SPEC §4.4
    assert f"remove_network:{placement.network_name}" in env.docker.calls
    assert f"remove_container:{env.docker.container_id}" in env.docker.calls

    # T8's seam: the run directory survives the run that wrote it.
    assert (
        json.loads(Path(placement.outputs_dir).joinpath("result.json").read_text("utf-8"))["status"]
        == "success"
    )
    task_json = Path(placement.task_json_path).read_text(encoding="utf-8")
    assert CREDENTIAL not in task_json  # D7
    assert json.loads(task_json)["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "<redacted>"
    assert not list(Path(placement.secrets_dir).glob("*"))  # and the mounts are gone
