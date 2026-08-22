from datetime import UTC, datetime, timedelta

from werft.github.auth import RUNNER_PERMISSIONS, InstallationToken
from werft.orchestrator.credentials import RunCredentials
from werft.runner.workspace import create_run_dirs, placement_for


class FakeAuth:
    def __init__(self, *tokens: InstallationToken) -> None:
        self._tokens = list(tokens)
        self.mints: list[tuple[str, str, dict, bool]] = []
        self.revoked: list[str] = []

    async def token_for(self, owner, repo, permissions, *, transient=False):
        self.mints.append((owner, repo, permissions, transient))
        return self._tokens.pop(0)

    async def revoke(self, token):
        self.revoked.append(token)
        return True


def token(value: str, minutes: int) -> InstallationToken:
    return InstallationToken(token=value, expires_at=datetime.now(UTC) + timedelta(minutes=minutes))


def creds(auth, tmp_path):
    placement = placement_for("r1", runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)
    return RunCredentials(auth, placement=placement, owner="ken", repo="elastic"), placement


async def test_mint_is_transient_and_uses_the_runner_permission_ceiling(tmp_path):
    """SPEC §4.4: attenuated to `contents: write` on the one repo. `transient`
    keeps a per-run token out of `AppAuth`'s shared cache, so revoking it at
    teardown cannot hand a dead token to an unrelated caller."""
    auth = FakeAuth(token("ghs_one", 60))
    credentials, placement = creds(auth, tmp_path)

    await credentials.mint()

    assert auth.mints == [("ken", "elastic", RUNNER_PERMISSIONS, True)]
    assert (tmp_path / "r1" / "secrets" / "git_token").read_text(encoding="utf-8") == "ghs_one"


async def test_refresh_replaces_by_rename_then_revokes_the_old_one(tmp_path):
    """Replace-then-revoke, in that order: the mounted file must never hold a
    token that has already been revoked, because the in-box askpass reads it on
    every git call."""
    auth = FakeAuth(token("ghs_one", 5), token("ghs_two", 60))
    credentials, placement = creds(auth, tmp_path)
    await credentials.mint()

    assert await credentials.refresh_if_due() is True

    token_path = tmp_path / "r1" / "secrets" / "git_token"
    assert token_path.read_text(encoding="utf-8") == "ghs_two"
    assert auth.revoked == ["ghs_one"]
    assert not list((tmp_path / "r1" / "secrets").glob(".git_token*"))  # no temp left behind


async def test_refresh_is_a_no_op_while_the_token_is_fresh(tmp_path):
    """Expiry-based, not a fixed cadence: a 55-minute-valid token is not
    re-minted just because the ticker fired."""
    auth = FakeAuth(token("ghs_one", 55))
    credentials, _ = creds(auth, tmp_path)
    await credentials.mint()

    assert await credentials.refresh_if_due() is False
    assert len(auth.mints) == 1


async def test_refresh_before_a_mint_mints(tmp_path):
    auth = FakeAuth(token("ghs_one", 60))
    credentials, _ = creds(auth, tmp_path)
    assert await credentials.refresh_if_due() is True


async def test_revoke_removes_the_file_and_forgets_the_token(tmp_path):
    auth = FakeAuth(token("ghs_one", 60))
    credentials, _ = creds(auth, tmp_path)
    await credentials.mint()

    await credentials.revoke()

    assert auth.revoked == ["ghs_one"]
    assert not (tmp_path / "r1" / "secrets" / "git_token").exists()
    assert credentials.token is None


async def test_revoke_without_a_mint_is_silent(tmp_path):
    credentials, _ = creds(FakeAuth(), tmp_path)
    await credentials.revoke()


async def test_revoke_never_raises_even_when_github_refuses(tmp_path):
    """`AppAuth.revoke` never raises by contract; this must not become the
    place a teardown dies."""

    class Boom(FakeAuth):
        async def revoke(self, token):
            raise RuntimeError("github is down")

    auth = Boom(token("ghs_one", 60))
    credentials, _ = creds(auth, tmp_path)
    await credentials.mint()
    await credentials.revoke()  # must not raise
