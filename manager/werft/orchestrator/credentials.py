"""The run's GitHub credential (SPEC §4.4).

One installation token per attempt, attenuated to `contents: write` on the one
repository, minted `transient=True` so a per-run token never occupies
`AppAuth`'s shared cache slot or reaches an unrelated caller. Written **by
rename**, because the in-container `git-askpass.sh` reads the file on every git
invocation — an atomic replace is what lets a 90-minute run outlive a one-hour
token without restarting anything.

Re-mint is **expiry-based**, not a fixed cadence: the driver's ticker asks on
every beat and this answers "not yet" until fewer than `remint_margin` of
validity remain. Ordering is replace-then-revoke, so the mounted file never
holds a token that has already been revoked.

Revoked on every teardown path — success, failure, cancel, deadline, driver
crash, shutdown. A crashed manager cannot revoke a token it no longer holds;
that one expires naturally within the hour, which is recorded and accepted
(SPEC §4.4's revoke is best-effort by construction).

This lives in `orchestrator/` rather than `runner/` because it needs both
`werft.github` and the runner's secrets directory, and those two are
independent siblings in the layer contract.
"""

import contextlib
import os
from datetime import UTC, datetime, timedelta

import structlog

from werft.github.auth import RUNNER_PERMISSIONS, AppAuth, InstallationToken
from werft.runner.create_body import RunPlacement
from werft.runner.workspace import GIT_TOKEN_FILENAME, write_secret

logger = structlog.get_logger(__name__)

#: Comfortably longer than the longest gap between two `refresh_if_due` calls
#: (the driver ticker's `heartbeat_seconds`).
DEFAULT_REMINT_MARGIN = timedelta(minutes=10)


class RunCredentials:
    def __init__(
        self,
        auth: AppAuth,
        *,
        placement: RunPlacement,
        owner: str,
        repo: str,
        remint_margin: timedelta = DEFAULT_REMINT_MARGIN,
    ) -> None:
        self._auth = auth
        self._placement = placement
        self._owner = owner
        self._repo = repo
        self._margin = remint_margin
        self._current: InstallationToken | None = None

    @property
    def token(self) -> str | None:
        return self._current.token if self._current else None

    async def mint(self) -> str:
        minted = await self._auth.token_for(
            self._owner, self._repo, RUNNER_PERMISSIONS, transient=True
        )
        write_secret(self._placement, GIT_TOKEN_FILENAME, minted.token)
        self._current = minted
        return minted.token

    async def refresh_if_due(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self._current is None:
            await self.mint()
            return True
        if self._current.expires_at - now > self._margin:
            return False
        stale = self._current.token
        await self.mint()
        with contextlib.suppress(Exception):
            await self._auth.revoke(stale)
        logger.info("driver.token_reminted", run_id=self._placement.run_id)
        return True

    async def revoke(self) -> None:
        if self._current is None:
            return
        with contextlib.suppress(Exception):
            await self._auth.revoke(self._current.token)
        self._current = None
        with contextlib.suppress(OSError):
            os.remove(os.path.join(self._placement.secrets_dir, GIT_TOKEN_FILENAME))
