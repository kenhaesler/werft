"""Reading the run's outputs directory after the container is dead (SPEC §4.3).

Completion authority is the die event + the inspected exit code + `result.json`.
This module supplies the third: it reads and validates `result.json`, and it
never trusts it — on a non-zero exit code the exit code is authoritative and a
present result is advisory only.

Everything here runs against a tree a root agent could write, so it goes through
the same discipline as `collect`: `lstat` first, regular files only, bounded read.
"""

import json
import os
import stat
from dataclasses import dataclass

from pydantic import ValidationError

from werft.contracts.result import RunResult

RESULT_FILENAME = "result.json"
LOG_FILENAME = "log.jsonl"

#: A result document is small by construction. Anything larger is not a result.
MAX_RESULT_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class OutputsRead:
    result: RunResult | None
    problem: str | None  # 'missing' | 'not_regular' | 'too_large' | 'invalid_json' | 'schema'

    @property
    def is_valid(self) -> bool:
        return self.result is not None


def read_result(outputs_dir: str) -> OutputsRead:
    """Read `result.json` defensively. Never raises on hostile content."""
    path = os.path.join(outputs_dir, RESULT_FILENAME)
    try:
        info = os.lstat(path)
    except OSError:
        return OutputsRead(None, "missing")

    if not stat.S_ISREG(info.st_mode):
        return OutputsRead(None, "not_regular")
    if info.st_size > MAX_RESULT_BYTES:
        return OutputsRead(None, "too_large")

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return OutputsRead(None, "not_regular")
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_RESULT_BYTES)

    try:
        payload = json.loads(raw)
    except ValueError:
        return OutputsRead(None, "invalid_json")

    try:
        return OutputsRead(RunResult.model_validate(payload), None)
    except ValidationError:
        return OutputsRead(None, "schema")
