"""Exact-value secret redaction for the log tee (SPEC §4.4).

SPEC §4.4: "Redaction handles both `ghs_` token formats (36-char and
`ghs_APPID_JWT` long form) **by exact value, not regex shape**."

That wording is load-bearing. GitHub's installation-token format changed in 2026
from `ghs_` + 36 chars to a variable-length `ghs_APPID_JWT` form of ~520 chars,
and GitHub itself names `ghs_[A-Za-z0-9]{36}` as a pattern that now fails. A
redactor that silently stops matching is worse than none, so this module never
pattern-matches: it replaces the literal secret values it was handed. A format
change cannot outrun it.

This is hygiene, not containment. A root agent can kill or patch this process;
the real mitigation is credential scoping (SPEC §4.3/§4.4).
"""

PLACEHOLDER = "[redacted]"


class Redactor:
    """Replaces known secret values with a placeholder, longest first."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets = self._clean(secrets or [])

    @staticmethod
    def _clean(secrets: list[str]) -> list[str]:
        # Longest first so a token that contains a shorter one is not left
        # half-redacted. Very short values are ignored: redacting a 3-character
        # string would shred the log without protecting anything.
        cleaned = {s.strip() for s in secrets if s and len(s.strip()) >= 8}
        return sorted(cleaned, key=len, reverse=True)

    def add(self, secret: str) -> None:
        """Register a newly minted token (the manager re-mints before expiry)."""
        self._secrets = self._clean([*self._secrets, secret])

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, PLACEHOLDER)
        return text
