"""Error taxonomy (lineage A§5.3): one retry decision point, typed."""


class WerftError(Exception):
    """Base for all Werft-raised errors."""


class TransientError(WerftError):
    """Retryable: backoff and try again."""


class ProviderError(WerftError):
    """Provider CLI / account-level failure."""


class QuotaExhaustedError(ProviderError):
    """Provider window exhausted; never consumes retry budget (SPEC §3.2)."""


class GitConflictError(WerftError):
    """Merge/rebase conflict; parks for a human."""


class PermanentError(WerftError):
    """Not retryable ever: bad config, repo 404. Parks without an attempt."""
