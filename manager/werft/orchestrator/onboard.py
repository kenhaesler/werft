"""Onboarding (SPEC §6.3): create `unattended` from `main`, apply partial
branch protection, ensure the dispatch label, and record the project — the
one-time setup a bootstrap project needs before its first run can even be
queued.

Two GitHub calls go through the **manager**-permission `ops` (`ensure_branch`,
`ensure_label`); exactly one goes through the transient **admin**-permission
`admin_ops` (`apply_partial_protection`) — SPEC §6.3's bootstrap protection
ordering needs `administration:write` for the one `PUT
.../branches/.../protection` call and nothing else, and `werft/github/auth.py`
mints that permission set only ever for this one call, never for the
manager's own day-to-day token (`MANAGER_PERMISSIONS` deliberately excludes
`administration`). Calling `apply_partial_protection` on `ops` instead of
`admin_ops` would silently 403 in production (the manager token lacks the
scope) — a dedicated test asserts the manager ops object gets zero
protection calls.

There is no separate "is the App installed here" check: `ops.get_ref_sha` is
the first GitHub call this function makes, and `RepoOps`/`GitHubClient` sit
on top of `AppAuth.token_for`, whose `installation_id` lookup already raises
`PermanentError` on a 404 (the App is not installed on this repo) — that
error surfaces through this call as-is, unlabeled as an "onboarding" error
specifically, since it is the same auth failure any other GitHub call
against an uninstalled repo would hit. A `main` branch that doesn't exist
(App installed, but `main_branch` is wrong, or the repo is empty) is this
function's own, distinct `PermanentError`.

Idempotent by construction on the GitHub side (`ensure_branch`/
`ensure_label`/`apply_partial_protection` all tolerate a repeat call — see
`werft/github/ops.py`), but not on Werft's own side: a duplicate `slug` or
`(owner, repo)` is checked, by a plain `SELECT`, before any GitHub call is
made at all — so a re-onboard attempt against an already-onboarded project
costs zero GitHub round trips — and raises `PermanentError`; the onboarding
API endpoint maps that to a 409.

That `SELECT` is a fast path, not a guarantee: it is unsynchronized and
sits before several awaited GitHub round trips, so two concurrent onboards
of the same slug/repo both pass it and the loser is caught by the
`projects` unique constraints instead, as an `IntegrityError` out of the
INSERT below. The endpoint answers both the same way (see
`duplicate_project_message`) — the DB constraints, not this check, are what
actually keeps the row unique.
"""

from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from werft.db.models import Project, ProjectEvent
from werft.domain.errors import PermanentError
from werft.github.ops import READY_LABEL, RepoOps

#: The label's colour is onboarding's alone; the *name* is
#: `werft.github.ops.READY_LABEL`, shared with the reader
#: (`list_ready_issues`) and the remover (`merge_flow._land_merged`) — SPEC
#: §6.3's onboarding step, verbatim.
_READY_LABEL_COLOR = "0e8a16"


def duplicate_project_message(slug: str, owner: str, repo: str) -> str:
    """The one wording for "this repo is already onboarded".

    Shared with the API layer: the duplicate `SELECT` below catches the
    sequential case and raises it as a `PermanentError`, but a *concurrent*
    onboard slips past that check (it is an unsynchronized read taken before
    several awaited GitHub round trips) and only fails at the `projects`
    unique constraints. The endpoint turns that `IntegrityError` into the
    same 409 with the same body — one race, one answer, whichever way it is
    detected."""
    return f"project already onboarded: slug={slug!r} owner={owner!r} repo={repo!r}"


async def onboard_project(
    session: AsyncSession,
    ops: RepoOps,
    admin_ops: RepoOps,
    *,
    slug: str,
    owner: str,
    repo: str,
    main_branch: str = "main",
    unattended_branch: str = "unattended",
) -> Project:
    """Onboard one repo as a bootstrap project (SPEC §6.3). See the module
    docstring for the manager/admin permission split and the duplicate/
    installation error taxonomy. Does not commit — the caller owns the
    transaction (the orchestrator's per-unit session, or the onboarding API
    endpoint's own request-scoped session)."""
    duplicate = await session.execute(
        select(Project.id).where(
            or_(
                Project.slug == slug,
                and_(Project.github_owner == owner, Project.github_repo == repo),
            )
        )
    )
    if duplicate.scalar_one_or_none() is not None:
        raise PermanentError(duplicate_project_message(slug, owner, repo))

    main_sha = await ops.get_ref_sha(main_branch)
    if main_sha is None:
        raise PermanentError(f"repo or main branch not found: {owner}/{repo}@{main_branch}")

    await ops.ensure_branch(unattended_branch, from_sha=main_sha)
    await admin_ops.apply_partial_protection(unattended_branch)
    await ops.ensure_label(READY_LABEL, _READY_LABEL_COLOR)

    inserted = await session.execute(
        insert(Project)
        .values(
            slug=slug,
            github_owner=owner,
            github_repo=repo,
            main_branch=main_branch,
            unattended_branch=unattended_branch,
            onboarded_at=func.now(),
        )
        .returning(Project.id)
    )
    project_id = inserted.scalar_one()
    session.add(
        ProjectEvent(
            project_id=project_id,
            event_type="onboarded",
            payload={"owner": owner, "repo": repo},
        )
    )
    return await session.get(Project, project_id)
