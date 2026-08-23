"""The fakes the dispatch plane's tests share.

Docker and GitHub are the two surfaces this milestone cannot exercise in CI, so
three files need the same two fakes: `tests/integration/test_driver.py` (one
attempt, driven directly), `tests/integration/test_sweeps.py` (crash recovery,
no driver at all) and `tests/integration/test_dispatch_e2e.py` (issue #24's
acceptance, driven only through `tick_once`). They live here so those three
cannot drift apart — an e2e that passes against a fake the driver tests no
longer use is an e2e that proves nothing.

`FakeDocker` is deliberately **one** class covering the whole `DockerClient`
surface rather than one per caller: the e2e drives the sweeps and the driver
through the same daemon, so a split fake could not answer both.
"""

import asyncio
import inspect
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from werft.github.auth import InstallationToken
from werft.github.ops import PullRequest
from werft.observe.alerts import NullAlertSink
from werft.runner.create_body import RunPlacement
from werft.runner.docker_api import DieEvent, DockerApiError


class FakeDocker:
    """The whole `DockerClient` surface the manager actually uses.

    Two failure knobs, because the two callers need different shapes.
    `raise_on` names a single operation the *launch* path should fail at
    (`create_container`, `start_container`, ...), which is how a boot failure
    mid-`prepare_and_launch` is reachable. `error` fails every operation and
    `failing_ops` narrows it to the named ones, which is how the mid-reap daemon
    outage (a scan that works, a remove that 500s) is reachable.

    A successful `remove_container` **drops the container from the listing**,
    exactly as a real daemon does — that is what makes the orphan sweep
    naturally idempotent with no marker to lean on.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.containers: list[dict] = []
        self.created_bodies: list[dict] = []
        self.container_id = "c0ffee" + "0" * 58
        self.started = asyncio.Event()
        self.hold_die = False
        self.raise_on: str | None = None
        self.error: Exception | None = None
        self.failing_ops: set[str] | None = None
        self.exit_code = 0
        self.placement: RunPlacement | None = None
        self.fakes: SimpleNamespace | None = None
        self._die = asyncio.Event()
        #: `network inspect` body a test can shape; `None` mimics a 404 (the
        #: network already gone). Defaults to a realistic single-subnet body so
        #: T8's evidence collection has something to stage without every test
        #: having to set it up.
        self.network_body: dict | None = {"IPAM": {"Config": [{"Subnet": "172.30.0.0/24"}]}}

    def release_die(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self._die.set()

    def _fail_if_configured(self, op: str) -> None:
        if self.raise_on == op:
            raise DockerApiError(500, f"fake docker refuses {op}")
        if self.error is not None and (self.failing_ops is None or op in self.failing_ops):
            raise self.error

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        self._fail_if_configured(name)

    async def create_network(self, name: str) -> str:
        self._guard("create_network")
        return "net-" + name

    async def remove_network(self, name: str) -> None:
        self.calls.append(f"remove_network:{name}")
        self._fail_if_configured("remove_network")

    async def inspect_network(self, name: str) -> dict | None:
        self.calls.append(f"inspect_network:{name}")
        self._fail_if_configured("inspect_network")
        return self.network_body

    async def create_container(self, name: str, body: dict) -> str:
        self._guard("create_container")
        self.created_bodies.append(body)
        return self.container_id

    async def start_container(self, container_id: str) -> None:
        self._guard("start_container")
        on_started = getattr(self.fakes, "on_started", None)
        if on_started is not None:
            # An awaitable hook is awaited *here*, before `launch` returns, so a
            # test that has to mutate the row between `start` and the
            # `claimed -> running` CAS is deterministic rather than a race.
            outcome = on_started(self.placement)
            if inspect.isawaitable(outcome):
                await outcome
        if not self.hold_die:
            self._die.set()
        self.started.set()

    async def inspect_container(self, container_id: str) -> dict:
        return {"State": {"ExitCode": self.exit_code}}

    async def list_containers(self, *, all_: bool = True) -> list[dict]:
        self.calls.append("list_containers")
        self._fail_if_configured("list_containers")
        return list(self.containers)

    async def kill_container(self, container_id: str, signal: str = "SIGKILL") -> None:
        self.calls.append(f"kill:{container_id}")
        self._fail_if_configured("kill_container")

    async def remove_container(self, container_id: str, *, force: bool = True) -> None:
        self.calls.append(f"remove_container:{container_id}")
        self._fail_if_configured("remove_container")
        self.containers = [c for c in self.containers if c.get("Id") != container_id]

    async def watch_die_events(self, run_id: str, *, since: int | None = None):
        await self._die.wait()
        yield DieEvent(container_id=self.container_id, exit_code=self.exit_code, run_id=run_id)


class FakeRepoOps:
    """The `RepoOps` surface the dispatch plane uses, stateful about refs.

    `ref_shas` is what makes "pushed" answerable manager-side: the driver
    decides `pushed` by comparing the run branch's head to `base_sha`, so a test
    that wants a pushed attempt puts a different sha in here and one that wants
    an unpushed attempt leaves it empty.
    """

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


def make_origin(repo: Path) -> tuple[str, str]:
    """A real local git repository to clone from, and its head sha.

    The clone is the one piece of the driver's `prepare` that is never faked:
    `clone_workspace` shells out to the real `git`, so the origin has to be a
    real repository with the project's `unattended_branch` on it.
    """
    repo.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
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


def write_outputs(placement: RunPlacement, *, status="success", envelope_result=None) -> None:
    """The `outputs/` tree a finished adapter leaves behind: SPEC §4.3's
    completion contract plus the stream-json transcript the provider spec
    classifies from."""
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
