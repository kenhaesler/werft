"""What every provider adapter must supply (SPEC §5).

An adapter is a *spec*, not a subprocess driver: it builds argv and env, and it
classifies an outcome from structured signals. Running the process is the
runner's job; deciding what happened is this layer's.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from werft.contracts.result import ResultStatus, UsageReport
from werft.contracts.task import TaskSpec
from werft.domain.attempts import AttemptOutcome


@dataclass(frozen=True)
class Classification:
    """The typed outcome of one attempt.

    `outcome` feeds `run_attempts` (the retry ledger); `status` feeds
    `result.json`. `exhausted_until` is the provider-reported quota signal that
    always beats the ledger's optimism (SPEC §7) — `None` when the provider gave
    no reset time, which is the common case and must not be faked.
    """

    #: `None` means "not yet determined" — the CLI finished cleanly but no CI
    #: has run, so no attempt outcome exists yet. Doctrine #1: only executed
    #: checks decide whether work is good. The orchestrator fills in
    #: ci_green/ci_red once the oracle reports.
    outcome: AttemptOutcome | None
    status: ResultStatus
    detail: str
    exhausted_until: datetime | None = None


class ProviderSpec(Protocol):
    """Providers are subscription CLIs dispatched at the process layer (doctrine #3)."""

    code: str

    def build_argv(
        self, task: TaskSpec, *, prompt_file: str, system_prompt_file: str
    ) -> list[str]: ...

    def build_env(self, task: TaskSpec, *, credential_path: str) -> dict[str, str]: ...

    def classify(self, *, envelope: dict | None, stderr: str, exit_code: int) -> Classification: ...

    def read_usage(self, envelope: dict | None) -> UsageReport | None: ...
