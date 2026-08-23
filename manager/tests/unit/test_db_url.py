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
