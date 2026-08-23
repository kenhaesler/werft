"""SPEC §10: secrets are file mounts, never env. `database_url` (both
`Settings.database_url` and the `WERFT_DATABASE_URL`/`WERFT_TEST_DATABASE_URL`
env vars `db/migrations/env.py` reads directly) is a password-free template;
this module supplies the one place that splices the real password in from
its file mount, so `app.py`'s `create_app` and the `alembic` migration
entrypoint resolve it identically. It lives in `werft.domain` — the layer
every other layer may import (SPEC §1) — specifically so `db/migrations/env.py`
(part of the `db` layer, a sibling of `config`, not an importer of it) can
call it without importing `werft.config`."""

from pathlib import Path

from sqlalchemy.engine.url import make_url

from werft.domain.errors import PermanentError


def apply_password_file(database_url: str, password_file: str) -> str:
    """Return `database_url` with its password replaced by the stripped
    contents of `password_file`. An empty `password_file` means "not
    configured" — `database_url` is returned unchanged, exactly as it was
    before this seam existed. A `password_file` that is set but unreadable
    is a boot failure (`PermanentError`, same taxonomy as the T9 egress
    guard in `create_app`): a manager that boots against a stale/missing
    secret mount would otherwise fail confusingly on the first query, not at
    startup where the operator is watching.
    """
    if not password_file:
        return database_url
    path = Path(password_file)
    if not path.is_file():
        raise PermanentError(
            f"WERFT_DATABASE_PASSWORD_FILE is set to {password_file!r} but that "
            "file does not exist or is not readable."
        )
    password = path.read_text().strip()
    return make_url(database_url).set(password=password).render_as_string(hide_password=False)
