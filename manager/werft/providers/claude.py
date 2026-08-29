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
from datetime import UTC, datetime

from werft.contracts.result import ResultStatus, UsageReport
from werft.contracts.task import TaskSpec
from werft.domain.attempts import AttemptOutcome
from werft.providers.base import Classification

#: Signal-death exit codes. 128+SIGTERM(15)=143 and 128+SIGKILL(9)=137.
#: Both matter: the adapter's own watchdog TERMs the tree, while the manager's
#: ceiling enforcement kills with SIGKILL over the Docker API — so treating only
#: 143 as a timeout left the manager-enforced path, the one that actually fires,
#: falling through to agent_failure.
SIGNAL_EXIT_CODES = frozenset({143, 137})

#: Account-level failures that arrive as bare stderr, with no JSON envelope at all.
#: Ordered: the first match wins, so the more specific patterns come first.
STDERR_PATTERNS: tuple[tuple[str, AttemptOutcome, ResultStatus], ...] = (
    (
        # "account" is the load-bearing noun: real-world messages route through
        # it even when the subject is nominally the org ("organization's
        # account has been disabled"). No account/organization/org alternation
        # is needed — the `permission_error` branch below covers the 403 forms
        # that phrase the refusal without ever saying "account".
        r"suspend|banned|account.*(disabled|terminated)",
        AttemptOutcome.POLICY_BLOCK,
        ResultStatus.POLICY_BLOCK,
    ),
    (
        # An entitlement/permission refusal: the credential itself is fine but
        # the plan or model access doesn't cover this request. Neither a retry
        # nor a re-login fixes it — an administrator or a plan change does, the
        # same remedy class as suspension. The API's own 403 `permission_error`
        # type is this same refusal by another name. The prose fallback is
        # deliberately narrow: "not authorized to use" only counts paired with
        # a model/plan context ("not authorized to use this model: ..."), so a
        # tool-level "not authorized to access <path>" stays agent_failure.
        r"permission_error|not authorized to use\b.{0,40}(?:model|plan)",
        AttemptOutcome.POLICY_BLOCK,
        ResultStatus.POLICY_BLOCK,
    ),
    (
        # `please run` is anchored to the Claude CLI's own re-auth prompt
        # ("Please run /login" or "Please run claude login") so a *different*
        # tool's login instructions — `please run docker login`, `gcloud auth
        # login` — fall through to agent_failure instead of masquerading as an
        # account credential problem.
        r"invalid api key|unauthoriz|authentication|not logged in"
        r"|please run\s+(?:/login|claude login)\b|oauth token.*(expired|invalid|revoked)"
        r"|credentials? (expired|invalid|not found)",
        AttemptOutcome.AUTH_FAILURE,
        ResultStatus.AUTH_FAILURE,
    ),
    (
        # Deliberately not the bare word "quota": a successful run whose stderr
        # mentions a disk quota, or an API quota it handled, or a project named
        # quota-service, would otherwise be filed as provider exhaustion and
        # would park the account.
        r"usage limit|rate limit|quota (?:exceeded|exhausted|reached)|out of quota",
        AttemptOutcome.QUOTA_EXHAUSTED,
        ResultStatus.QUOTA_EXHAUSTED,
    ),
)

#: A hard usage-limit stop delivered as envelope text. Both word orders occur in
#: the wild — "You have hit your session limit, resets 3:45pm" and "Claude usage
#: limit reached" — and matching only the verb-first form silently classified a
#: quota stop as a successful run. The subject before the verb/noun form varies
#: too — a product name, a window length ("5-hour limit reached"), or nothing
#: named at all ("You've hit your limit") — so the subject is a generic short
#: phrase rather than an enumerated word list.
#: Anchored at the start on purpose. When the CLI stops on a limit its message
#: *replaces* the result, so it always leads; whereas the `result` field
#: otherwise holds the agent's own summary, and a run that legitimately worked on
#: rate-limit handling ("added a test for when the usage limit is reached") must
#: not park the account. Anchoring is what separates the two — reinforced by the
#: run-shape gate applied at the call site (see `_looks_like_a_stop`), because an
#: agent's own summary can still happen to *open* with the same words.
USAGE_LIMIT_TEXT = re.compile(
    r"^(?:[\w][\w'-]*\s+){0,3}"
    r"(?:limit\s+(?:reached|exceeded|hit)"
    r"|(?:hit|reached|exceeded)\s+(?:your\s+)?(?:[\w][\w'-]*\s+){0,2}limit)\b",
    re.IGNORECASE,
)
RESET_TEXT = re.compile(
    r"resets?\s+(?:at\s+)?(?P<when>[0-9]{1,2}:[0-9]{2}\s*(?:am|pm)?|[0-9]{4}-[0-9]{2}-[0-9]{2}T[^\s\"]+)",
    re.IGNORECASE,
)
#: The CLI's pipe-and-epoch hard-stop form: "<message>|<unix timestamp>". Unlike
#: a wall-clock hour or a naive ISO string, a unix timestamp is unambiguous —
#: no zone to invent, no date to guess — so it is the one bare-number form that
#: is trusted as a real instant. Accepts exactly seconds (10 digits) or
#: milliseconds (13 digits) past the pipe — anything else (an 11- or 12-digit
#: run) is not a shape the CLI emits and is rejected rather than guessed at.
EPOCH_RESET = re.compile(r"\|\s*(?P<epoch>[0-9]{10}|[0-9]{13})\b")

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
            and (
                # Both spellings occur: the CLI's own shorthand and the API's
                # error-type string passed through verbatim. And HTTP 429 is
                # rate limiting by definition, whatever the free-text "error"
                # says, so a bare status code is trusted on its own too.
                event.get("error") in ("rate_limit", "rate_limit_error")
                or event.get("error_status") == 429
            )
        ):
            signals.append(event)
    return ParsedStream(result=result, rate_limit_signals=signals)


def _looks_like_a_stop(envelope: dict) -> bool:
    """Shape check for a hard limit stop, independent of the wording.

    When the CLI stops on a limit, the message *replaces* the result before
    any real work happens: the run is one or two turns and the "result" is the
    stop notice itself, not a summary. An agent that did real work and merely
    describes limits in its own summary runs many turns and produces a normal
    amount of output — `num_turns` alone separates the two by a wide margin, so
    a summary that happens to *open* with limit wording is not filed as a stop.
    Turn count is trusted only when the CLI actually reported one; its absence
    (e.g. a hand-built envelope in a test) must not block a real stop.
    """
    num_turns = envelope.get("num_turns")
    return not (num_turns is not None and num_turns > 3)


def _parse_reset(text: str) -> datetime | None:
    """Best-effort. The CLI documents no reset-time field, so ambiguity means None.

    A naive datetime is treated as ambiguous and rejected: `exhausted_until` is a
    TIMESTAMPTZ, and a value with no offset would be silently interpreted in some
    other zone — the same "invent a number" failure §7 forbids, just quieter.
    """
    text = text or ""
    epoch_match = EPOCH_RESET.search(text)
    if epoch_match:
        raw = epoch_match.group("epoch")
        seconds = int(raw) / 1000 if len(raw) == 13 else int(raw)
        try:
            instant = datetime.fromtimestamp(seconds, tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
        # No now-relative "sane window" check here on purpose: the adapter has
        # no clock of its own to judge one by, and admission already treats an
        # exhausted_until that isn't in the future as non-binding
        # (quota/admission.py: `limits.exhausted_until > now`). A stale epoch
        # is therefore harmless — it just never gates anything — whereas
        # inventing a window here would be a second, adapter-side clock
        # disagreeing with the one that actually decides admission.
        return instant

    match = RESET_TEXT.search(text)
    if not match:
        return None
    try:
        parsed = datetime.fromisoformat(match.group("when"))
    except ValueError:
        # A wall-clock string like "3:45pm" has no date and no timezone.
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


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
            # The run is unattended: there is nobody to answer a permission
            # prompt, and `acceptEdits` auto-accepts file edits ONLY — every
            # Bash call (git, the project's own test runner, package installs)
            # would block forever and burn the ceiling. The container is the
            # wall here, not the permission dialog: capable box, no route out
            # except the egress proxy, destroyed after the run (SPEC §4.2).
            "--permission-mode",
            "bypassPermissions",
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
        if exit_code in SIGNAL_EXIT_CODES:
            return Classification(
                AttemptOutcome.TIMEOUT, ResultStatus.TIMEOUT, "terminated at the run ceiling"
            )

        # 2. Account-level failures: plain stderr, no envelope. Checked BEFORE any
        #    parse handling, because filing these as parse errors silences the
        #    re-auth and quota alerts (SPEC §5).
        #    An account-level failure is defined (module docstring, rule 2) as
        #    arriving with NO JSON envelope — so a run that actually produced a
        #    clean, non-error success envelope contradicts that claim: the CLI
        #    kept going, which a suspended/unauthenticated/exhausted account
        #    cannot do. Only in that specific situation is a stderr match
        #    treated as incidental chatter (a transient retry notice, a benign
        #    warning) rather than a terminal account state. Any other exit —
        #    no envelope at all, or the process actually failed (non-zero exit,
        #    an error envelope) — keeps stderr authoritative, exactly as before.
        clean_success = (
            exit_code == 0
            and envelope is not None
            and str(envelope.get("subtype") or "") == "success"
            and not envelope.get("is_error")
        )
        if not clean_success:
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

        # 6. A hard usage-limit stop arrives as text in `result` (see the
        #    USAGE_LIMIT_TEXT comment for why the match is anchored).
        text = str(envelope.get("result") or "")
        if USAGE_LIMIT_TEXT.search(text.strip()) and _looks_like_a_stop(envelope):
            return Classification(
                AttemptOutcome.QUOTA_EXHAUSTED,
                ResultStatus.QUOTA_EXHAUSTED,
                "provider usage limit reported in the result text",
                exhausted_until=_parse_reset(text),
            )

        if subtype == "success" and not envelope.get("is_error"):
            # outcome is deliberately None. The CLI finishing cleanly is not a
            # verdict on the work: doctrine #1 says only executed CI decides, and
            # at this point no PR exists and no check has run. The orchestrator
            # writes ci_green/ci_red once the oracle reports (SPEC §3.2).
            return Classification(None, ResultStatus.SUCCESS, "subtype=success")

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
