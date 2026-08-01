"""The Claude Code adapter (SPEC §5, §4.4).

Two rules shape this file and both come from things that have already gone
wrong once:

1. **Never `--bare`.** It is mutually exclusive with the OAuth token the
   subscription posture depends on, and it discards `CLAUDE.md` [BP§15.3]. A
   `--bare` run either fails auth or silently falls through to API-rate billing,
   breaking the flat-rate premise doctrine #3 rests on.
2. **Account-level failures emit plain stderr with no JSON envelope.** They must
   be classified by stderr match and never filed as parse errors, "or the
   re-auth/quota alerts never fire" (SPEC §5). A generic
   `outcome_parse_failed` here is exactly the invisible-failure mode those
   alerts exist for.

A note on rate limits, verified against the CLI's documented surface 2026-08-01:
there is **no dedicated `rate_limit` subtype** in `-p` output. Transient retries
appear as `type:"system", subtype:"api_retry"` with `error:"rate_limit"`; a hard
usage-limit stop surfaces as text in the `result` field and exposes no
documented reset-time field. So `exhausted_until` is best-effort and often
`None` — SPEC §5's "+ reset time when present" is honoured literally, and §7's
ledger never depends on it.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from werft.contracts.result import ResultStatus, UsageReport
from werft.contracts.task import TaskSpec
from werft.domain.attempts import AttemptOutcome
from werft.providers.base import Classification

#: SIGTERM. The CLI exits 143 when the manager kills it at the ceiling.
SIGTERM_EXIT_CODE = 143

#: Account-level failures that arrive as bare stderr, with no JSON envelope at all.
#: Ordered: the first match wins, so the more specific patterns come first.
STDERR_PATTERNS: tuple[tuple[str, AttemptOutcome, ResultStatus], ...] = (
    (
        r"suspend|banned|account.*(disabled|terminated)",
        AttemptOutcome.POLICY_BLOCK,
        ResultStatus.POLICY_BLOCK,
    ),
    (
        r"invalid api key|unauthoriz|authentication|not logged in"
        r"|please run .?claude login|oauth token.*(expired|invalid)"
        r"|credentials? (expired|invalid|not found)",
        AttemptOutcome.AUTH_FAILURE,
        ResultStatus.AUTH_FAILURE,
    ),
    (r"usage limit|rate limit|quota", AttemptOutcome.QUOTA_EXHAUSTED, ResultStatus.QUOTA_EXHAUSTED),
)

#: A hard usage-limit stop delivered as envelope text, e.g.
#: "You have hit your session limit, resets 3:45pm".
USAGE_LIMIT_TEXT = re.compile(
    r"(hit|reached|exceeded).{0,40}(usage|session|weekly|rate)\s*limit", re.IGNORECASE
)
RESET_TEXT = re.compile(
    r"resets?\s+(?:at\s+)?(?P<when>[0-9]{1,2}:[0-9]{2}\s*(?:am|pm)?|[0-9]{4}-[0-9]{2}-[0-9]{2}T[^\s\"]+)",
    re.IGNORECASE,
)

#: Envelope subtypes that are the CLI's own budget/turn caps, not provider quota.
#: SPEC §5: "`error_max_budget_usd` maps to `agent_failure` (it is a CLI-side
#: budget, not provider quota)."
CLI_SIDE_LIMIT_SUBTYPES = frozenset(
    {"error_max_budget_usd", "error_max_turns", "error_max_structured_output_retries"}
)


@dataclass(frozen=True)
class ParsedStream:
    """What the manager needs out of a `--output-format stream-json` transcript."""

    result: dict | None
    rate_limit_signals: list[dict]


def parse_stream(lines: list[str]) -> ParsedStream:
    """Pull the final result envelope and any in-band rate-limit warnings.

    The rate-limit signals are usage-reader duty (a) — the load-bearing one.
    Malformed lines are skipped: a transcript is agent-adjacent data, and one bad
    line must not lose the envelope that decides control flow.
    """
    import json

    result: dict | None = None
    signals: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result = event
        elif (
            event.get("type") == "system"
            and event.get("subtype") == "api_retry"
            and event.get("error") == "rate_limit"
        ):
            signals.append(event)
    return ParsedStream(result=result, rate_limit_signals=signals)


def _parse_reset(text: str) -> datetime | None:
    """Best-effort. The CLI documents no reset-time field, so ambiguity means None."""
    match = RESET_TEXT.search(text or "")
    if not match:
        return None
    when = match.group("when")
    try:
        return datetime.fromisoformat(when)
    except ValueError:
        # A wall-clock string like "3:45pm" has no date and no timezone; inventing
        # one would hand the quota ledger a wrong number, which §7 forbids.
        return None


class ClaudeSpec:
    """SPEC §5's adapter, as a pure spec object."""

    code = "claude"

    def build_argv(self, task: TaskSpec, *, prompt_file: str, system_prompt_file: str) -> list[str]:
        return [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            task.model,  # SPEC §5: config value per project; no model IDs here
            "--append-system-prompt-file",
            system_prompt_file,
            "--permission-mode",
            "acceptEdits",
            "@" + prompt_file,
        ]

    def build_env(self, task: TaskSpec, *, credential_path: str) -> dict[str, str]:
        """The credential is read from the read-only mount, never passed in argv.

        SPEC §4.4: the provider credential is manager-held and mounted ro. Env is
        used for the token because the CLI reads it there; the *value* never
        appears in argv, where it would be world-readable via /proc/*/cmdline.
        """
        with open(credential_path, encoding="utf-8") as handle:
            token = handle.read().strip()
        return {
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            "CI": "true",
            "TERM": "dumb",
        }

    def classify(self, *, envelope: dict | None, stderr: str, exit_code: int) -> Classification:
        stderr = stderr or ""

        # 1. The manager killed us at the ceiling.
        if exit_code == SIGTERM_EXIT_CODE:
            return Classification(
                AttemptOutcome.TIMEOUT, ResultStatus.TIMEOUT, "terminated at the run ceiling"
            )

        # 2. Account-level failures: plain stderr, no envelope. Checked BEFORE any
        #    parse handling, because filing these as parse errors silences the
        #    re-auth and quota alerts (SPEC §5).
        for pattern, outcome, status in STDERR_PATTERNS:
            if re.search(pattern, stderr, re.IGNORECASE):
                return Classification(outcome, status, f"stderr match: {pattern}")

        # 3. No envelope and nothing recognisable on stderr.
        if envelope is None:
            return Classification(
                AttemptOutcome.AGENT_FAILURE,
                ResultStatus.ERROR,
                "no result envelope and no recognised account-level stderr",
            )

        # 4. Refusal.
        if envelope.get("stop_reason") == "refusal":
            return Classification(
                AttemptOutcome.POLICY_BLOCK, ResultStatus.POLICY_BLOCK, "stop_reason=refusal"
            )

        subtype = str(envelope.get("subtype") or "")

        # 5. CLI-side caps are the agent's failure, never provider quota.
        if subtype in CLI_SIDE_LIMIT_SUBTYPES:
            return Classification(
                AttemptOutcome.AGENT_FAILURE, ResultStatus.FAILURE, f"subtype={subtype}"
            )

        # 6. A hard usage-limit stop arrives as text in `result`.
        text = str(envelope.get("result") or "")
        if USAGE_LIMIT_TEXT.search(text):
            return Classification(
                AttemptOutcome.QUOTA_EXHAUSTED,
                ResultStatus.QUOTA_EXHAUSTED,
                "provider usage limit reported in the result text",
                exhausted_until=_parse_reset(text),
            )

        if subtype == "success" and not envelope.get("is_error"):
            return Classification(AttemptOutcome.CI_GREEN, ResultStatus.SUCCESS, "subtype=success")

        return Classification(
            AttemptOutcome.AGENT_FAILURE,
            ResultStatus.FAILURE,
            f"subtype={subtype or 'unknown'} is_error={envelope.get('is_error')}",
        )

    def read_usage(self, envelope: dict | None) -> UsageReport | None:
        """Usage-reader duty (b): per-run token/cost fields → ledger input.

        Display-only by construction: nothing here is an admission input (SPEC §7).
        """
        if not envelope:
            return None
        usage = envelope.get("usage") or {}
        cost = envelope.get("total_cost_usd")
        return UsageReport(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            total_cost_usd=float(cost) if cost is not None else None,
        )
