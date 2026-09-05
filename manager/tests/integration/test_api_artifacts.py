"""`/api/v1/runs/{id}/artifacts/{path}` — artifact file serving (Task 13/B4,
the stored-XSS surface SPEC §8 calls out): the `artifacts` DB row is the
index (an unindexed path 404s before any filesystem touch), the resolved
file location is re-checked with `os.path.realpath` containment, a symlink
planted at the resolved location 404s rather than being followed, and every
response carries `Content-Type: application/octet-stream` +
`X-Content-Type-Options: nosniff` + a sanitized `Content-Disposition:
attachment` — so a browser can never be tricked into rendering a collected
artifact inline.

Seeding follows `test_api_runs.py`/`test_api_mutations.py`'s style: this
file duplicates the handful of seed helpers it needs rather than importing
them across test modules, the established convention in this package.

Platform notes (this suite runs on both the Windows dev host and Linux CI):

- httpx normalizes `..` dot-segments out of a request URL client-side
  (RFC 3986 `remove_dot_segments`) before ever sending it — a plain
  `.../artifacts/../../etc/passwd` request would collapse to
  `.../runs/etc/passwd`, silently eating the `artifacts` segment and never
  reaching the route at all. `test_traversal_path_row_404s_via_containment`
  percent-encodes the dot-segments instead (`%2e%2e%2f`), which survives
  client-side normalization and is decoded back to the literal `..` by the
  ASGI layer — the same way a real attacker's request arrives over the
  wire, and verified empirically against this exact app/transport pair
  before writing the assertion.
- unprivileged Windows processes often cannot create symlinks —
  `test_symlink_at_artifact_path_is_404` attempts `os.symlink` and only
  skips if creation itself raises `OSError`, so it still runs wherever the
  platform actually supports it (CI is Linux).
- the hostile-filename acceptance payload (`x"><script>alert(1)</script>
  .html`) contains `"` and `<`, characters the Win32 file APIs refuse
  outright in any path component — verified empirically on this host
  (`OSError(22, 'Invalid argument')` for a bare file, `'The filename,
  directory name, or volume label syntax is incorrect'` for a directory).
  That's a hard filesystem constraint, not a permissions gap, so there is
  no way to physically create a file with this literal name on Windows.
  `test_hostile_filename_ascii_fallback_strips_quotes_and_angle_brackets`
  therefore calls `_content_disposition_header` directly with the exact
  verbatim payload — the same function the route calls, zero divergence —
  while `test_happy_path_bytes_round_trip_and_headers` already proves that
  function's output is wired into a real response's headers end to end.
"""

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from werft.api.routes import _artifact_containment_ok, _content_disposition_header, get_session
from werft.app import create_app
from werft.config.settings import Settings
from werft.db.models import Artifact, BacklogItem, Project, Run

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


async def seed_run(session, project: Project, item: BacklogItem, *, status: str = "queued") -> Run:
    rid = (
        await session.execute(
            text(
                "INSERT INTO runs (project_id, backlog_item_id, status) "
                "VALUES (:p, :b, :s) RETURNING id"
            ),
            {"p": project.id, "b": item.id, "s": status},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Run, rid)


async def seed_artifact(session, run: Run, *, path: str, size: int = 4) -> Artifact:
    aid = (
        await session.execute(
            text("INSERT INTO artifacts (run_id, path, bytes) VALUES (:r, :p, :b) RETURNING id"),
            {"r": run.id, "p": path, "b": size},
        )
    ).scalar_one()
    await session.commit()
    return await session.get(Artifact, aid)


def artifact_file_path(artifacts_root: str, run: Run, path: str) -> Path:
    return Path(artifacts_root) / str(run.id) / "artifacts" / path


# -- app wiring -----------------------------------------------------------

TOKEN = "s3cr3t-token"


def make_client_app(db_session: AsyncSession, *, token_file: str, artifacts_root: str):
    app = create_app(Settings(api_token_file=token_file, artifacts_root=artifacts_root))

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


@pytest.fixture
def artifacts_root(tmp_path) -> str:
    root = tmp_path / "artifacts-root"
    root.mkdir()
    return str(root)


# -- happy path -------------------------------------------------------------


async def test_happy_path_bytes_round_trip_and_headers(
    db_session, token_file, auth_headers, artifacts_root
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="log.jsonl")

    content = b'{"event": "hello"}\n'
    file_path = artifact_file_path(artifacts_root, run, "log.jsonl")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(content)

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}/artifacts/log.jsonl", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    disposition = resp.headers["content-disposition"]
    assert disposition == "attachment; filename=\"log.jsonl\"; filename*=UTF-8''log.jsonl"


async def test_empty_artifact_full_download_is_a_valid_empty_stream(
    db_session, token_file, auth_headers, artifacts_root
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="empty.log", size=0)
    file_path = artifact_file_path(artifacts_root, run, "empty.log")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"")

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}/artifacts/empty.log", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == "0"
    assert resp.headers["accept-ranges"] == "bytes"


# -- DB row present, file missing --------------------------------------------


async def test_db_row_present_file_missing_is_404(
    db_session, token_file, auth_headers, artifacts_root
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="missing.log")
    # Deliberately: no file written to disk for this row.

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/runs/{run.id}/artifacts/missing.log", headers=auth_headers
        )

    assert resp.status_code == 404


# -- file vanishes between the lstat check and the open ----------------------


async def test_read_failure_between_lstat_and_read_is_404(
    db_session, token_file, auth_headers, artifacts_root, monkeypatch
) -> None:
    """A file removed or made unreadable in the narrow window between the
    route's `os.lstat` check and its safe file-descriptor open must still 404,
    like every other miss on this route — never surface an unhandled
    `OSError` as an unrelated 500. Monkeypatches `os.open` to
    simulate that race; a portable real-filesystem reproduction of a
    mid-request unlink isn't available across the platforms this suite
    runs on."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="log.jsonl")

    file_path = artifact_file_path(artifacts_root, run, "log.jsonl")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"data")

    def _raise_oserror(path, flags) -> int:
        raise OSError("simulated race: file vanished after lstat")

    monkeypatch.setattr(os, "open", _raise_oserror)

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}/artifacts/log.jsonl", headers=auth_headers)

    assert resp.status_code == 404


# -- bounded single-range reads ----------------------------------------------


async def test_artifact_download_honors_single_byte_range(
    db_session, token_file, auth_headers, artifacts_root
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    content = b"0123456789"
    await seed_artifact(db_session, run, path="large.log", size=len(content))
    file_path = artifact_file_path(artifacts_root, run, "large.log")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(content)

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/runs/{run.id}/artifacts/large.log",
            headers=auth_headers | {"Range": "bytes=2-5"},
        )

    assert resp.status_code == 206
    assert resp.content == b"2345"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-range"] == "bytes 2-5/10"
    assert resp.headers["content-length"] == "4"


@pytest.mark.parametrize("range_header", ["bytes=20-30", "bytes=5-2", "bytes=0-1,4-5", "items=0-1"])
async def test_artifact_download_rejects_invalid_or_unsatisfiable_ranges(
    db_session, token_file, auth_headers, artifacts_root, range_header
) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    content = b"0123456789"
    await seed_artifact(db_session, run, path="log.jsonl", size=len(content))
    file_path = artifact_file_path(artifacts_root, run, "log.jsonl")
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(content)

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/runs/{run.id}/artifacts/log.jsonl",
            headers=auth_headers | {"Range": range_header},
        )

    assert resp.status_code == 416
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-range"] == "bytes */10"


# -- traversal path as a DB row itself ---------------------------------------


async def test_traversal_path_row_404s_via_containment(
    db_session, token_file, auth_headers, artifacts_root
) -> None:
    """The artifact row's own `path` is `../../secret.txt` — inserted
    directly, bypassing whatever the (currently nonexistent) collector
    would normally validate, exactly as the brief specifies: "a
    `../../etc/passwd`-style path AS A DB ROW". The route's containment
    re-check (`os.path.realpath` against the run's own `artifacts/`
    directory), not any DB-side validation, is what must catch this.

    Mutation-sensitive by construction: `secret.txt` is a *real* file
    planted just outside `base_dir` (inside `artifacts_root`), and
    `base_dir` itself is created on disk. Path resolution for
    `<base_dir>/../../secret.txt` requires the OS to walk into `artifacts/`
    before backing out of it, so if `base_dir` didn't exist the
    subsequent `os.lstat` would 404 on ENOENT regardless of whether
    containment ran — proving nothing about the containment check. With
    `base_dir` present and `secret.txt` real, the *only* thing standing
    between this request and a served 200 with `secret.txt`'s bytes is
    `_artifact_containment_ok`; deleting that check makes this test fail
    (see the fix report's mutation-testing evidence).
    """
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="../../secret.txt")

    secret = Path(artifacts_root) / "secret.txt"  # outside base_dir, inside artifacts_root
    secret.write_bytes(b"TOP SECRET")
    base_dir = artifact_file_path(artifacts_root, run, "")
    base_dir.mkdir(parents=True)

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # See module docstring: percent-encoded so httpx's client-side URL
        # normalization doesn't collapse the dot-segments before sending.
        resp = await client.get(
            f"/api/v1/runs/{run.id}/artifacts/%2e%2e%2f%2e%2e%2fsecret.txt",
            headers=auth_headers,
        )

    assert resp.status_code == 404


def test_artifact_containment_ok_rejects_escapes_and_allows_nested(tmp_path) -> None:
    """Direct unit coverage of `_artifact_containment_ok` (SPEC §8's
    containment control) — the mutation-sensitive integration test above
    only pins one escape shape end to end; this pins the function itself
    against every shape the implementer's self-review claimed to have
    checked by hand."""
    base_dir = tmp_path / "run-id" / "artifacts"
    base_dir.mkdir(parents=True)

    assert _artifact_containment_ok(base_dir, base_dir / "../../etc/passwd") is False
    assert _artifact_containment_ok(base_dir, base_dir / "/etc/passwd") is False
    assert _artifact_containment_ok(base_dir, base_dir / "normal/nested/file.txt") is True

    if os.name == "nt":
        # Windows drive-absolute escape only means anything on a platform
        # where `\` is a path separator and `C:` is a recognized drive —
        # `PurePosixPath.__truediv__` treats the same literal string as one
        # (odd but genuinely contained) relative filename, so asserting
        # rejection here would fail on Linux CI for the wrong reason.
        assert (
            _artifact_containment_ok(base_dir, base_dir / "C:\\Windows\\System32\\config\\SAM")
            is False
        )


# -- symlink at the resolved artifact path -----------------------------------


async def test_symlink_at_artifact_path_is_404(
    db_session, token_file, auth_headers, artifacts_root
) -> None:
    """The symlink's target lives *inside* the run's own `artifacts/`
    directory — deliberately, so the realpath containment re-check alone
    cannot reject this request (nothing here escapes the tree). The only
    thing standing between this request and a served (wrong) file is the
    route's separate `os.lstat` check on the artifact path itself, which
    must report the entry as a symlink, not a regular file, regardless of
    where it points. A target placed *outside* the tree would also 404,
    but via containment instead — proving nothing about the lstat check
    this scenario exists to pin down."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="evil-link.log")

    real_target = artifact_file_path(artifacts_root, run, "real-target.log")
    real_target.parent.mkdir(parents=True)
    real_target.write_text("not meant to be served as evil-link.log")
    link_path = artifact_file_path(artifacts_root, run, "evil-link.log")
    try:
        os.symlink(real_target, link_path)
    except OSError:
        pytest.skip(reason="platform cannot create symlinks")

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/runs/{run.id}/artifacts/evil-link.log", headers=auth_headers
        )

    assert resp.status_code == 404


# -- hostile filename: script tag renders inert (server half) ---------------


def test_hostile_filename_ascii_fallback_strips_quotes_and_angle_brackets() -> None:
    header = _content_disposition_header('x"><script>alert(1)</script>.html')

    assert header.startswith("attachment;")
    assert header.count('"') == 2  # exactly the ascii-fallback's two delimiters

    first_quote = header.index('"')
    second_quote = header.index('"', first_quote + 1)
    ascii_fallback = header[first_quote + 1 : second_quote]
    assert '"' not in ascii_fallback
    assert "<" not in ascii_fallback


def test_ascii_fallback_strips_control_characters_and_backslash() -> None:
    """Not one of the brief's five listed scenarios, but the requirement
    text is explicit that control characters must be stripped "so a
    hostile filename cannot break out of the header" — CR/LF above all,
    since an unstripped pair would split the header in two (classic HTTP
    response header injection)."""
    header = _content_disposition_header("evil\r\nX-Injected: true\\.txt")
    assert "\r" not in header
    assert "\n" not in header


async def test_hostile_filename_full_response_is_octet_stream_with_nosniff(
    db_session, token_file, auth_headers, artifacts_root
) -> None:
    """A real request/response round trip for a filename that's hostile in
    a different, Windows-legal way (embeds an HTML entity look-alike and
    non-ASCII), proving `_content_disposition_header`'s output really does
    reach a live response's headers next to a fixed
    `application/octet-stream` content-type that is never derived from the
    filename. Combined with the two unit tests above (exact verbatim
    payload) and the happy-path test (verbatim header format), this covers
    the acceptance criterion end to end on every platform."""
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    hostile_name = "café-&amp;-report.html"
    await seed_artifact(db_session, run, path=hostile_name)

    file_path = artifact_file_path(artifacts_root, run, hostile_name)
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"<html></html>")

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/runs/{run.id}/artifacts/{hostile_name}", headers=auth_headers
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # Literal expected string, not `_content_disposition_header(hostile_name)` —
    # comparing against the same function that produced the header would pass
    # even if the sanitizer regressed, since both sides would change together.
    assert (
        resp.headers["content-disposition"] == 'attachment; filename="caf-&amp;-report.html"; '
        "filename*=UTF-8''caf%C3%A9-%26amp%3B-report.html"
    )


# -- auth wiring reaches the new route too -----------------------------------


async def test_artifact_download_requires_auth(db_session, token_file, artifacts_root) -> None:
    project = await seed_project(db_session)
    item = await seed_backlog_item(db_session, project, 1)
    run = await seed_run(db_session, project, item)
    await seed_artifact(db_session, run, path="log.jsonl")

    app = make_client_app(db_session, token_file=token_file, artifacts_root=artifacts_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/runs/{run.id}/artifacts/log.jsonl")

    assert resp.status_code == 401
