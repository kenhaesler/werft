"""Per-project dispatch config: the file, not the database (plan decision D3).

SPEC §4.5 puts per-project registry lists in Werft's own config, never in the
managed repository — an agent can edit the repository. SPEC §4.1 builds one
base image per project **on the VM**, so `image_digest` — the dominant field
here — changes every time the operator rebuilds one; a file re-read once per
dispatch sweep makes that rebuild take effect without a manager restart, while
a column would need a migration plus either an onboard-payload change or
hand-written SQL. And SPEC §9 closes the API write set, so a DB-held runner
config would need a seventh mutation endpoint to be operable at all — which
that section names as the signal to re-read the doctrine.

Layering: `config` is a sibling of `contracts` and `db`, so this module cannot
see `runner.create_body.ProjectRunnerConfig`. `orchestrator/dispatch.py` is the
one conversion point.

`extra="forbid"` is what keeps the vocabulary closed, the same discipline
`ProjectRunnerConfig` uses: a typo is a refusal, never a silently ignored knob.
"""

import json
import re
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from werft.domain.errors import PermanentError

logger = structlog.get_logger(__name__)

SETTING_NAME = "WERFT_DISPATCH_CONFIG_FILE"

#: SPEC §4.5's own examples, closed vocabulary. PyPI needs both hosts; the dnf
#: set is the Rocky mirror pair the base image already uses.
REGISTRY_PRESETS: dict[str, tuple[str, ...]] = {
    "npm": ("registry.npmjs.org",),
    "pypi": ("pypi.org", "files.pythonhosted.org"),
    "dnf-rocky": ("mirrors.rockylinux.org", "dl.rockylinux.org"),
    "crates": ("crates.io", "static.crates.io", "index.crates.io"),
    "go": ("proxy.golang.org", "sum.golang.org"),
}

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)


class ProjectDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Digest-pinned (SPEC §4.1). `build_create_body` rejects tags too; failing
    #: at load means the operator learns at boot rather than at the first claim.
    image_digest: str
    #: SPEC §5: "a config value per project/work-type; this spec hardcodes no
    #: model IDs."
    model: str
    #: Doubles as the quota reservation (SPEC §7) and the manager-enforced
    #: container ceiling (SPEC §4.3, 90 min default).
    timeout_seconds: int = Field(default=5400, gt=0, le=90 * 60)
    memory_bytes: int = Field(default=4 << 30, ge=2 << 30, le=32 << 30)
    nano_cpus: int = Field(default=2_000_000_000, ge=1_000_000_000, le=8_000_000_000)
    #: SPEC §4.5: the project's declared registry set, by preset name — werft
    #: config, never repo config. Expanded by `egress_hosts()`.
    registries: list[str] = Field(default_factory=list)
    #: SPEC §4.5: "the project's declared extra hosts" — literal hostnames.
    extra_hosts: list[str] = Field(default_factory=list)

    @field_validator("image_digest")
    @classmethod
    def _digest_pinned(cls, value: str) -> str:
        if "@sha256:" not in value:
            raise ValueError(
                f"image {value!r} must be digest-pinned with @sha256: (SPEC §4.1); got a tag"
            )
        return value

    @field_validator("registries")
    @classmethod
    def _known_presets(cls, value: list[str]) -> list[str]:
        unknown = [r for r in value if r not in REGISTRY_PRESETS]
        if unknown:
            raise ValueError(
                f"unknown registry preset(s) {unknown!r}; known: {sorted(REGISTRY_PRESETS)}"
            )
        return value

    @field_validator("extra_hosts")
    @classmethod
    def _hostname_shaped(cls, value: list[str]) -> list[str]:
        for host in value:
            if not _HOSTNAME.match(host.lower()):
                raise ValueError(f"extra host {host!r} is not a bare hostname")
        return value

    def egress_hosts(self) -> list[str]:
        """SPEC §4.5: presets expanded, extra hosts merged, sorted and deduped
        so the driver (a later task) gets a stable, comparable host list."""
        hosts: set[str] = set()
        for preset in self.registries:
            hosts.update(REGISTRY_PRESETS[preset])
        hosts.update(h.lower() for h in self.extra_hosts)
        return sorted(hosts)


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projects: dict[str, ProjectDispatch] = Field(default_factory=dict)

    def for_slug(self, slug: str) -> ProjectDispatch | None:
        return self.projects.get(slug)


def load_dispatch_config(path: str) -> DispatchConfig:
    """An unset or absent path is an empty config, never a boot failure: the
    manager must still serve `/api/v1` and its pollers with nothing to
    dispatch. A path that *is* set but unreadable or invalid raises — and
    `create_app` calls this directly, so a broken file fails boot loudly rather
    than parking every run at 03:00.

    A path that is set but names no file is the third case, and it is the one
    that used to boot silently: every candidate then parks with
    `permanent_error`, one per tick, each needing a manual requeue — which is
    exactly what D3's "never park runs on a typo" exists to prevent. It stays
    non-fatal (unset must remain a clean boot, and the file is edited by hand
    between image rebuilds) but it is now loud, so a mistyped
    `WERFT_DISPATCH_CONFIG_FILE` is legible at boot instead of at 03:00.
    """
    if not path:
        return DispatchConfig()
    file = Path(path)
    if not file.is_file():
        logger.warning("app.dispatch_config_file_missing", path=path, setting=SETTING_NAME)
        return DispatchConfig()
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermanentError(f"dispatch config {path!r} is unreadable: {exc}") from exc
    try:
        return DispatchConfig.model_validate(payload)
    except ValidationError as exc:
        raise PermanentError(f"dispatch config {path!r} is invalid: {exc}") from exc


def dispatch_for(config: DispatchConfig, slug: str) -> ProjectDispatch:
    entry = config.for_slug(slug)
    if entry is None:
        raise PermanentError(
            f"no dispatch config for project {slug!r} (add it to the file named by {SETTING_NAME})"
        )
    return entry


class DispatchConfigCache:
    """Re-read once per dispatch sweep, with a last-good fallback.

    The two failure modes are deliberately asymmetric (D3). At **startup** a
    malformed file is fatal: the operator is watching, and a manager that boots
    on a broken config parks every run overnight. **Mid-flight** it is not: the
    file is edited by hand between image rebuilds, and a half-saved buffer must
    not take the loop down or convert the queue into parked rows.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._last_good = DispatchConfig()

    def current(self) -> DispatchConfig:
        try:
            self._last_good = load_dispatch_config(self._path)
        except PermanentError as exc:
            logger.error("dispatch.config_unreadable", path=self._path, error=str(exc))
        return self._last_good
