"""`result.json` — runner to manager (SPEC §4.3).

"the behavioral completion contract — `status` drives control flow; only
free-text and token counts are display-only" (SPEC §4.3, resolving [BP§14]
defect #3).

Written atomically as the adapter's last act. On a non-zero exit code the
manager treats the exit code as authoritative and any present result as advisory.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ResultStatus(StrEnum):
    """SPEC §4.3: "`result.json.status` set includes `quota_exhausted` from day one"."""

    SUCCESS = "success"
    FAILURE = "failure"
    QUOTA_EXHAUSTED = "quota_exhausted"
    POLICY_BLOCK = "policy_block"
    AUTH_FAILURE = "auth_failure"
    TIMEOUT = "timeout"
    ERROR = "error"


class UsageReport(BaseModel):
    """Observational only.

    SPEC §7: "tokens are never admission inputs [CLP C2]". Quota admission is
    metered provider-CLI wall-clock seconds; nothing here is ever read by the
    admission path — these numbers exist to be displayed and to feed the ledger's
    display fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_cost_usd: float | None = None


class ResultError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str = ""


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ResultStatus
    commit_sha: str | None = None
    pushed: bool = False
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    usage: UsageReport | None = None
    error: ResultError | None = None
