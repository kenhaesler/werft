"""`apply_password_file` (SPEC §10): the one place `database_url`'s password
is spliced in from a file mount. Both `create_app` (`test_app.py`) and
`db/migrations/env.py` call this directly; these tests pin the pure
string-composition contract in isolation."""

from pathlib import Path

import pytest

from werft.domain.db_url import apply_password_file
from werft.domain.errors import PermanentError


def test_no_password_file_returns_the_url_unchanged():
    url = "postgresql+asyncpg://werft@postgres:5432/werft"
    assert apply_password_file(url, "") == url


def test_password_file_set_splices_the_password_into_the_url(tmp_path: Path):
    secret = tmp_path / "pg_password"
    secret.write_text("s3cret\n")

    resolved = apply_password_file("postgresql+asyncpg://werft@postgres:5432/werft", str(secret))

    assert resolved == "postgresql+asyncpg://werft:s3cret@postgres:5432/werft"


def test_password_file_contents_are_stripped(tmp_path: Path):
    secret = tmp_path / "pg_password"
    secret.write_text("  s3cret  \n\n")

    resolved = apply_password_file("postgresql+asyncpg://werft@postgres:5432/werft", str(secret))

    assert resolved == "postgresql+asyncpg://werft:s3cret@postgres:5432/werft"


def test_password_file_set_but_missing_is_a_permanent_error(tmp_path: Path):
    missing = tmp_path / "does-not-exist"

    with pytest.raises(PermanentError, match="WERFT_DATABASE_PASSWORD_FILE"):
        apply_password_file("postgresql+asyncpg://werft@postgres:5432/werft", str(missing))


def test_password_file_empty_is_a_permanent_error(tmp_path: Path):
    """An empty file would otherwise silently produce a passwordless URL —
    the manager boots clean and fails opaquely on the first real connection
    instead of loudly at startup."""
    secret = tmp_path / "pg_password"
    secret.write_text("")

    with pytest.raises(PermanentError, match="WERFT_DATABASE_PASSWORD_FILE"):
        apply_password_file("postgresql+asyncpg://werft@postgres:5432/werft", str(secret))


def test_password_file_whitespace_only_is_a_permanent_error(tmp_path: Path):
    secret = tmp_path / "pg_password"
    secret.write_text("   \n\n  \n")

    with pytest.raises(PermanentError, match="WERFT_DATABASE_PASSWORD_FILE"):
        apply_password_file("postgresql+asyncpg://werft@postgres:5432/werft", str(secret))


def test_password_file_unreadable_is_a_permanent_error(tmp_path: Path, monkeypatch):
    """Compose file-secrets inherit host file modes, and the manager does
    not run as root — `Path.is_file()` passing is not proof the process can
    actually read the file's contents. A real permission failure is
    platform/ACL-dependent to reproduce, so this monkeypatches `read_text`
    to raise the `OSError` subclass a real permission failure would (rather
    than fighting Windows ACLs / running as non-root in CI)."""
    secret = tmp_path / "pg_password"
    secret.write_text("s3cret\n")

    def _raise(*args, **kwargs):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "read_text", _raise)

    with pytest.raises(PermanentError, match="WERFT_DATABASE_PASSWORD_FILE"):
        apply_password_file("postgresql+asyncpg://werft@postgres:5432/werft", str(secret))
