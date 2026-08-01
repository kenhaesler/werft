"""Project lifecycle (SPEC §3.1)."""

from enum import StrEnum


class ProjectLifecycle(StrEnum):
    BOOTSTRAP = "bootstrap"
    ORACLE_GATED = "oracle_gated"
