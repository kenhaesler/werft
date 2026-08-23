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
    try:
        contents = path.read_text()
    except OSError as exc:
        # Compose file-secrets inherit host file modes, and the manager does
        # not run as root — `is_file()` passing is not proof the process can
        # actually read the file's contents. Same taxonomy as the two guards
        # above: a permission failure here belongs at boot, not surfaced as
        # an opaque failure the first time a route issues a query.
        raise PermanentError(
            f"WERFT_DATABASE_PASSWORD_FILE is set to {password_file!r} but it "
            f"could not be read: {exc}"
        ) from exc
    password = contents.strip()
    if not password:
        # An empty/whitespace-only file would otherwise silently produce a
        # passwordless URL — the manager then boots clean and fails opaquely
        # on the first real connection instead of at startup where the
        # operator is watching (same D3 rationale as the other boot guards).
        raise PermanentError(
            f"WERFT_DATABASE_PASSWORD_FILE is set to {password_file!r} but its "
            "contents are empty (after stripping whitespace)."
        )
    return make_url(database_url).set(password=password).render_as_string(hide_password=False)
