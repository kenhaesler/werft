"""Bounded, format-agnostic subnet filter over a shared proxy/DNS access log.

The manager does not control the log's format — squid and dns-guard land in
T9, and other loggers may join later. Rather than parse each format, this
module treats the log as opaque text and keeps a line whenever *any*
whitespace-separated-ish token on it parses as an IP address inside one of
the run's subnets (plan decision D6). That is deliberately permissive: it
will occasionally keep a line because an unrelated numeric field happens to
parse as an address in-range, but it can never silently miss a hit because a
new log format shows up.

Same defensive discipline as `collect.py` / `outputs.py`: the log is a
shared file this process does not own exclusively, so it is read via
`lstat` + `S_ISREG` + `O_NOFOLLOW`, and any `OSError` degrades to a skip
(`None`) rather than raising. Read is bounded to a tail window
(`max_scan_bytes`, D8) so an unbounded shared log can never turn evidence
collection into an unbounded read; output is bounded too
(`max_out_bytes`), dropping the *oldest* matched lines first when the
matched set does not fit, since the newest activity is the most likely to
matter to whatever triggered evidence collection.
"""

import contextlib
import ipaddress
import os
import re
import stat

MAX_SCAN_BYTES = 32 * 1024 * 1024  # read window from the tail of a shared log
#: == collect.MAX_FILE_BYTES, restated here to avoid the import-cycle question.
MAX_OUT_BYTES = 25 * 1024 * 1024

_CANDIDATE_TOKEN = re.compile(r"[0-9a-fA-F:.]+")
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_subnets(subnets: list[str]) -> list[_Network]:
    parsed = []
    for s in subnets:
        try:
            parsed.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            continue
    return parsed


def _line_matches(line: bytes, networks: list[_Network]) -> bool:
    text = line.decode("utf-8", errors="replace")
    for token in _CANDIDATE_TOKEN.findall(text):
        try:
            addr = ipaddress.ip_address(token)
        except ValueError:
            continue
        for net in networks:
            if addr in net:
                return True
    return False


def extract_egress_lines(
    log_path: str,
    subnets: list[str],
    dest_path: str,
    *,
    max_scan_bytes: int = MAX_SCAN_BYTES,
    max_out_bytes: int = MAX_OUT_BYTES,
) -> int | None:
    """Filter `log_path` down to lines attributable to `subnets`, write to `dest_path`.

    Returns bytes written, `0` if the log was readable but held no matching
    line (no file is created on 0), or `None` when skipped (empty/missing/
    unreadable/non-regular `log_path`, or empty `subnets`). Never raises.

    Note the distinction in the "empty" cases: an empty `log_path` *string*
    (unconfigured, D7) is what returns `None` here; a `log_path` that points
    at a real, readable, zero-byte file is not skipped — it is read, found to
    hold no matching line, and returns `0` like any other no-match log. And a
    non-empty `subnets` list where every entry fails to parse is treated the
    same as an empty `subnets` list — no usable subnet means there is nothing
    to filter on, so this returns `None` rather than `0`.
    """
    if not subnets:
        return None

    networks = _parse_subnets(subnets)
    if not networks:
        return None

    try:
        info = os.lstat(log_path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None

    try:
        fd = os.open(log_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None

    try:
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - max_scan_bytes)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return None

    if start > 0 and b"\n" in raw:
        raw = raw.split(b"\n", 1)[1]
    elif start > 0:
        raw = b""

    lines = raw.splitlines(keepends=True)
    matched = [line for line in lines if _line_matches(line, networks)]

    if not matched:
        return 0

    # Front-truncate to the newest `max_out_bytes`. Computed as a single cut
    # index walked from the end, then one slice: the obvious
    # `while total > cap: matched.pop(0)` is O(n^2) (every `pop(0)` shifts the
    # whole list), and this runs on the orchestrator's only event loop against
    # a *shared* proxy log, so a busy rig turns it into minutes of stall.
    kept_bytes = 0
    cut = 0
    for index in range(len(matched) - 1, -1, -1):
        length = len(matched[index])
        if kept_bytes + length > max_out_bytes:
            cut = index + 1
            break
        kept_bytes += length
    matched = matched[cut:]

    if not matched:
        return 0

    payload = b"".join(matched)

    dirname = os.path.dirname(dest_path)
    tmp_path = dest_path + ".tmp"
    try:
        if dirname:
            # 0700 like every other directory Werft creates under a run
            # (`workspace.create_run_dirs`): the staged extract holds whatever
            # the run's traffic revealed, and the umask default would leave it
            # world-traversable.
            os.makedirs(dirname, mode=0o700, exist_ok=True)
            if os.name != "nt":
                os.chmod(dirname, 0o700)
        with open(tmp_path, "wb") as out:
            out.write(payload)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, dest_path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        return None

    return len(payload)
