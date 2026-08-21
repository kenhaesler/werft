"""`orchestrator/onboard.py` against a real DB (SPEC §6.3 bootstrap
protection ordering; plan Task 8).

Unlike the rest of `tests/integration`, this module needs no seeding
helpers beyond a unique tag per test: `onboard_project` starts from an
empty `projects` table by design — onboarding *is* the seed.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from werft.db.models import Project, ProjectEvent
from werft.domain.errors import PermanentError
from werft.orchestrator.onboard import onboard_project

# -- fakes ------------------------------------------------------------------


class FakeRepoOps:
    """Duck-typed manager-permission `RepoOps`. Tracks every call,
    including `apply_partial_protection` — onboarding must never call this
    on the manager ops object (SPEC §6.3: that call needs
    `administration:write`, which the manager's own token deliberately
    lacks), so a test can assert this list stays empty."""

    def __init__(self, *, main_sha: str | None = "main-sha-abc123") -> None:
        self._main_sha = main_sha
        self.get_ref_sha_calls: list[str] = []
        self.ensure_branch_calls: list[tuple[str, str]] = []
        self.ensure_label_calls: list[tuple[str, str]] = []
        self.apply_partial_protection_calls: list[str] = []

    async def get_ref_sha(self, branch: str) -> str | None:
        self.get_ref_sha_calls.append(branch)
        return self._main_sha

    async def ensure_branch(self, branch: str, from_sha: str) -> str:
        self.ensure_branch_calls.append((branch, from_sha))
        return from_sha

    async def ensure_label(self, name: str, color: str) -> None:
        self.ensure_label_calls.append((name, color))

    async def apply_partial_protection(self, branch: str) -> None:
        self.apply_partial_protection_calls.append(branch)


class FakeAdminOps:
    """Duck-typed admin-permission `RepoOps`: the only object onboarding
    should ever call `apply_partial_protection` on."""

    def __init__(self) -> None:
        self.apply_partial_protection_calls: list[str] = []

    async def apply_partial_protection(self, branch: str) -> None:
        self.apply_partial_protection_calls.append(branch)


async def project_events(session, project_id) -> list[ProjectEvent]:
    result = await session.execute(
        select(ProjectEvent)
        .where(ProjectEvent.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


# -- happy path ---------------------------------------------------------------


async def test_onboard_happy_path_ensures_branch_protects_via_admin_labels_and_records(
    db_session,
) -> None:
    tag = uuid.uuid4().hex[:8]
    ops = FakeRepoOps(main_sha="deadbeef")
    admin_ops = FakeAdminOps()

    project = await onboard_project(
        db_session,
        ops,
        admin_ops,
        slug=f"proj-{tag}",
        owner="acme",
        repo=f"widgets-{tag}",
    )
    await db_session.commit()

    assert ops.get_ref_sha_calls == ["main"]
    assert ops.ensure_branch_calls == [("unattended", "deadbeef")]
    assert ops.ensure_label_calls == [("werft:ready", "0e8a16")]

    # The load-bearing assertion: the manager ops object never applies
    # protection — only the transient admin ops object does.
    assert ops.apply_partial_protection_calls == []
    assert admin_ops.apply_partial_protection_calls == ["unattended"]

    assert project.slug == f"proj-{tag}"
    assert project.github_owner == "acme"
    assert project.github_repo == f"widgets-{tag}"
    assert project.main_branch == "main"
    assert project.unattended_branch == "unattended"
    assert project.onboarded_at is not None

    stored = await db_session.get(Project, project.id)
    assert stored is not None

    events = await project_events(db_session, project.id)
    assert len(events) == 1
    assert events[0].event_type == "onboarded"
    assert events[0].payload == {"owner": "acme", "repo": f"widgets-{tag}"}


async def test_onboard_custom_branch_names_are_used_verbatim(db_session) -> None:
    tag = uuid.uuid4().hex[:8]
    ops = FakeRepoOps(main_sha="cafef00d")
    admin_ops = FakeAdminOps()

    project = await onboard_project(
        db_session,
        ops,
        admin_ops,
        slug=f"custom-{tag}",
        owner="acme",
        repo=f"custom-{tag}",
        main_branch="trunk",
        unattended_branch="auto",
    )
    await db_session.commit()

    assert ops.get_ref_sha_calls == ["trunk"]
    assert ops.ensure_branch_calls == [("auto", "cafef00d")]
    assert admin_ops.apply_partial_protection_calls == ["auto"]
    assert project.main_branch == "trunk"
    assert project.unattended_branch == "auto"


# -- missing main branch / uninstalled repo ------------------------------------


async def test_onboard_missing_main_branch_raises_permanent_error_before_protection_or_label(
    db_session,
) -> None:
    ops = FakeRepoOps(main_sha=None)
    admin_ops = FakeAdminOps()

    with pytest.raises(PermanentError):
        await onboard_project(
            db_session, ops, admin_ops, slug="no-main", owner="acme", repo="ghost"
        )

    assert ops.get_ref_sha_calls == ["main"]
    assert ops.ensure_branch_calls == []
    assert ops.ensure_label_calls == []
    assert admin_ops.apply_partial_protection_calls == []


# -- duplicates -----------------------------------------------------------------


async def test_onboard_duplicate_slug_raises_permanent_error_with_zero_github_calls(
    db_session,
) -> None:
    tag = uuid.uuid4().hex[:8]
    await onboard_project(
        db_session,
        FakeRepoOps(),
        FakeAdminOps(),
        slug=f"dup-{tag}",
        owner="acme",
        repo=f"one-{tag}",
    )
    await db_session.commit()

    ops2 = FakeRepoOps()
    admin_ops2 = FakeAdminOps()
    with pytest.raises(PermanentError):
        await onboard_project(
            db_session,
            ops2,
            admin_ops2,
            slug=f"dup-{tag}",
            owner="acme",
            repo=f"two-{tag}",
        )

    # The duplicate check happens before any GitHub call at all — a
    # re-onboard attempt against an already-onboarded slug costs nothing.
    assert ops2.get_ref_sha_calls == []
    assert ops2.ensure_branch_calls == []
    assert ops2.ensure_label_calls == []
    assert admin_ops2.apply_partial_protection_calls == []


async def test_onboard_duplicate_owner_repo_raises_permanent_error(db_session) -> None:
    tag = uuid.uuid4().hex[:8]
    await onboard_project(
        db_session,
        FakeRepoOps(),
        FakeAdminOps(),
        slug=f"first-{tag}",
        owner="acme",
        repo=f"widgets-{tag}",
    )
    await db_session.commit()

    ops2 = FakeRepoOps()
    admin_ops2 = FakeAdminOps()
    with pytest.raises(PermanentError):
        await onboard_project(
            db_session,
            ops2,
            admin_ops2,
            slug=f"second-{tag}",
            owner="acme",
            repo=f"widgets-{tag}",
        )

    assert ops2.get_ref_sha_calls == []
    assert admin_ops2.apply_partial_protection_calls == []


async def test_onboard_after_commit_a_repeat_call_with_identical_args_still_dupes(
    db_session,
) -> None:
    """GitHub-side calls are all idempotent-tolerant (SPEC §6.3): a re-run
    of onboarding with the exact same arguments would be harmless on
    GitHub's side, but Werft's own duplicate check still stops it before
    any GitHub call is made, since the `projects` row itself already
    exists."""
    tag = uuid.uuid4().hex[:8]
    kwargs = {"slug": f"idem-{tag}", "owner": "acme", "repo": f"idem-{tag}"}
    await onboard_project(db_session, FakeRepoOps(), FakeAdminOps(), **kwargs)
    await db_session.commit()

    ops2 = FakeRepoOps()
    with pytest.raises(PermanentError):
        await onboard_project(db_session, ops2, FakeAdminOps(), **kwargs)

    assert ops2.get_ref_sha_calls == []


# -- DuplicateProjectError / is_duplicate_project_violation ------------------


def _integrity(sqlstate: str, constraint: str) -> IntegrityError:
    """Fakes what SQLAlchemy hands us — asyncpg's error, wrapped."""
    orig = SimpleNamespace(sqlstate=sqlstate, constraint_name=constraint)
    return IntegrityError("stmt", {}, orig)


def test_duplicate_project_error_is_a_permanent_error_subtype():
    from werft.orchestrator.onboard import DuplicateProjectError

    assert issubclass(DuplicateProjectError, PermanentError)


def test_only_the_two_projects_unique_constraints_read_as_duplicates():
    """Postgres auto-names them from `0001_spine.py`'s `slug TEXT NOT NULL
    UNIQUE` and `UNIQUE (github_owner, github_repo)`."""
    from werft.orchestrator.onboard import is_duplicate_project_violation

    assert is_duplicate_project_violation(_integrity("23505", "projects_slug_key")) is True
    assert (
        is_duplicate_project_violation(_integrity("23505", "projects_github_owner_github_repo_key"))
        is True
    )
    assert is_duplicate_project_violation(_integrity("23514", "projects_slug_check")) is False
    assert is_duplicate_project_violation(_integrity("23505", "runs_pkey")) is False
