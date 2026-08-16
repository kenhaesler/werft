"""Task 16 (B7): the composition root's conditional dashboard mount.

`Settings.dashboard_dist` naming an existing directory at `create_app` time
mounts it at `/ui` (`StaticFiles(..., html=True)`, so `/ui/` serves its
`index.html`) and adds a `/` -> `/ui/` redirect. Unset or pointing at a
missing directory, the manager boots API-only — no mount, `/` 404s — so a
manager deployed without a built dashboard never fails to start."""

from pathlib import Path

import httpx

from werft.app import create_app
from werft.config.settings import Settings


async def test_dashboard_dist_present_mounts_ui_and_redirects_root(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>werft dashboard</title>")

    app = create_app(Settings(dashboard_dist=str(dist)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        redirect = await client.get("/", follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/ui/"

        served = await client.get("/ui/")
        assert served.status_code == 200
        assert "werft dashboard" in served.text


async def test_dashboard_dist_unset_boots_api_only_and_root_404s() -> None:
    """The default `Settings()` (empty `dashboard_dist`) — the manager must
    still boot clean and answer `/healthz` with no mount at all."""
    app = create_app(Settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 404

        healthz = await client.get("/healthz")
        assert healthz.status_code == 200


async def test_dashboard_dist_pointing_at_missing_directory_boots_api_only(
    tmp_path: Path,
) -> None:
    """A configured-but-missing `dashboard_dist` (e.g. the build step hasn't
    run yet) must not crash `create_app` — `StaticFiles` requires its
    directory to exist at construction time, so the mount is conditional on
    an existence check, not just a truthy setting."""
    missing = tmp_path / "does-not-exist"

    app = create_app(Settings(dashboard_dist=str(missing)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 404

        healthz = await client.get("/healthz")
        assert healthz.status_code == 200
