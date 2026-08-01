"""Atomic result writing (SPEC §4.3).

`result.json` is "the adapter's LAST act, written atomically (tmp + rename)".
The manager may read the outputs directory at any moment after the die event, so
a half-written result must never be observable: the rename is what guarantees a
reader sees either the old file or the complete new one, never a prefix.
"""

import contextlib
import json
import os
import tempfile
from typing import Any


def write_json_atomic(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 — closed by the `with` below
        mode="w", encoding="utf-8", dir=directory, prefix=".result-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)  # atomic within a filesystem
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise
