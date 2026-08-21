"""The attempt driver, end to end against a real Postgres and a real git origin
(SPEC §4.3; plan D1/D6/D7 and decisions 15-17).

Docker and GitHub are faked — those are the two surfaces this milestone cannot
exercise in CI — but nothing else is: the clone is a real `git clone` of a real
local repository, the transitions go through the real trigger-enforced state
machine, and the quota ledger is the real one.
"""

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from werft.config.dispatch import DispatchConfigCache
from werft.config.settings import Settings
from werft.db.models import ProviderAccount, QuotaLedgerEntry, Run, RunAttempt, RunEvent
from werft.domain.runs import run_branch_name
from werft.github.auth import InstallationToken
from werft.github.client import GitHubUnavailable
from werft.github.ops import PullRequest
from werft.observe.alerts import NullAlertSink
from werft.orchestrator.dispatch import claim_next
from werft.orchestrator.driver import DriverDeps, attend_run
from werft.providers.claude import ClaudeSpec
from werft.quota.ledger import LedgerQuota
from werft.runner.docker_api import DieEvent, DockerApiError
from werft.runner.git import GitError
from werft.runner.workspace import create_run_dirs, placement_for

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not on PATH")

DIGEST = "werft-runner-elastic@sha256:" + "d" * 64
CREDENTIAL = "oauth-token-from-file"


# --- fakes -------------------------------------------------------------------


class FakeDocker:
    """The `DockerClient` surface `RunnerLifecycle` actually uses."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created_bodies: list[dict] = []
        self.container_id = "c0ffee" + "0" * 58
        self.started = asyncio.Event()
        self.hold_die = False
        self.raise_on: str | None = None
        self.exit_code = 0
        self.placement = None
        self.fakes: SimpleNamespace | None = None
        self._die = asyncio.Event()

    def release_die(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self._die.set()

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if self.raise_on == name:
            raise DockerApiError(500, f"fake docker refuses {name}")

    async def create_network(self, name: str) -> str:
        self._guard("create_network")
        return "net-" + name

    async def remove_network(self, name: str) -> None:
        self.calls.append(f"remove_network:{name}")

    async def create_container(self, name: str, body: dict) -> str:
        self._guard("create_container")
        self.created_bodies.append(body)
        return self.container_id

    async def start_container(self, container_id: str) -> None:
        self._guard("start_container")
        on_started = getattr(self.fakes, "on_started", None)
        if on_started is not None:
            on_started(self.placement)
        if not self.hold_die:
            self._die.set()
        self.started.set()

    async def inspect_container(self, container_id: str) -> dict:
        return {"State": {"ExitCode": self.exit_code}}

    async def kill_container(self, container_id: str, signal: str = "SIGKILL") -> None:
        self.calls.append(f"kill:{container_id}")

    async def remove_container(self, container_id: str, *, force: bool = True) -> None:
        self.calls.append(f"remove_container:{container_id}")

    async def watch_die_events(self, run_id: str, *, since: int | None = None):
        await self._die.wait()
        yield DieEvent(container_id=self.container_id, exit_code=self.exit_code, run_id=run_id)


class FakeRepoOps:
    def __init__(self) -> None:
        self.ref_shas: dict[str, str] = {}
        self.ref_error: Exception | None = None
        self.ensure_branch_calls: list[tuple[str, str]] = []
        self.force_reset_calls: list[tuple[str, str]] = []
        self.open_pr_calls: list[tuple[str, str]] = []
        self.opened_pr = PullRequest(
            number=77,
            state="open",
            merged=False,
            head_ref="werft/run",
            head_sha="f" * 40,
            base_ref="unattended",
            mergeable=True,
            mergeable_state="clean",
            html_url="https://example.invalid/pr/77",
        )

    async def get_ref_sha(self, branch: str) -> str | None:
        if self.ref_error is not None:
            raise self.ref_error
        return self.ref_shas.get(branch)

    async def ensure_branch(self, branch: str, from_sha: str) -> str:
        self.ensure_branch_calls.append((branch, from_sha))
        return from_sha

    async def force_reset_ref(self, branch: str, sha: str) -> None:
        self.force_reset_calls.append((branch, sha))

    async def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        self.open_pr_calls.append((head, base))
        return self.opened_pr


def token(value: str, *, minutes: int) -> InstallationToken:
    return InstallationToken(token=value, expires_at=datetime.now(UTC) + timedelta(minutes=minutes))


class FakeAuth:
    def __init__(self) -> None:
        self.tokens: list[InstallationToken] = []
        self.mints: list[tuple[str, str]] = []
        self.revoked: list[str] = []
        self._serial = 0

    async def token_for(self, owner, repo, permissions, *, transient=False):
        self.mints.append((owner, repo))
        if self.tokens:
            return self.tokens.pop(0)
        self._serial += 1
        return token(f"ghs_auto_{self._serial}", minutes=60)

    async def revoke(self, value: str) -> bool:
        self.revoked.append(value)
        return True


class SpyAlerts(NullAlertSink):
    def __init__(self) -> None:
        self.run_parked_calls: list[tuple[str, uuid.UUID, str]] = []
        self.quota_exhausted_until_calls: list[tuple[str, datetime]] = []
        self.review_waiting_calls: list[tuple[str, uuid.UUID, str]] = []

    async def run_parked(self, project_slug, run_id, reason):
        self.run_parked_calls.append((project_slug, run_id, reason))

    async def quota_exhausted_until(self, provider, until):
        self.quota_exhausted_until_calls.append((provider, until))

    async def review_waiting(self, project_slug, run_id, pr_url):
        self.review_waiting_calls.append((project_slug, run_id, pr_url))


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def origin(tmp_path) -> tuple[str, str]:
    repo = tmp_path / "origin"
    repo.mkdir()

    def run(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    run("git", "init", "-q", "--initial-branch=main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    run("git", "add", ".")
    run("git", "commit", "-qm", "first")
    run("git", "branch", "unattended")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return Path(repo).as_uri(), head


@pytest.fixture
async def deps_fixture(migrated_db, db_session, tmp_path, monkeypatch, origin):
    from werft.orchestrator import driver as driver_module

    remote_uri, origin_sha = origin
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
    item_id = (
        await db_session.execute(
            text(
                "INSERT INTO backlog_items (project_id, github_issue_number, title, body,"
                " github_updated_at) VALUES (:p, 7, 'make it work', 'the body', now())"
                " RETURNING id"
            ),
            {"p": project_id},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO runs (project_id, backlog_item_id, status, next_attempt_at)"
            " VALUES (:p, :i, 'queued', now() - interval '1 minute')"
        ),
        {"p": project_id, "i": item_id},
    )
    account_label = f"a{tag}"
    await db_session.execute(
        text(
            "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
            " ceiling_seconds) VALUES ('claude', :l, 5, 18000)"
        ),
        {"l": account_label},
    )
    await db_session.commit()

    credential = tmp_path / "claude-credential"
    credential.write_text(CREDENTIAL + "\n", encoding="utf-8")
    config_file = tmp_path / "dispatch.json"
    config_file.write_text(
        json.dumps(
            {
                "projects": {
                    slug: {
                        "image_digest": DIGEST,
                        "model": "claude-sonnet-4-6",
                        "timeout_seconds": 1800,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(
        runs_root=str(tmp_path / "runs"),
        claude_credential_file=str(credential),
        dispatch_config_file=str(config_file),
        lease_seconds=120,
        heartbeat_seconds=1,
        max_concurrent_runs=8,
    )
    config = DispatchConfigCache(str(config_file))
    quota = LedgerQuota(label=account_label, typical_reservation_seconds=1800)

    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        outcome = await claim_next(
            session,
            quota=quota,
            config=config.current(),
            settings=settings,
            alerts=NullAlertSink(),
            now=datetime.now(UTC),
            live_driver_count=0,
        )
    assert outcome.status == "claimed"
    run_id = outcome.run_id

    docker = FakeDocker()
    ops = FakeRepoOps()
    auth = FakeAuth()
    alerts = SpyAlerts()
    fakes = SimpleNamespace(
        docker=docker,
        ops=ops,
        auth=auth,
        alerts=alerts,
        origin_sha=origin_sha,
        git_remote=remote_uri,
        git_error=None,
        on_started=None,
        git=SimpleNamespace(clone_calls=[]),
        factory=factory,
        settings=settings,
        slug=slug,
        account_label=account_label,
    )
    docker.fakes = fakes
    docker.placement = placement_for(
        run_id, runs_root=settings.runs_root, dns_ip=settings.runner_dns_ip
    )

    monkeypatch.setattr(driver_module, "remote_url", lambda **_kwargs: fakes.git_remote)

    real_clone = driver_module.clone_workspace

    async def recording_clone(**kwargs):
        fakes.git.clone_calls.append(kwargs)
        if fakes.git_error is not None:
            raise fakes.git_error
        return await real_clone(**kwargs)

    monkeypatch.setattr(driver_module, "clone_workspace", recording_clone)

    deps = DriverDeps(
        session_factory=factory,
        docker=docker,
        auth=auth,
        ops_for=lambda _project: ops,
        alerts=alerts,
        quota=quota,
        spec=ClaudeSpec(),
        settings=settings,
        config=config,
    )
    try:
        yield deps, run_id, fakes
    finally:
        await engine.dispose()


# --- readers -----------------------------------------------------------------


async def await_running(fakes, run_id, *, timeout: float = 10.0) -> Run:
    """`FakeDocker.started` fires inside `start_container`, which is a couple of
    awaits before the `claimed -> running` CAS commits. Every assertion about
    the running row has to wait for the CAS, not for the start."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        row = await fetch(fakes, run_id)
        if row.status == "running":
            return row
        assert asyncio.get_running_loop().time() < deadline, f"still {row.status}"
        await asyncio.sleep(0.02)


async def fetch(fakes, run_id) -> Run:
    async with fakes.factory() as session:
        return await session.get(Run, run_id)


async def latest_attempt(fakes, run_id) -> RunAttempt:
    async with fakes.factory() as session:
        return (
            await session.execute(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one()


async def ledger_entry(fakes, run_id) -> QuotaLedgerEntry:
    async with fakes.factory() as session:
        return (
            await session.execute(
                select(QuotaLedgerEntry)
                .where(QuotaLedgerEntry.run_id == run_id)
                .order_by(QuotaLedgerEntry.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one()


async def dispatch_phases(fakes, run_id) -> list[str]:
    async with fakes.factory() as session:
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


async def fetch_account(fakes) -> ProviderAccount:
    async with fakes.factory() as session:
        return (
            await session.execute(
                select(ProviderAccount).where(ProviderAccount.label == fakes.account_label)
            )
        ).scalar_one()


def placement_of(fakes, run_id):
    return placement_for(
        run_id, runs_root=fakes.settings.runs_root, dns_ip=fakes.settings.runner_dns_ip
    )


def run_dir_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).run_dir)


def workspace_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).workspace_dir)


def outputs_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).outputs_dir)


def secrets_of(fakes, run_id) -> Path:
    return Path(placement_of(fakes, run_id).secrets_dir)


def network_of(run_id) -> str:
    return f"werft-net-{run_id}"


def write_outputs(placement, *, status="success", envelope_result=None) -> None:
    outputs = Path(placement.outputs_dir)
    outputs.mkdir(parents=True, exist_ok=True)
    ended = datetime.now(UTC)
    outputs.joinpath("result.json").write_text(
        json.dumps(
            {
                "status": status,
                "pushed": False,
                "started_at": (ended - timedelta(minutes=1)).isoformat(),
                "ended_at": ended.isoformat(),
                "duration_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "usage": {"input_tokens": 11, "output_tokens": 3},
        "total_cost_usd": 0.5,
    }
    if envelope_result is not None:
        envelope["result"] = envelope_result
    outputs.joinpath("log.jsonl").write_text(json.dumps(envelope) + "\n", encoding="utf-8")


async def set_running_with_container(fakes, run_id, container_id: str) -> None:
    create_run_dirs(placement_of(fakes, run_id))
    async with fakes.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET status = 'running', version = version + 1, container_id = :c,"
                " base_sha = :s, lease_expires_at = now() + interval '2 minutes'"
                " WHERE id = :r"
            ),
            {"c": container_id, "s": fakes.origin_sha, "r": run_id},
        )


async def cancel_run_in_db(fakes, run_id, *, observed: int) -> None:
    """What the operator cancel route does (T6): close the attempt, true the
    reservation up, CAS to `canceled` — all before this driver notices."""
    async with fakes.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE run_attempts SET ended_at = now(), duration_seconds = :d,"
                " outcome = 'canceled' WHERE run_id = :r AND ended_at IS NULL"
            ),
            {"d": observed, "r": run_id},
        )
        await session.execute(
            text(
                "UPDATE quota_ledger SET actual_wallclock_s = :d"
                " WHERE run_id = :r AND actual_wallclock_s IS NULL"
            ),
            {"d": observed, "r": run_id},
        )
        await session.execute(
            text("UPDATE runs SET status = 'canceled', version = version + 1 WHERE id = :r"),
            {"r": run_id},
        )


# --- prepare + launch --------------------------------------------------------


async def test_a_claimed_run_is_cloned_branch_reset_launched_and_moved_to_running(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.hold_die = True  # stay in `running`

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()

    row = await await_running(fakes, run_id)
    assert row.container_id == fakes.docker.container_id
    assert row.base_sha == fakes.origin_sha
    assert fakes.ops.ensure_branch_calls == [(run_branch_name(run_id), fakes.origin_sha)]
    assert fakes.ops.force_reset_calls == [(run_branch_name(run_id), fakes.origin_sha)]
    assert fakes.docker.created_bodies[0]["Labels"] == {"werft.run_id": str(run_id)}
    assert (workspace_of(fakes, run_id) / "README.md").exists()
    payload = json.loads((run_dir_of(fakes, run_id) / "task.json").read_text(encoding="utf-8"))
    assert payload["argv"][0] == "claude"
    assert "/run/secrets/prompt.md" in " ".join(payload["argv"])
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == CREDENTIAL
    assert sorted(os.listdir(secrets_of(fakes, run_id))) == [
        "askpass.sh",
        "git_token",
        "prompt.md",
        "system_prompt.md",
    ]
    phases = await dispatch_phases(fakes, run_id)
    assert phases[-2:] == ["workspace_ready", "container_started"]

    fakes.docker.release_die(0)
    await task


async def test_re_preparing_a_claimed_run_rebuilds_the_tree(deps_fixture):
    """A re-adopted `claimed` run that inherited the previous attempt's
    `result.json` would be classified from a stale file."""
    driver_deps, run_id, fakes = deps_fixture
    outputs = outputs_of(fakes, run_id)
    outputs.mkdir(parents=True, exist_ok=True)
    stale = outputs / "result.json"
    stale.write_text('{"status":"success"}', encoding="utf-8")

    await attend_run(driver_deps, run_id)  # `on_started` writes nothing this time

    assert not stale.exists()


# --- finalization ------------------------------------------------------------


async def test_a_pushed_success_finalizes_to_awaiting_review_and_trues_up(deps_fixture):
    """Decision 15: `pushed` is decided manager-side. The adapter hard-codes
    `result.json.pushed = False` and never inspects git, so the only honest
    signal is the run branch's head moving off `base_sha`."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_shas[run_branch_name(run_id)] = "f" * 40
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status == "awaiting_review"  # bootstrap project
    assert row.pr_number == fakes.ops.opened_pr.number
    assert row.exit_code == 0
    assert row.result["usage"]["input_tokens"] == 11  # display-only (SPEC §7)
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s is not None
    assert fakes.auth.revoked  # SPEC §4.4
    assert f"remove_network:{network_of(run_id)}" in fakes.docker.calls
    assert (await dispatch_phases(fakes, run_id))[-1] == "container_died"


async def test_a_success_that_pushed_nothing_is_not_a_success(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_shas.clear()
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status in ("queued", "parked")
    assert "without push" in (row.error_message or "")
    assert not fakes.ops.open_pr_calls


async def test_github_unavailable_during_push_detection_defers_instead_of_failing(deps_fixture):
    """Decision 15: leave the run `running` and let the lease and the next
    attend re-drive, rather than declaring "not pushed" and throwing the work
    away."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_error = GitHubUnavailable(503, "down")
    fakes.on_started = lambda placement: write_outputs(placement, status="success")

    await attend_run(driver_deps, run_id)

    assert (await fetch(fakes, run_id)).status == "running"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s is None  # nothing settled


async def test_a_quota_exhausted_classification_blocks_the_account_durably(deps_fixture):
    """D11 + #24 acceptance 3."""
    driver_deps, run_id, fakes = deps_fixture
    reset = datetime.now(UTC) + timedelta(hours=2)
    fakes.on_started = lambda p: write_outputs(
        p,
        status="failure",
        envelope_result=f"Claude usage limit reached, resets {reset.isoformat()}",
    )

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status == "blocked_quota"
    assert row.attempt_count == 0  # quota is budget-exempt
    assert (await latest_attempt(fakes, run_id)).outcome == "quota_exhausted"
    account = await fetch_account(fakes)
    assert account.exhausted_until == reset and account.exhausted_source == "cli"
    assert fakes.alerts.quota_exhausted_until_calls == [("claude", reset)]


async def test_a_limit_message_without_a_reset_still_blocks_and_says_so(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.on_started = lambda p: write_outputs(
        p, status="failure", envelope_result="Claude usage limit reached"
    )

    await attend_run(driver_deps, run_id)

    account = await fetch_account(fakes)
    assert account.exhausted_source == "cli_no_reset"
    assert (
        timedelta(minutes=14) < account.exhausted_until - datetime.now(UTC) < timedelta(minutes=16)
    )


# --- failure paths -----------------------------------------------------------


async def test_a_repo_404_parks_through_two_legal_transitions(deps_fixture, tmp_path):
    """D6: `domain/runs.py::TRANSITIONS` has no `claimed -> parked` edge, so a
    permanent failure is always `claimed -> failed -> parked`."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.ops.ref_shas.clear()
    fakes.git_remote = (tmp_path / "nope").as_uri()  # clone raises PermanentError

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert (row.status, row.parked_reason) == ("parked", "permanent_error")
    assert fakes.alerts.run_parked_calls
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 0  # never started


async def test_a_clone_failure_rides_the_retry_ladder_and_trues_up_zero(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.git_error = GitError(["git", "clone"], 128, "fatal: unable to access")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert row.status == "queued" and row.next_attempt_at > datetime.now(UTC)
    assert (await latest_attempt(fakes, run_id)).outcome == "infra_failure"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 0


async def test_a_launch_failure_releases_the_reservation_on_the_way_out(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.raise_on = "create_container"

    await attend_run(driver_deps, run_id)  # must not raise

    assert (await fetch(fakes, run_id)).status in ("queued", "parked")
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s is not None
    assert f"remove_network:{network_of(run_id)}" in fakes.docker.calls


# --- the ticker --------------------------------------------------------------


async def test_the_heartbeat_extends_the_lease_while_attending(deps_fixture):
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.hold_die = True

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    earlier = await await_running(fakes, run_id)
    await asyncio.sleep(1.4)
    later = await fetch(fakes, run_id)

    assert later.lease_expires_at > earlier.lease_expires_at
    assert later.last_heartbeat_at is not None

    fakes.docker.release_die(0)
    await task


async def test_the_token_is_reminted_by_rename_while_the_run_is_long(deps_fixture):
    """SPEC §4.4: a 90-minute run outlives a one-hour token, and the in-box
    askpass reads the file fresh on every git call."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.auth.tokens = [token("ghs_one", minutes=5), token("ghs_two", minutes=60)]
    fakes.docker.hold_die = True

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    await await_running(fakes, run_id)
    await asyncio.sleep(1.4)

    assert (secrets_of(fakes, run_id) / "git_token").read_text(encoding="utf-8") == "ghs_two"
    assert "token_reminted" in await dispatch_phases(fakes, run_id)

    fakes.docker.release_die(0)
    await task

    assert fakes.auth.revoked == ["ghs_one", "ghs_two"]  # old at re-mint, live at teardown


# --- ownership, adoption, cancellation ---------------------------------------


async def test_a_run_canceled_mid_attempt_is_not_finalized(deps_fixture):
    """D10 + decision 17: the cancel route already closed the attempt and trued
    up quota; the driver must notice and skip, not raise from
    `_current_attempt` and not double-account."""
    driver_deps, run_id, fakes = deps_fixture
    loop = asyncio.get_running_loop()
    pending: list[asyncio.Task] = []
    fakes.on_started = lambda p: pending.append(
        loop.create_task(cancel_run_in_db(fakes, run_id, observed=42))
    )
    fakes.docker.hold_die = True

    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    await await_running(fakes, run_id)
    await asyncio.gather(*pending)
    fakes.docker.release_die(0)
    await task

    assert (await fetch(fakes, run_id)).status == "canceled"
    assert (await ledger_entry(fakes, run_id)).actual_wallclock_s == 42  # the guard held
    assert (await latest_attempt(fakes, run_id)).outcome == "canceled"  # not overwritten
    assert "abandoned" in await dispatch_phases(fakes, run_id)
    assert fakes.auth.revoked  # teardown still ran


async def test_re_adopting_a_running_run_with_a_dead_container_finalizes_it(deps_fixture):
    """Crash-window row 6: no driver task exists, the container already died,
    and a fresh `attend_run` must still land the run."""
    driver_deps, run_id, fakes = deps_fixture
    await set_running_with_container(fakes, run_id, fakes.docker.container_id)
    write_outputs(placement_of(fakes, run_id), status="success")
    fakes.ops.ref_shas[run_branch_name(run_id)] = "f" * 40
    fakes.docker.release_die(0)

    await attend_run(driver_deps, run_id)

    assert (await fetch(fakes, run_id)).status == "awaiting_review"
    assert fakes.git.clone_calls == []  # re-adoption never re-clones a running run


async def test_cancelling_the_driver_task_leaves_the_container_alive(deps_fixture):
    """D1: a manager restart must not kill a 60-minute agent run."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.docker.hold_die = True
    task = asyncio.create_task(attend_run(driver_deps, run_id))
    await fakes.docker.started.wait()
    await await_running(fakes, run_id)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not any(c.startswith("kill:") for c in fakes.docker.calls)
    assert not any(c.startswith("remove_container") for c in fakes.docker.calls)
    assert (await fetch(fakes, run_id)).status == "running"


async def test_teardown_scrubs_the_credential_out_of_task_json(deps_fixture):
    """D7: the retained, T8-backed-up run dir must carry no live credential."""
    driver_deps, run_id, fakes = deps_fixture
    fakes.on_started = lambda p: write_outputs(p, status="success")

    await attend_run(driver_deps, run_id)

    payload = json.loads((run_dir_of(fakes, run_id) / "task.json").read_text(encoding="utf-8"))
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "<redacted>"
    assert payload["argv"][0] == "claude"  # still readable evidence
    assert CREDENTIAL not in (run_dir_of(fakes, run_id) / "task.json").read_text(encoding="utf-8")


async def test_a_run_that_is_no_longer_attendable_is_left_alone(deps_fixture):
    """`attend_run` is entered from a sweep that read the row moments ago; by
    the time it runs the row may have moved. It must not clone, launch or
    finalize anything."""
    driver_deps, run_id, fakes = deps_fixture
    async with fakes.factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET status = 'canceled', version = version + 1 WHERE id = :r"),
            {"r": run_id},
        )

    await attend_run(driver_deps, run_id)

    assert fakes.docker.calls == []
    assert fakes.git.clone_calls == []


async def test_a_project_with_no_dispatch_config_parks_the_run(deps_fixture, tmp_path):
    """The same `PermanentError` the claim path parks on, one phase later — the
    operator can empty the file between the claim and the attend."""
    driver_deps, run_id, fakes = deps_fixture
    Path(fakes.settings.dispatch_config_file).write_text('{"projects": {}}', encoding="utf-8")

    await attend_run(driver_deps, run_id)

    row = await fetch(fakes, run_id)
    assert (row.status, row.parked_reason) == ("parked", "permanent_error")
    assert fakes.git.clone_calls == []
