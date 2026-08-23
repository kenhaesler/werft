"""`orchestrator/evidence.py` against a real DB (T8 task 5).

Seeding follows `test_finalize.py`/`test_sweeps.py`'s idiom: raw-SQL seed
helpers for `projects`/`backlog_items`/`runs`, then a real `async_sessionmaker`
built off `migrated_db` for the session-factory duck type the function needs
(the loop/dispatch/sweep integration tests all build theirs the same way).
"""

import os
import stat
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from werft.db.models import Artifact, Project, RunEvent
from werft.orchestrator.evidence import _event_payload, collect_run_evidence
from werft.runner.collect import CollectionReport, DroppedEntry
from werft.runner.create_body import RunPlacement

# -- seeding ------------------------------------------------------------------


async def seed_project(session) -> Project:
    tag = uuid.uuid4().hex[:8]
    pid = (
        await session.execute(
            text(
                "INSERT INTO projects (slug, github_owner, github_repo) "
                "VALUES (:slug, 'o', :repo) RETURNING id"
            ),
            {"slug": f"p-{tag}", "repo": f"r-{tag}"},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Project, pid)


async def seed_run(session, project: Project) -> uuid.UUID:
    item_id = (
        await session.execute(
            text(
                "INSERT INTO backlog_items (project_id, github_issue_number, title, "
                "github_updated_at) VALUES (:p, 1, 'an item', now()) RETURNING id"
            ),
            {"p": project.id},
        )
    ).scalar_one()
    run_id = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status) "
                "VALUES (:p, :b, 'running') RETURNING id"
            ),
            {"p": project.id, "b": item_id},
        )
    ).scalar_one()
    await session.commit()
    return run_id


def make_placement(run_id: uuid.UUID, run_dir) -> RunPlacement:
    run_dir_s = str(run_dir)
    return RunPlacement(
        run_id=str(run_id),
        container_name=f"werft-run-{run_id}",
        network_name=f"werft-net-{run_id}",
        dns_ip="10.0.0.2",
        run_dir=run_dir_s,
        workspace_dir=os.path.join(run_dir_s, "workspace"),
        outputs_dir=os.path.join(run_dir_s, "outputs"),
        task_json_path=os.path.join(run_dir_s, "task.json"),
        secrets_dir=os.path.join(run_dir_s, "secrets"),
    )


@pytest.fixture
async def factory(migrated_db) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_db)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def dispatch_event(db_session, run_id: uuid.UUID) -> RunEvent:
    rows = (
        (
            await db_session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .where(RunEvent.event_type == "dispatch")
                .order_by(RunEvent.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, rows
    return rows[0]


# -- tests ----------------------------------------------------------------


async def test_collects_writes_rows_and_event(tmp_path, migrated_db, db_session, factory):
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "workspace" / ".werft-artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "report.html").write_bytes(b"hello")  # 5 bytes

    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "log.jsonl").write_bytes(b"ab")  # 2 bytes

    placement = make_placement(run_id, run_dir)
    store = tmp_path / "store"

    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(store),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )

    assert report is not None
    assert {c.rel_path for c in report.artifacts} == {
        "werft-artifacts/report.html",
        "outputs/log.jsonl",
    }

    rows = (
        (await db_session.execute(select(Artifact).where(Artifact.run_id == run_id)))
        .scalars()
        .all()
    )
    assert {(r.path, r.bytes) for r in rows} == {
        ("werft-artifacts/report.html", 5),
        ("outputs/log.jsonl", 2),
    }
    assert all(r.content_hash is None and r.event_ref is None for r in rows)

    ev = await dispatch_event(db_session, run_id)
    assert ev.payload["phase"] == "artifacts"
    assert ev.payload["collected"] == 2
    assert ev.payload["truncated"] is False
    assert ev.payload["dropped"] == []
    assert "dropped_elided" not in ev.payload


async def test_zero_artifacts_still_writes_event(tmp_path, migrated_db, db_session, factory):
    # The container died before writing anything: the placement dirs exist
    # (as `lifecycle.py` would have created them) but hold nothing. The D5
    # binding rule is "one event per collection pass, always" — the
    # `if report.artifacts:` guard around the upsert must not also skip the
    # event.
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "outputs").mkdir(parents=True)

    placement = make_placement(run_id, run_dir)
    store = tmp_path / "store"

    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(store),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )

    assert report is not None
    assert report.artifacts == []

    rows = (
        (await db_session.execute(select(Artifact).where(Artifact.run_id == run_id)))
        .scalars()
        .all()
    )
    assert rows == []

    ev = await dispatch_event(db_session, run_id)
    assert ev.payload["phase"] == "artifacts"
    assert ev.payload["collected"] == 0


async def test_recollection_upserts_not_duplicates(tmp_path, migrated_db, db_session, factory):
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True)
    log_path = outputs_dir / "log.jsonl"
    log_path.write_bytes(b"ab")

    placement = make_placement(run_id, run_dir)
    store = tmp_path / "store"

    await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(store),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )

    log_path.write_bytes(b"abcdef")  # grows from 2 to 6 bytes

    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(store),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )
    assert report is not None

    rows = (
        (await db_session.execute(select(Artifact).where(Artifact.run_id == run_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].path == "outputs/log.jsonl"
    assert rows[0].bytes == 6

    events = (
        (
            await db_session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .where(RunEvent.event_type == "dispatch")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2


async def test_drop_event_names_exact_files(tmp_path, migrated_db, db_session, factory):
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True)

    max_total = 10
    (outputs_dir / "small.bin").write_bytes(b"x" * 4)
    (outputs_dir / "big.bin").write_bytes(b"y" * 9)  # small(4) + big(9) > 10

    placement = make_placement(run_id, run_dir)
    store = tmp_path / "store"

    import werft.orchestrator.evidence as evidence_mod

    monkeypatch_total = evidence_mod.MAX_TOTAL_BYTES
    evidence_mod.MAX_TOTAL_BYTES = max_total
    try:
        report = await collect_run_evidence(
            factory,
            run_id=run_id,
            placement=placement,
            artifacts_root=str(store),
            subnets=[],
            squid_access_log="",
            dns_guard_query_log="",
        )
    finally:
        evidence_mod.MAX_TOTAL_BYTES = monkeypatch_total

    assert report is not None
    assert {c.rel_path for c in report.artifacts} == {"outputs/small.bin"}
    assert [(d.rel_path, d.reason) for d in report.dropped] == [("outputs/big.bin", "total_cap")]

    ev = await dispatch_event(db_session, run_id)
    assert ev.payload["dropped"] == [{"path": "outputs/big.bin", "reason": "total_cap", "bytes": 9}]
    assert ev.payload["truncated"] is True


async def test_egress_staged_and_collected(tmp_path, migrated_db, db_session, factory):
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    log_path = tmp_path / "squid-access.log"
    known_line = "1660000000.123    5 172.24.0.3 TCP_MISS/200 known test fetch\n"
    noise_line = "1660000001.456    3 10.0.0.9 TCP_MISS/200 unrelated noise\n"
    log_path.write_text(known_line + noise_line, encoding="utf-8")

    placement = make_placement(run_id, run_dir)
    store = tmp_path / "store"

    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(store),
        subnets=["172.24.0.0/29"],
        squid_access_log=str(log_path),
        dns_guard_query_log="",
    )

    assert report is not None
    assert "egress/squid-access.log" in {c.rel_path for c in report.artifacts}

    dest = store / str(run_id) / "artifacts" / "egress" / "squid-access.log"
    content = dest.read_text(encoding="utf-8")
    assert "known test fetch" in content
    assert "unrelated noise" not in content

    rows = (
        (
            await db_session.execute(
                select(Artifact)
                .where(Artifact.run_id == run_id)
                .where(Artifact.path == "egress/squid-access.log")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks/FIFOs are POSIX-only")
async def test_hostile_tree_zero_nonregular_bytes(tmp_path, migrated_db, db_session, factory):
    """Non-regular entries *inside* a source tree. The other half of the same
    threat — the source *root* itself being a link — is
    `test_symlinked_source_root_never_reaches_the_served_store` below.
    """
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "workspace" / ".werft-artifacts"
    artifacts_dir.mkdir(parents=True)

    secret_path = tmp_path / "secret.txt"
    secret_path.write_bytes(b"super-secret-do-not-collect")
    os.symlink(str(secret_path), str(artifacts_dir / "sneaky-link"))

    fifo_path = artifacts_dir / "sneaky-fifo"
    os.mkfifo(str(fifo_path))

    (artifacts_dir / "legit.txt").write_bytes(b"ok")

    placement = make_placement(run_id, run_dir)
    store = tmp_path / "store"

    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(store),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )

    assert report is not None
    assert {c.rel_path for c in report.artifacts} == {"werft-artifacts/legit.txt"}
    dropped_names = {d.rel_path for d in report.dropped}
    assert "werft-artifacts/sneaky-link" in dropped_names
    assert "werft-artifacts/sneaky-fifo" in dropped_names

    dest_root = store / str(run_id) / "artifacts"
    total_bytes = 0
    for dirpath, _dirnames, filenames in os.walk(dest_root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            info = os.lstat(full)
            assert stat.S_ISREG(info.st_mode)
            total_bytes += info.st_size
    assert total_bytes == 2  # only "legit.txt"'s bytes


async def test_failure_is_swallowed(tmp_path, migrated_db, db_session, factory):
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    placement = make_placement(run_id, run_dir)

    # artifacts_root points into a file, not a directory: os.makedirs(dest, ...)
    # inside collect_trees must raise, and the wrapper must swallow it.
    bad_root = tmp_path / "not-a-dir"
    bad_root.write_bytes(b"x")

    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(bad_root),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )

    assert report is None

    rows = (
        (await db_session.execute(select(Artifact).where(Artifact.run_id == run_id)))
        .scalars()
        .all()
    )
    assert rows == []

    # `trg_runs_created` (0001_spine.py) fires a `created` event on every run
    # insert regardless of this module — the assertion is that the swallowed
    # failure never got as far as writing *its own* `dispatch` event, not
    # that the run has no events at all.
    events = (
        (
            await db_session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .where(RunEvent.event_type == "dispatch")
            )
        )
        .scalars()
        .all()
    )
    assert events == []


# -- _event_payload (pure) -----------------------------------------------


def test_event_payload_elides_dropped_past_100():
    dropped = [DroppedEntry(f"outputs/f{i}.bin", "total_cap", 1) for i in range(101)]
    report = CollectionReport(artifacts=[], dropped=dropped, bytes_total=0, truncated=True)

    payload = _event_payload(report)

    assert len(payload["dropped"]) == 100
    assert payload["dropped_elided"] == 1
    assert payload["dropped"][0] == {"path": "outputs/f0.bin", "reason": "total_cap", "bytes": 1}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks are POSIX-only")
async def test_symlinked_source_root_never_reaches_the_served_store(
    tmp_path, migrated_db, db_session, factory
):
    """The end-to-end shape of the escape: the agent owns `workspace/`, so it
    can replace `.werft-artifacts` with a link at the manager's *own* secrets
    directory. Collection runs before `remove_secrets` (and on the sweep path
    revoke may never have happened at all), and the store is served over HTTP —
    so a followed root is a credential leak, not a tidiness bug.
    """
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    placement = make_placement(run_id, run_dir)

    secrets_dir = run_dir / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "git_token").write_bytes(b"ghp_supersecret_do_not_collect")

    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    os.symlink(str(secrets_dir), str(workspace / ".werft-artifacts"))

    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "log.jsonl").write_bytes(b"ab")

    store = tmp_path / "store"

    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=placement,
        artifacts_root=str(store),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )

    assert report is not None
    assert {c.rel_path for c in report.artifacts} == {"outputs/log.jsonl"}
    assert ("werft-artifacts", "not_regular") in [(d.rel_path, d.reason) for d in report.dropped]

    rows = (
        (await db_session.execute(select(Artifact).where(Artifact.run_id == run_id)))
        .scalars()
        .all()
    )
    assert {r.path for r in rows} == {"outputs/log.jsonl"}

    # Not one token byte anywhere under the HTTP-served store.
    for dirpath, _dirnames, filenames in os.walk(store):
        for name in filenames:
            with open(os.path.join(dirpath, name), "rb") as handle:
                assert b"ghp_supersecret" not in handle.read()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not real on Windows")
async def test_store_directories_are_created_0700(tmp_path, migrated_db, db_session, factory):
    project = await seed_project(db_session)
    run_id = await seed_run(db_session, project)

    run_dir = tmp_path / "run"
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "log.jsonl").write_bytes(b"ab")

    store = tmp_path / "store"
    report = await collect_run_evidence(
        factory,
        run_id=run_id,
        placement=make_placement(run_id, run_dir),
        artifacts_root=str(store),
        subnets=[],
        squid_access_log="",
        dns_guard_query_log="",
    )

    assert report is not None
    run_store = store / str(run_id)
    assert run_store.stat().st_mode & 0o777 == 0o700
    assert (run_store / "artifacts").stat().st_mode & 0o777 == 0o700
