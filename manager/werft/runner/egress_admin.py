"""Per-slot egress network math and squid allowlist file management.

T9 gives every run its own isolated Docker network so a run's containers can
only reach the internet through a slot-scoped squid proxy + dns-guard pair.
This module is the pure-function half of that: it derives the per-slot
subnet and the fixed squid/dns-guard addresses inside it
(`slot_subnet`/`slot_squid_ip`/`slot_dns_ip`), and it maintains the on-disk
squid `dstdomain` include file that drives what a slot's proxy allows
(`write_allowlist`/`clear_allowlist`). A later driver task (Part A) wires
these into the actual per-run Docker network and container lifecycle; this
module has no knowledge of Docker, the orchestrator, or config — the
`runner/` layer cannot import those (lint-imports enforces it).

`BASE_ALLOW` is always merged in: hosts a run needs regardless of what a
task's config declares (GitHub for git/gh operations, the Anthropic API for
the agent itself).

Same defensive-write discipline as `egress.py`/`collect.py`/`outputs.py` for
the allowlist file: written atomically via a temp file + `os.replace`, mode
0644. Unlike those, nothing in this file is secret — squid reads it as part
of its own config — so the directory is created 0755 (`exist_ok=True`)
rather than 0700, and write failures are allowed to raise rather than
degrading to a skip: an allowlist write is a deliberate administrative
action with a direct caller, not incidental parsing of a shared file.
"""

import contextlib
import os

BASE_ALLOW: tuple[str, ...] = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",  # release/LFS assets; the *.githubusercontent.com
    # wildcard is deliberately NOT allowed (lineage §6.7)
    "api.anthropic.com",  # provider API; extend from observed egress evidence
)


def _validate_slot(slot: int) -> None:
    if not 0 <= slot <= 255:
        raise ValueError(f"slot must be 0..255, got {slot}")


def slot_subnet(slot: int, *, prefix: str = "10.90") -> str:
    """Return the /24 subnet CIDR for `slot` (e.g. "10.90.3.0/24")."""
    _validate_slot(slot)
    return f"{prefix}.{slot}.0/24"


def slot_squid_ip(slot: int, *, prefix: str = "10.90") -> str:
    """Return the squid proxy address inside `slot`'s subnet (host .2)."""
    _validate_slot(slot)
    return f"{prefix}.{slot}.2"


def slot_dns_ip(slot: int, *, prefix: str = "10.90") -> str:
    """Return the dns-guard address inside `slot`'s subnet (host .3)."""
    _validate_slot(slot)
    return f"{prefix}.{slot}.3"


def allowlist_path(allow_dir: str, slot: int) -> str:
    """Return the path to `slot`'s squid `dstdomain` include file."""
    _validate_slot(slot)
    return os.path.join(allow_dir, f"slot{slot}.txt")


def _atomic_write(path: str, payload: bytes) -> None:
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, mode=0o755, exist_ok=True)
        if os.name != "nt":
            os.chmod(dirname, 0o755)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as out:
            out.write(payload)
        if os.name != "nt":
            os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def write_allowlist(allow_dir: str, slot: int, hosts: list[str]) -> None:
    """Write `slot`'s squid `dstdomain` include file.

    `hosts` is lowercased, deduped, merged with `BASE_ALLOW`, and sorted;
    each entry is written as one line with a leading `.` (squid's
    subdomain-inclusive `dstdomain` form: `.github.com` matches
    `github.com` and any subdomain), trailing newline. Written atomically
    (temp file + `os.replace`), file mode 0644, directory created 0755
    (`exist_ok=True`) — nothing in this file is secret, squid reads it.
    """
    path = allowlist_path(allow_dir, slot)
    merged = sorted({h.lower() for h in hosts} | set(BASE_ALLOW))
    payload = "".join(f".{h}\n" for h in merged).encode("utf-8")
    _atomic_write(path, payload)


def clear_allowlist(allow_dir: str, slot: int) -> None:
    """Write an empty allowlist file for `slot` (the egress-off state).

    An *absent* include file is a squid config error; an empty file is the
    deliberate "no extra hosts allowed" state. Written atomically like
    `write_allowlist`.
    """
    path = allowlist_path(allow_dir, slot)
    _atomic_write(path, b"")
