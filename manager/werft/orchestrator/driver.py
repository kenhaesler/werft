"""One run's attempt, attended from `claimed` to finalization (SPEC §4.3).

A driver is a per-run asyncio task, not a tick handler — but it holds **no open
DB transaction while it waits** (SPEC §3.3 item 4). Each phase opens its own
short session; `RunnerLifecycle.await_completion`, the one long await, touches
no session at all. Everything a restart would need is already in columns:
`status`, `container_id`, `lease_expires_at`, `hard_deadline_at`, the open
`run_attempts` row and the open `quota_ledger` row. The driver is therefore an
*optimisation over the sweeps*, never the only path — kill -9 the manager at
any await below and `sweeps.py` finishes the job.

`attend_run` is entered from the attend sweep and from nowhere else, which is
what makes crash recovery the normal path: it reads the run's columns and
resumes at whichever phase they describe (`claimed` + no container ⇒ prepare
and launch; `running` + a container ⇒ attend it). There is no separate
recovery entry point to rot.

`pushed` is decided here, manager-side, by comparing the run branch's head to
`base_sha`. The adapter cannot answer it — it hard-codes
`result.json.pushed = False` and never touches git — and doctrine #1 will not
take the agent's own word for whether its work exists.
"""

import asyncio
import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from werft.config.dispatch import DispatchConfigCache, ProjectDispatch, dispatch_for
from werft.config.settings import Settings
from werft.contracts.result import ResultStatus
from werft.contracts.task import TaskSpec
from werft.db.models import BacklogItem, Project, Run, RunAttempt, RunEvent
from werft.db.transitions import transition_run
from werft.domain.attempts import AttemptOutcome
from werft.domain.errors import PermanentError
from werft.domain.runs import ParkedReason, RunStatus, run_branch_name
from werft.github.auth import AppAuth
from werft.github.client import GitHubUnavailable
from werft.github.ops import RepoOps
from werft.observe.alerts import AlertSink
from werft.orchestrator.credentials import RunCredentials
from werft.orchestrator.dispatch import runner_config_for
from werft.orchestrator.finalize import advance_failed, finalize_attempt
from werft.providers.base import Classification, ProviderSpec
from werft.providers.claude import parse_stream
from werft.quota.ledger import LedgerQuota
from werft.runner.create_body import RunPlacement
from werft.runner.docker_api import DockerClient
from werft.runner.git import clone_env, clone_workspace, remote_url, write_askpass
from werft.runner.lifecycle import Completion, RunnerLifecycle, meaning_of, now_epoch_seconds
from werft.runner.outputs import OutputsRead, read_log_tail, read_result
from werft.runner.workspace import (
    PROMPT_FILENAME,
    SYSTEM_PROMPT_FILENAME,
    build_prompt,
    build_system_prompt,
    create_run_dirs,
    credential_values,
    in_box,
    placement_for,
    remove_secrets,
    scrub_task_json,
    write_secret,
    write_task_json,
)

logger = structlog.get_logger(__name__)

#: The statuses `attend_run` will act on at all. Anything else means the row
#: moved between the sweep's discovery query and this task starting.
_ATTENDABLE = (RunStatus.CLAIMED.value, RunStatus.RUNNING.value)

#: SPEC §4.3's exit tiers that no transcript can overrule, and the outcome each
#: one is (decision 16). `cli_unstartable`/`workspace_git_failure` are facts
#: about the *box*; `result_serialization_failure` is a fact about the run —
#: the CLI ran and the adapter could not write the completion contract, which
#: is the agent's failure and must spend the retry budget like one.
_EXIT_TIER_OUTCOMES: dict[str, tuple[AttemptOutcome, ResultStatus]] = {
    "cli_unstartable": (AttemptOutcome.INFRA_FAILURE, ResultStatus.ERROR),
    "workspace_git_failure": (AttemptOutcome.INFRA_FAILURE, ResultStatus.ERROR),
    "result_serialization_failure": (AttemptOutcome.AGENT_FAILURE, ResultStatus.FAILURE),
}


@dataclass(frozen=True)
class DriverDeps:
    """Everything one attempt needs, injected. No global reaches into here —
    `sweeps.py` and `loop.py` build this once and hand the same instance to
    every driver task."""

    session_factory: async_sessionmaker[AsyncSession]
    docker: DockerClient
    auth: AppAuth
    ops_for: Callable[[Project], RepoOps]
    alerts: AlertSink
    quota: LedgerQuota
    spec: ProviderSpec
    settings: Settings
    config: DispatchConfigCache


def classify_completion(
    *,
    spec: ProviderSpec,
    completion: Completion,
    outputs: OutputsRead,
    envelope: dict | None,
    stderr: str,
) -> Classification:
    """Decision 16: a **pure** function of four structured signals.

    Ordered, and the order is the point:

    1. **The manager's ceiling kill wins outright.** A killed run may still
       have written a plausible envelope on its way down; `timed_out` is the
       manager's own observation of its own SIGKILL, and nothing inside the
       box can outrank it.
    2. **The adapter's exit-code contract** (SPEC §4.3). "The CLI would not
       start" and "the workspace was broken" are infrastructure facts; "the
       result could not be serialized" is the agent's. All three are stated by
       a process the agent could not have edited into lying about its own
       `exit()` value without also skipping the work.
    3. **The provider adapter's own classification** — the envelope, and the
       bare-stderr account-level patterns SPEC §5 insists come first there.
    4. **A `SUCCESS` with no valid `result.json` is not a success.** SPEC §4.3
       makes `result.json` the completion contract: a run that did not write
       one did not complete, whatever its exit code claimed.
    """
    if completion.timed_out:
        return Classification(
            AttemptOutcome.TIMEOUT,
            ResultStatus.TIMEOUT,
            f"killed at the manager-enforced run ceiling (exit {completion.exit_code})",
        )

    meaning = meaning_of(completion.exit_code)
    tier = _EXIT_TIER_OUTCOMES.get(meaning)
    if tier is not None:
        outcome, status = tier
        return Classification(outcome, status, f"adapter exit {completion.exit_code}: {meaning}")

    classification = spec.classify(envelope=envelope, stderr=stderr, exit_code=completion.exit_code)
    if classification.status == ResultStatus.SUCCESS and not outputs.is_valid:
        return replace(
            classification,
            outcome=AttemptOutcome.AGENT_FAILURE,
            status=ResultStatus.FAILURE,
            detail=f"result.json {outputs.problem}",
        )
    return classification


@dataclass(frozen=True)
class _Loaded:
    """The run's columns as plain values, plus a **transient** `Project` copy.

    Nothing ORM-bound crosses a session boundary: the `Project` here is
    constructed, never loaded, so it belongs to no session and cannot be
    poisoned by one closing. It exists solely to satisfy `ops_for`'s signature,
    which reads `github_owner`/`github_repo` eagerly. `_finalize` re-reads the
    real row inside its own transaction.
    """

    status: str
    version: int
    container_id: str | None
    base_sha: str | None
    updated_at: datetime
    hard_deadline_at: datetime | None
    provider: str
    project: Project
    issue_number: int
    issue_title: str
    issue_body: str
    issue_labels: list[str]


class _Driver:
    def __init__(self, deps: DriverDeps, run_id: UUID) -> None:
        self._deps = deps
        self._settings = deps.settings
        self._run_id = run_id
        self._placement: RunPlacement | None = None
        self._lifecycle: RunnerLifecycle | None = None
        self._credentials: RunCredentials | None = None
        #: The *physical* container, kept for teardown on every path — including
        #: the paths where this driver no longer owns the run. Losing ownership
        #: is a fact about the row; the container it just started is a fact
        #: about the host, and forgetting the id is how one leaks.
        self._container_id: str | None = None
        #: Whether this driver is still the one accounting for this attempt.
        #: Cleared when the `claimed -> running` CAS is lost (somebody cancelled
        #: while the container was starting): teardown still runs, finalize does
        #: not — the attempt row and the reservation are already somebody else's.
        self._owns_run = True
        #: Set only on decision 15's defer path (`_finalize` could not reach
        #: GitHub) *and* only when the lease renewal there proved the row is
        #: still `claimed`/`running`. The run stays `running` with its container
        #: intact so the next attend sweep re-drives it; tearing the box down
        #: here would leave that re-drive with nothing but a die event that may
        #: have aged out of the daemon's buffer. Cleanup is the orphan sweep's
        #: once the run really leaves `running`. If the row has *already* left
        #: it, there is no re-drive to protect and teardown runs normally.
        self._skip_teardown = False
        self._ticker: asyncio.Task[None] | None = None

    # -- entry point ---------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._drive()
        except asyncio.CancelledError:
            # Shutdown, not failure (plan decision D1). The container outlives
            # this process on purpose; the next boot's attend sweep re-adopts
            # it. Killing a 60-minute agent because the manager restarted is
            # the opposite of "state lives in the database". The teardown below
            # is deliberately *not* reached: it would remove that container.
            logger.info("driver.canceled_for_shutdown", run_id=str(self._run_id))
            raise
        except PermanentError as exc:
            logger.warning("driver.permanent_error", run_id=str(self._run_id), error=str(exc))
            await self._safely(
                self._fail_attempt(
                    detail=str(exc), permanent=True, outcome=AttemptOutcome.INFRA_FAILURE
                )
            )
            await self._teardown()
        except Exception as exc:  # noqa: BLE001 - a driver never propagates
            # A driver that raises leaves a container running and a reservation
            # open, and the only thing that would notice is a sweep two minutes
            # later.
            logger.error("driver.failed", run_id=str(self._run_id), error=str(exc))
            await self._safely(
                self._fail_attempt(
                    detail=str(exc), permanent=False, outcome=AttemptOutcome.INFRA_FAILURE
                )
            )
            await self._teardown()
        else:
            if self._skip_teardown:
                logger.info("driver.teardown_deferred", run_id=str(self._run_id))
                return
            await self._teardown()

    async def _drive(self) -> None:
        loaded = await self._load()
        if loaded is None:
            logger.info("driver.not_attendable", run_id=str(self._run_id))
            return

        entry = dispatch_for(self._deps.config.current(), loaded.project.slug)
        self._placement = placement_for(
            self._run_id,
            runs_root=self._settings.runs_root,
            dns_ip=self._settings.runner_dns_ip,
        )
        self._lifecycle = RunnerLifecycle(
            self._deps.docker,
            ceiling_seconds=self._ceiling_seconds(loaded, entry, now=datetime.now(UTC)),
        )

        if loaded.container_id:
            # Re-adoption. `since` reaches back past the row's last write so a
            # die event that already happened is replayed rather than missed
            # (`_first_die` falls back to `inspect` if it is older still).
            os.makedirs(self._placement.secrets_dir, exist_ok=True)
            await self._mint(loaded)
            self._container_id = loaded.container_id
            base_sha = loaded.base_sha
            since = int(loaded.updated_at.timestamp()) - 1
        elif loaded.status == RunStatus.CLAIMED.value:
            create_run_dirs(self._placement)
            await self._mint(loaded)
            since, base_sha = await self._prepare_and_launch(loaded, entry)
            if not self._owns_run:
                # Lost the CAS: somebody cancelled while the container was
                # starting. Teardown still removes it — `_container_id` is
                # deliberately still set — but nothing here finalizes an
                # attempt somebody else has already accounted for.
                return
        else:
            raise RuntimeError("run is `running` with no container_id")

        completion = await self._attend(since)
        await self._finalize(loaded, completion, base_sha)

    @staticmethod
    def _ceiling_seconds(loaded: _Loaded, entry: ProjectDispatch, *, now: datetime) -> float:
        """The container ceiling this driver will enforce, clamped by the run's
        **absolute** `hard_deadline_at`.

        `RunnerLifecycle` measures its ceiling from the moment *this* driver
        starts attending. On a fresh dispatch that is the same clock the
        deadline was written from (`hard_deadline_at = claim + timeout +
        margin`), so the plain timeout is already inside it. On the re-adoption
        path it is not: a manager restart at minute 89 of a 90-minute run would
        otherwise hand the container a fresh 90 minutes — roughly twice the
        ceiling — and `sweeps.sweep_deadlines` deliberately skips runs a live
        driver holds, so nothing else would cut it short.

        Never below one second: a deadline that is already past still wants an
        attended kill-and-finalize, not a zero-timeout race.
        """
        ceiling = float(entry.timeout_seconds)
        if loaded.hard_deadline_at is None:
            return ceiling
        return max(1.0, min(ceiling, (loaded.hard_deadline_at - now).total_seconds()))

    # -- phases --------------------------------------------------------------

    async def _load(self) -> _Loaded | None:
        """One short session. Returns `None` when the run is not attendable."""
        async with self._deps.session_factory() as session:
            row = (
                await session.execute(
                    select(Run, Project, BacklogItem)
                    .join(Project, Project.id == Run.project_id)
                    .join(BacklogItem, BacklogItem.id == Run.backlog_item_id)
                    .where(Run.id == self._run_id)
                )
            ).first()
            if row is None:
                return None
            run, project, item = row
            if run.status not in _ATTENDABLE:
                return None
            return _Loaded(
                status=run.status,
                version=run.version,
                container_id=run.container_id,
                base_sha=run.base_sha,
                updated_at=run.updated_at,
                hard_deadline_at=run.hard_deadline_at,
                provider=run.provider or self._deps.spec.code,
                project=Project(
                    id=project.id,
                    slug=project.slug,
                    github_owner=project.github_owner,
                    github_repo=project.github_repo,
                    unattended_branch=project.unattended_branch,
                    lifecycle=project.lifecycle,
                ),
                issue_number=item.github_issue_number,
                issue_title=item.title,
                issue_body=item.body,
                issue_labels=list(item.labels or []),
            )

    async def _mint(self, loaded: _Loaded) -> None:
        self._credentials = RunCredentials(
            self._deps.auth,
            placement=self._placement,
            owner=loaded.project.github_owner,
            repo=loaded.project.github_repo,
            remint_margin=timedelta(seconds=self._settings.token_remint_margin_seconds),
        )
        await self._credentials.mint()

    async def _prepare_and_launch(self, loaded: _Loaded, entry: ProjectDispatch) -> tuple[int, str]:
        placement = self._placement
        branch = run_branch_name(self._run_id)
        askpass = write_askpass(placement.secrets_dir)
        remote = remote_url(
            github_web_url=self._settings.github_web_url,
            owner=loaded.project.github_owner,
            repo=loaded.project.github_repo,
        )
        clone = await clone_workspace(
            remote=remote,
            base_branch=loaded.project.unattended_branch,
            dest=placement.workspace_dir,
            run_branch=branch,
            env=clone_env(askpass_path=askpass),
            timeout_seconds=self._settings.clone_timeout_seconds,
        )
        base_sha = clone.base_sha

        # Every attempt, not just the first: SPEC §3.2's only dispatch behavior
        # is `retry` — "force-reset branch to base" — so a re-attempt must not
        # inherit the previous attempt's commits on the remote either.
        ops = self._deps.ops_for(loaded.project)
        await ops.ensure_branch(branch, from_sha=base_sha)
        await ops.force_reset_ref(branch, base_sha)

        task = TaskSpec(
            run_id=str(self._run_id),
            project_slug=loaded.project.slug,
            provider=loaded.provider,
            repo_remote=remote,
            base_branch=loaded.project.unattended_branch,
            base_sha=base_sha,
            target_branch=branch,
            issue_number=loaded.issue_number,
            issue_title=loaded.issue_title,
            issue_body=loaded.issue_body,
            issue_labels=loaded.issue_labels,
            model=entry.model,
            timeout_seconds=entry.timeout_seconds,
        )
        argv = self._deps.spec.build_argv(
            task,
            prompt_file=in_box(PROMPT_FILENAME),
            system_prompt_file=in_box(SYSTEM_PROMPT_FILENAME),
        )
        env = self._deps.spec.build_env(task, credential_path=self._settings.claude_credential_file)
        task = task.model_copy(update={"argv": argv, "env": env})
        write_secret(placement, PROMPT_FILENAME, build_prompt(task))
        write_secret(placement, SYSTEM_PROMPT_FILENAME, build_system_prompt(task))
        write_task_json(placement, task)
        await self._event("workspace_ready", {"base_sha": base_sha, "branch": branch})

        # Before creation, never after: a container that exits between `start`
        # and the events stream being established emits its die event into the
        # gap (`DockerClient.watch_die_events`).
        since = now_epoch_seconds()
        await self._lifecycle.prepare(placement)
        container_id = await self._lifecycle.launch(
            placement,
            runner_config_for(entry),
            entrypoint=self._settings.runner_entrypoint,
        )
        self._container_id = container_id

        now = datetime.now(UTC)
        async with self._deps.session_factory() as session, session.begin():
            ok = await transition_run(
                session,
                run_id=self._run_id,
                expected_version=loaded.version,
                new_status=RunStatus.RUNNING,
                extra={
                    "container_id": container_id,
                    "base_sha": base_sha,
                    "lease_expires_at": now + timedelta(seconds=self._settings.lease_seconds),
                    "last_heartbeat_at": now,
                },
            )
            if not ok:
                # Somebody moved this row while the container was starting —
                # in practice an operator cancel. The caller tears down, and
                # `_container_id` stays set so that teardown actually removes
                # the container this driver just started: the row's own
                # `container_id` is still NULL, so nothing id-based would ever
                # find it again.
                logger.info("driver.launch_cas_lost", run_id=str(self._run_id))
                self._owns_run = False
                return since, base_sha
            session.add(
                RunEvent(
                    run_id=self._run_id,
                    event_type="dispatch",
                    payload={
                        "phase": "container_started",
                        "container_id": container_id,
                        "image": entry.image_digest,
                    },
                )
            )
        return since, base_sha

    async def _attend(self, since: int) -> Completion:
        """The one long await. It holds no session — the ticker beside it opens
        its own, one short transaction per beat."""
        self._ticker = asyncio.create_task(self._tick_loop())
        try:
            return await self._lifecycle.await_completion(
                self._placement, self._container_id, since=since
            )
        finally:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker

    async def _tick_loop(self) -> None:
        interval = max(0.1, float(self._settings.heartbeat_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                owns = await self._tick_once()
            except Exception as exc:  # noqa: BLE001 - a stalled beat is not fatal
                logger.warning("driver.heartbeat_failed", run_id=str(self._run_id), error=str(exc))
                continue
            if not owns:
                # D10 gives the container to the tick's canceled-container
                # sweep, so exactly one component owns Docker for a cancel.
                # This just stops renewing and lets `_finalize` discover it.
                logger.info("driver.ownership_lost", run_id=str(self._run_id))
                return

    async def _tick_once(self) -> bool:
        """Returns whether this driver still owns the run.

        The heartbeat is not merely liveness: its guarded UPDATE is also how
        this driver learns it no longer owns the run. An operator cancel (or a
        sweep that reaped us) leaves the row in a status the guard excludes,
        the UPDATE matches zero rows, and we abandon rather than finalize an
        attempt somebody else already accounted for.
        """
        now = datetime.now(UTC)
        if not await self._renew_lease(now=now):
            return False
        if self._credentials is not None and await self._credentials.refresh_if_due(now=now):
            await self._event("token_reminted", {})
        return True

    async def _renew_lease(self, *, now: datetime) -> bool:
        """Push `lease_expires_at` out by one full lease. Returns whether the row
        was still ours to renew.

        An ordinary guarded UPDATE, never a CAS: a lease renewal asserts nothing
        about the run's status and must not bump `version` out from under a
        transition somebody else is composing. The `status IN (claimed,
        running)` guard is the whole arbitration — a row an operator cancel or a
        sweep already moved matches zero rows, and the caller learns it no
        longer owns the run.
        """
        async with self._deps.session_factory() as session, session.begin():
            result = await session.execute(
                update(Run)
                .where(
                    Run.id == self._run_id,
                    Run.status.in_((RunStatus.CLAIMED.value, RunStatus.RUNNING.value)),
                )
                .values(
                    last_heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=self._settings.lease_seconds),
                )
            )
        return bool(result.rowcount)

    async def _finalize(
        self, loaded: _Loaded, completion: Completion, base_sha: str | None
    ) -> None:
        outputs = read_result(self._placement.outputs_dir)
        parsed = parse_stream(
            read_log_tail(self._placement.outputs_dir, max_bytes=self._settings.log_tail_bytes)
        )
        stderr = outputs.result.error.message if outputs.result and outputs.result.error else ""
        classification = classify_completion(
            spec=self._deps.spec,
            completion=completion,
            outputs=outputs,
            envelope=parsed.result,
            stderr=stderr,
        )
        usage = self._deps.spec.read_usage(parsed.result)

        branch = run_branch_name(self._run_id)
        ops = self._deps.ops_for(loaded.project)
        try:
            head = await ops.get_ref_sha(branch)
        except GitHubUnavailable as exc:
            # Decision 15: not a verdict. Leave the run `running`; the lease and
            # the next attend sweep re-drive it. Declaring "not pushed" here
            # would throw away work that may well be on GitHub already.
            #
            # And leave the *box* alone with it: teardown would remove the
            # container and network and revoke the git token while the row still
            # says `running`, so the re-drive would inherit a run it can only
            # reconstruct from a die event that may have aged out. "Re-drive"
            # has to mean the whole attempt is still there to re-drive.
            #
            # And renew the lease on the way out, because this is the last
            # write this attempt makes: the heartbeat ticker was cancelled when
            # `_attend` returned, and `run()` is about to complete, which drops
            # this run from `Orchestrator._drivers`. Without this the row would
            # sit `running` with a lease frozen at container death and no
            # registry entry — and `sweep_leases` would close the attempt as an
            # `infra_failure` roughly a lease later, whose retry
            # `force_reset_ref`s the branch and discards whatever the agent had
            # already pushed. The arbitration invariant is "sweep a row only
            # when it has neither a live driver NOR a valid lease": each
            # re-drive tick buys another `lease_seconds`, so the row becomes
            # sweepable exactly when the re-drives themselves stop — a manager
            # death, which is when the sweep *should* claim it. The re-adoption
            # path needs no separate renewal: a re-drive that cannot reach
            # GitHub either lands right back here.
            # The renewal is also the test of whether there is anything left to
            # defer *for*. Its guard is `status IN (claimed, running)`, so a
            # `False` means the row has definitively left those statuses — a
            # sweep or an operator cancel already settled this attempt — and
            # nothing will ever re-drive it. Deferring then would only hand the
            # container, network and token to the orphan sweep minutes later;
            # falling through to the normal teardown cleans them up now.
            renewed = await self._renew_lease(now=datetime.now(UTC))
            self._skip_teardown = renewed
            logger.warning(
                "driver.push_check_unavailable",
                run_id=str(self._run_id),
                error=str(exc),
                lease_renewed=renewed,
            )
            return
        pushed = head is not None and head != base_sha

        async with self._deps.session_factory() as session, session.begin():
            # Decision 17: re-assert ownership inside the finalize transaction.
            # Between the die event and here, a cancel route may have closed
            # this attempt and trued its reservation up already.
            run = (
                await session.execute(select(Run).where(Run.id == self._run_id).with_for_update())
            ).scalar_one_or_none()
            if run is None:
                return
            session.add(
                RunEvent(
                    run_id=self._run_id,
                    event_type="dispatch",
                    payload={
                        "phase": "container_died",
                        "exit_code": completion.exit_code,
                        "timed_out": completion.timed_out,
                        "meaning": completion.meaning,
                        "outcome": classification.outcome.value if classification.outcome else None,
                        "pushed": pushed,
                    },
                )
            )
            if run.status != RunStatus.RUNNING.value:
                session.add(
                    RunEvent(
                        run_id=self._run_id,
                        event_type="dispatch",
                        payload={"phase": "abandoned", "status": run.status},
                    )
                )
                logger.info("driver.abandoned", run_id=str(self._run_id), status=run.status)
                return

            await session.execute(
                update(Run)
                .where(Run.id == self._run_id)
                .values(
                    exit_code=completion.exit_code,
                    result={
                        "result_json": outputs.result.model_dump(mode="json")
                        if outputs.result
                        else None,
                        "problem": outputs.problem,
                        # SPEC §7: display-only. Nothing here is an admission input.
                        "usage": usage.model_dump() if usage else None,
                        "exit_meaning": completion.meaning,
                    },
                )
            )
            fresh = await session.get(Run, self._run_id, populate_existing=True)
            project = await session.get(Project, fresh.project_id)
            await finalize_attempt(
                session,
                ops,
                fresh,
                project,
                classification=classification,
                pushed=pushed,
                quota=self._deps.quota,
                alerts=self._deps.alerts,
            )

    # -- failure + teardown ---------------------------------------------------

    async def _fail_attempt(self, *, detail: str, permanent: bool, outcome: AttemptOutcome) -> None:
        """One transaction: close the attempt, return the reservation, and move
        the row off `claimed`/`running`.

        A permanent failure parks in **two** CAS calls, because SPEC §3.2 has no
        `claimed -> parked` edge at all: `claimed -> failed`, then
        `failed -> parked`. Everything else rides `advance_failed`'s ladder.
        """
        async with self._deps.session_factory() as session, session.begin():
            run = (
                await session.execute(select(Run).where(Run.id == self._run_id).with_for_update())
            ).scalar_one_or_none()
            if run is None or run.status not in _ATTENDABLE:
                return

            observed = await self._observed_seconds(session, run)
            attempt = (
                await session.execute(
                    select(RunAttempt)
                    .where(RunAttempt.run_id == self._run_id, RunAttempt.ended_at.is_(None))
                    .order_by(RunAttempt.attempt_no.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if attempt is not None:
                await session.execute(
                    update(RunAttempt)
                    .where(RunAttempt.id == attempt.id)
                    .values(
                        outcome=outcome.value,
                        ended_at=datetime.now(UTC),
                        duration_seconds=observed,
                    )
                )
            await self._deps.quota.release(session, run, observed)

            ok = await transition_run(
                session,
                run_id=self._run_id,
                expected_version=run.version,
                new_status=RunStatus.FAILED,
                extra={"error_message": detail},
            )
            if not ok:
                logger.info("driver.fail_cas_lost", run_id=str(self._run_id))
                return
            failed = await session.get(Run, self._run_id, populate_existing=True)
            project = await session.get(Project, failed.project_id)

            if permanent:
                parked = await transition_run(
                    session,
                    run_id=self._run_id,
                    expected_version=failed.version,
                    new_status=RunStatus.PARKED,
                    extra={
                        "parked_reason": ParkedReason.PERMANENT_ERROR.value,
                        "error_message": detail,
                    },
                )
                if parked:
                    await self._deps.alerts.run_parked(
                        project.slug, self._run_id, ParkedReason.PERMANENT_ERROR.value
                    )
                return

            await advance_failed(
                session,
                failed,
                outcome=outcome,
                exhausted_until=None,
                quota=self._deps.quota,
                alerts=self._deps.alerts,
            )
            advanced = await session.get(Run, self._run_id, populate_existing=True)
            if advanced.status == RunStatus.PARKED.value:
                await self._deps.alerts.run_parked(
                    project.slug, self._run_id, advanced.parked_reason or "unknown"
                )

    async def _observed_seconds(self, session: AsyncSession, run: Run) -> int:
        """Wall clock the provider CLI actually consumed. **Zero whenever the
        container never started** — a CLI that never ran consumed nothing, and
        billing the window for it would over-report headroom loss."""
        if run.container_id is None:
            return 0
        started = (
            await session.execute(
                select(RunAttempt.started_at)
                .where(RunAttempt.run_id == self._run_id, RunAttempt.ended_at.is_(None))
                .order_by(RunAttempt.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if started is None:
            return 0
        return max(0, int((datetime.now(UTC) - started).total_seconds()))

    async def _teardown(self) -> None:
        """Best effort, and never raises: it runs while other failures may
        already be in flight. **The run directory is never deleted** — T8's
        collector reads `outputs/` and `workspace/` afterwards."""
        if self._placement is None:
            return
        if self._lifecycle is not None:
            try:
                await self._lifecycle.teardown(self._placement, self._container_id)
            except Exception as exc:  # noqa: BLE001 - a daemon outage is the orphan sweep's job
                logger.warning("driver.teardown_failed", run_id=str(self._run_id), error=str(exc))
        # The git token comes from this driver's own credential object; the
        # provider credential is read back out of `task.json` (see
        # `workspace.credential_values`), so the scrub is identical on the
        # launch path and on the re-adoption path that never built that file.
        secrets = [
            value
            for value in (
                self._credentials.token if self._credentials else None,
                *credential_values(self._placement),
            )
            if value
        ]
        scrub_task_json(self._placement, secrets=secrets)
        if self._credentials is not None:
            await self._credentials.revoke()
        remove_secrets(self._placement)

    # -- helpers --------------------------------------------------------------

    async def _event(self, phase: str, extra: dict[str, Any]) -> None:
        async with self._deps.session_factory() as session, session.begin():
            session.add(
                RunEvent(
                    run_id=self._run_id,
                    event_type="dispatch",
                    payload={"phase": phase, **extra},
                )
            )

    @staticmethod
    async def _safely(coroutine) -> None:
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the recovery path is best effort too
            logger.error("driver.fail_attempt_failed", error=str(exc))


async def attend_run(deps: DriverDeps, run_id: UUID) -> None:
    """Attend one run's whole attempt. **Never raises** except
    `asyncio.CancelledError` (plan decision D1: cancellation is a manager
    shutdown, and the container is meant to outlive it)."""
    try:
        await _Driver(deps, run_id).run()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - belt and braces around `run`'s own guard
        logger.error("driver.unhandled", run_id=str(run_id), error=str(exc))
