"""Task 19: the live-Docker dispatch smoke, issue #24's final executed leg.

Skipped where no daemon is reachable at all; runs in CI on ubuntu-24.04
against `/var/run/docker.sock`, same as `test_docker_live.py`. On the Windows
dev box this repo is also developed on, Docker Desktop exposes the daemon
only through a named pipe (`docker context inspect` shows
`npipe:////./pipe/dockerDesktopLinuxEngine`; there is no unix socket file and
no TCP listener) -- so the reachability check below additionally probes that
path via `docker-py` (already a transitive dependency through
`testcontainers`), and the `client` fixture bridges the manager's own
`werft.runner.docker_api.DockerClient` (which only speaks unix-socket or
plain-TCP httpx transports) onto docker-py's already-correct Windows
named-pipe transport for exactly this one test file. This is test-only
plumbing to satisfy this task's "prove it against a real daemon on this
machine" requirement; it changes nothing in `werft/runner/docker_api.py`,
and on Linux CI it is never constructed at all -- the default unix-socket
transport is used there, unmodified.

As in `test_docker_live.py`, the SELinux relabelling this smoke asks for
(`:z` / `:Z` on the bind mounts) is NOT falsifiable off a SELinux-enforcing
host: what is proven here is that the mount wiring is what the manager builds
and that the container reads and writes through it, not that the labels are
enforced. Enforcement needs the Rocky Linux 10 host, and is asserted by
install.sh at T9.
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fakes import FakeAuth, FakeRepoOps, make_origin
from werft.config.dispatch import DispatchConfigCache
from werft.config.settings import Settings
from werft.db.models import Run
from werft.domain.runs import run_branch_name
from werft.observe.alerts import NullAlertSink
from werft.orchestrator import driver as driver_module
from werft.orchestrator.dispatch import claim_next
from werft.orchestrator.driver import DriverDeps, attend_run
from werft.providers.claude import ClaudeSpec
from werft.quota.ledger import LedgerQuota
from werft.runner.docker_api import DockerClient
from werft.runner.workspace import placement_for

DOCKER_SOCKET = "/var/run/docker.sock"

# A tiny, always-present image: the daemon pulls it once in CI.
TEST_IMAGE_REF = "busybox:1.37"


def _win32_daemon_reachable() -> bool:
    """Windows-only fallback probe: is Docker Desktop's named pipe alive?"""
    try:
        import docker as docker_py

        docker_py.from_env().ping()
        return True
    except Exception:
        return False


def _daemon_reachable() -> bool:
    if os.path.exists(DOCKER_SOCKET):
        return True
    return sys.platform == "win32" and _win32_daemon_reachable()


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _daemon_reachable(), reason="no reachable Docker daemon"),
]


class _NpipeBridgeStream(httpx.AsyncByteStream):
    """Bridges a `requests.Response`'s (blocking) `iter_content` onto an
    async byte stream, one thread-hop per chunk. Good enough for a smoke
    test's request volume; not a general-purpose transport."""

    def __init__(self, resp) -> None:
        self._resp = resp
        self._iter = resp.iter_content(chunk_size=65536)

    async def __aiter__(self):
        while True:

            def _next():
                try:
                    return next(self._iter)
                except StopIteration:
                    return None

            chunk = await asyncio.to_thread(_next)
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        await asyncio.to_thread(self._resp.close)


class _NpipeBridgeTransport(httpx.AsyncBaseTransport):
    """Reroutes every request `DockerClient` makes through `docker-py`'s
    `APIClient` (a `requests.Session` subclass with its Windows
    `NpipeHTTPAdapter` already mounted by `docker.from_env()`), rather than
    reimplementing named-pipe HTTP framing. Docker Desktop's daemon speaks
    HTTP-over-named-pipe here; `docker-py` already gets that right and is
    already on this project's dependency graph via `testcontainers`."""

    def __init__(self) -> None:
        import docker as docker_py

        self._docker = docker_py.from_env()
        self._api = self._docker.api
        self._base = self._api.base_url.rstrip("/")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path
        if isinstance(raw_path, (bytes, bytearray)):
            raw_path = raw_path.decode("ascii")
        url = self._base + raw_path
        body = request.content if request.content else None
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

        def do_request():
            return self._api.request(
                request.method, url, data=body, headers=headers, stream=True, timeout=None
            )

        resp = await asyncio.to_thread(do_request)
        return httpx.Response(
            status_code=resp.status_code,
            headers=list(resp.raw.headers.items()),
            stream=_NpipeBridgeStream(resp),
            request=request,
        )

    async def aclose(self) -> None:
        await asyncio.to_thread(self._api.close)


@pytest.fixture
async def client():
    docker = DockerClient()
    if sys.platform == "win32" and not os.path.exists(DOCKER_SOCKET):
        # The real daemon here is only reachable over the named pipe; see
        # module docstring.
        docker._client = httpx.AsyncClient(
            transport=_NpipeBridgeTransport(), base_url="http://docker"
        )
    await docker.negotiate()
    try:
        yield docker
    finally:
        await docker.aclose()


async def pinned_image(client: DockerClient) -> str:
    """Resolve the test image to a digest -- create bodies are digest-only."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "image",
        "inspect",
        TEST_IMAGE_REF,
        "--format",
        "{{index .RepoDigests 0}}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not out.strip():
        pull = await asyncio.create_subprocess_exec(
            "docker", "pull", "--quiet", TEST_IMAGE_REF, stdout=asyncio.subprocess.PIPE
        )
        await pull.communicate()
        return await pinned_image(client)
    return out.decode().strip()


async def test_a_claimed_run_reaches_awaiting_review_against_a_real_daemon(
    tmp_path, migrated_db, client, monkeypatch
):
    """The demo rig in a test: a real daemon, a real create body, a real die
    event, real outputs read back through the `:z` label, real teardown.

    It is **not** a real agent, and cannot be until T9 (behavioral decision 19):
    the per-run network is `Internal: true` with no proxy, so nothing inside the
    box can reach the provider API. The entrypoint is a busybox-style shell
    writing the adapter's own `result.json` contract, which proves every
    manager-side link in the chain -- mounts, labels, die event, exit code,
    outputs read-back, teardown, network removal -- without the runner image.
    The full-agent path is T9's acceptance.

    Two adaptations from the brief's literal snippet, both needed for the run
    to actually land `awaiting_review` rather than retry/park:

    1. The entrypoint also writes `outputs/log.jsonl` with a bare
       `{"type": "result", "subtype": "success", "is_error": false}` envelope
       -- `parse_stream` only treats a line as the result envelope when
       `type == "result"`. `result.json` alone answers "did the box finish";
       `ClaudeSpec.classify` grades
       SUCCESS from the parsed *log stream* envelope
       (`werft/orchestrator/driver.py::_finalize` reads `envelope=parsed.result`
       from `read_log_tail`/`parse_stream`, never from `result.json` itself),
       so a `result.json`-only entrypoint classifies `AGENT_FAILURE` ("no
       result envelope") and never reaches the review queue.
    2. GitHub is faked (`FakeRepoOps`/`FakeAuth` from `tests/fakes.py`,
       Task 18's shared fakes) exactly as `test_driver.py` fakes it: this
       milestone cannot exercise real GitHub in CI, and Task 19's scope is
       proving the *Docker* side end to end. `FakeRepoOps.ref_shas` is set so
       the driver's manager-side "did it push" check (decision 15) reads
       `pushed=True`, which is what actually drives the run to
       `awaiting_review` on a bootstrap project.
    """
    entrypoint = [
        "/bin/sh",
        "-c",
        'printf \'%s\' \'{"status":"success","pushed":false,'
        '"started_at":"2026-08-16T12:00:00+00:00",'
        '"ended_at":"2026-08-16T12:00:01+00:00",'
        '"duration_seconds":1.0,"error":null}\' > /outputs/result.json && '
        'printf \'%s\' \'{"type":"result","subtype":"success","is_error":false}\''
        " > /outputs/log.jsonl",
    ]

    origin_uri, _origin_sha = make_origin(tmp_path / "origin")
    tag = uuid.uuid4().hex[:8]
    slug = f"p{tag}"

    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session, session.begin():
        project_id = (
            await session.execute(
                text(
                    "INSERT INTO projects (slug, github_owner, github_repo, unattended_branch)"
                    " VALUES (:s, 'ken', :r, 'unattended') RETURNING id"
                ),
                {"s": slug, "r": f"repo{tag}"},
            )
        ).scalar_one()
        item_id = (
            await session.execute(
                text(
                    "INSERT INTO backlog_items (project_id, github_issue_number, title, body,"
                    " github_updated_at) VALUES (:p, 7, 'make it work', 'the body', now())"
                    " RETURNING id"
                ),
                {"p": project_id},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status, next_attempt_at)"
                " VALUES (:p, :i, 'queued', now() - interval '1 minute')"
            ),
            {"p": project_id, "i": item_id},
        )
        account_label = f"a{tag}"
        await session.execute(
            text(
                "INSERT INTO provider_accounts (provider, label, rolling_window_hours,"
                " ceiling_seconds) VALUES ('claude', :l, 5, 18000)"
            ),
            {"l": account_label},
        )

    credential = tmp_path / "claude-credential"
    credential.write_text("dummy-oauth-token\n", encoding="utf-8")

    digest = await pinned_image(client)
    config_file = tmp_path / "dispatch.json"
    config_file.write_text(
        json.dumps(
            {
                "projects": {
                    slug: {
                        "image_digest": digest,
                        "model": "claude-sonnet-4-6",
                        "timeout_seconds": 60,
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
        runner_entrypoint=entrypoint,
    )
    config = DispatchConfigCache(str(config_file))
    quota = LedgerQuota(label=account_label, typical_reservation_seconds=1800)

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

    ops = FakeRepoOps()
    ops.ref_shas[run_branch_name(run_id)] = "f" * 40
    auth = FakeAuth()

    monkeypatch.setattr(driver_module, "remote_url", lambda **_kwargs: origin_uri)

    deps = DriverDeps(
        session_factory=factory,
        docker=client,
        auth=auth,
        ops_for=lambda _project: ops,
        alerts=NullAlertSink(),
        quota=quota,
        spec=ClaudeSpec(),
        settings=settings,
        config=config,
    )

    try:
        await attend_run(deps, run_id)

        async with factory() as session:
            run = await session.get(Run, run_id)
        assert run.status == "awaiting_review"

        placement = placement_for(
            run_id, runs_root=settings.runs_root, dns_ip=settings.runner_dns_ip
        )
        assert (Path(placement.run_dir) / "outputs" / "result.json").exists()
        assert placement.network_name not in [n["Name"] for n in await client.list_networks()]
        assert not [
            c
            for c in await client.list_containers()
            if (c.get("Labels") or {}).get("werft.run_id") == str(run_id)
        ]
    finally:
        await engine.dispose()
