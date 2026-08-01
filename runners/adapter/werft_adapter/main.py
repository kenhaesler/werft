"""Adapter entry point: PID 1 inside the runner (SPEC §4.3).

    task.json (ro)  ->  provider CLI  ->  /outputs/{log.jsonl,result.json}, exit code

The exit code is the contract tier; `result.json.status` is the task outcome.
"They finished" and "it succeeded" are never one scalar — conflating them is
what v1's judgment gate exploited.

Hygiene, not containment: see the package docstring.
"""

import json
import os
import sys
import threading
from collections import deque
from datetime import UTC, datetime

from werft_adapter import (
    EXIT_CLI_UNSTARTABLE,
    EXIT_CONTRACT_FULFILLED,
    EXIT_RESULT_SERIALIZATION_FAILURE,
    EXIT_WORKSPACE_GIT_FAILURE,
)
from werft_adapter.atomic import write_json_atomic
from werft_adapter.process import reap_until_echild, start_in_own_process_group, tree_kill
from werft_adapter.redact import Redactor

TASK_PATH = "/task.json"
OUTPUTS_DIR = "/outputs"
WORKSPACE = "/work"
GIT_TOKEN_PATH = "/run/secrets/git_token"


def _read_secrets() -> list[str]:
    """Values to strip from the transcript. Read by value, never matched by shape."""
    secrets: list[str] = []
    for path in (GIT_TOKEN_PATH,):
        try:
            with open(path, encoding="utf-8") as handle:
                secrets.append(handle.read().strip())
        except OSError:
            continue
    return secrets


def run_cli(
    argv: list[str],
    env: dict[str, str],
    *,
    log_path: str,
    ceiling_seconds: float,
    secrets: list[str] | None = None,
) -> tuple[int, str]:
    """Run the CLI, tee a redacted transcript, return (exit code, stderr tail).

    stderr is drained by its own thread rather than after stdout: a child that
    fills the 64 KiB stderr pipe buffer while we are blocked reading stdout would
    deadlock, and `claude` writes account-level failures to stderr — precisely
    the case the classifier depends on.

    The ceiling likewise runs on a watchdog thread, because a child that prints
    nothing at all never advances a deadline check placed in the stdout loop.
    (Hygiene only — the manager enforces the real ceiling over the Docker API.)
    """
    redact = Redactor(secrets or [])
    stderr_tail: deque[str] = deque(maxlen=50)

    try:
        process = start_in_own_process_group(argv, env, WORKSPACE)
    except OSError as exc:
        return EXIT_CLI_UNSTARTABLE, f"could not start {argv[0]!r}: {exc}"

    def drain_stderr() -> None:
        for line in process.stderr or ():
            stderr_tail.append(redact(line))

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    finished = threading.Event()

    def watchdog() -> None:
        if not finished.wait(ceiling_seconds):
            tree_kill(process)

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        with open(log_path, "a", encoding="utf-8") as log:
            for line in process.stdout or ():
                log.write(redact(line))
                log.flush()
        exit_code = process.wait()
        finished.set()
        stderr_thread.join(timeout=5)
        return exit_code, "".join(stderr_tail)
    finally:
        finished.set()
        tree_kill(process)
        reap_until_echild()


def main(argv: list[str] | None = None) -> int:
    started = datetime.now(UTC)
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    try:
        with open(TASK_PATH, encoding="utf-8") as handle:
            task = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"unreadable {TASK_PATH}: {exc}\n")
        return EXIT_WORKSPACE_GIT_FAILURE

    cli_argv = list(argv or task.get("argv") or [])
    if not cli_argv:
        sys.stderr.write("task.json carries no argv for the provider CLI\n")
        return EXIT_CLI_UNSTARTABLE

    env = dict(os.environ)
    env.update(task.get("env") or {})

    exit_code, stderr_tail = run_cli(
        cli_argv,
        env,
        log_path=os.path.join(OUTPUTS_DIR, "log.jsonl"),
        ceiling_seconds=float(task.get("timeout_seconds", 5400)),
        secrets=_read_secrets(),
    )

    ended = datetime.now(UTC)
    try:
        write_json_atomic(
            os.path.join(OUTPUTS_DIR, "result.json"),
            {
                # The manager classifies from the transcript and stderr; the
                # adapter records facts and never judges the outcome itself.
                "status": "success" if exit_code == 0 else "failure",
                "pushed": False,
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "duration_seconds": (ended - started).total_seconds(),
                "error": None if exit_code == 0 else {"code": "cli_failed", "message": stderr_tail},
            },
        )
    except Exception as exc:  # noqa: BLE001 — this is the serialization tier
        sys.stderr.write(f"could not serialise result.json: {exc}\n")
        return EXIT_RESULT_SERIALIZATION_FAILURE

    return EXIT_CONTRACT_FULFILLED if exit_code == 0 else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
