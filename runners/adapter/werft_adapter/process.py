"""Process supervision: own process group, tree-kill, reap to ECHILD.

[BP-P3.3]: PID 1, setsid, tree-kill (killpg TERM -> 10 s -> KILL), blocking
reap to ECHILD. No zombie outlives the container and no external reaper is
needed, because none exists inside the box.

Hygiene, not containment - see the package docstring.

Portability note, and it is load-bearing: this package runs on the runner
image's interpreter (python3.12), not on the manager's 3.14. Python 3.14
accepts unparenthesized `except A, B:` via PEP 758 and 3.12 does not, so
syntax that "works locally" can still be a SyntaxError at PID 1 inside the
container. tests/unit/test_adapter_runtime.py compiles this package against
the container's feature version to keep that from shipping again.
"""

import contextlib
import errno
import os
import signal
import subprocess
import time
from collections.abc import Callable

TERM_GRACE_SECONDS = 10.0


def start_in_own_process_group(
    argv: list[str], env: dict[str, str], cwd: str, *, input_pipe: bool = False
) -> subprocess.Popen:
    """Launch the CLI as the leader of a new process group.

    The group is what makes tree-kill possible: a CLI that spawns a build which
    spawns a test runner leaves a tree, and killing only the direct child would
    leave the rest running until the container is destroyed.

    Pipes decode with errors="replace": a single invalid byte from the CLI or
    anything it shells out to would otherwise raise UnicodeDecodeError out of
    the read loop and kill the adapter mid-run.
    """
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
        "env": env,
        "bufsize": 1,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if input_pipe:
        kwargs["stdin"] = subprocess.PIPE
    if hasattr(os, "setsid"):
        kwargs["preexec_fn"] = os.setsid  # noqa: PLW1509 - a new session is the point
    return subprocess.Popen(argv, **kwargs)


def tree_kill(process: subprocess.Popen, *, grace_seconds: float = TERM_GRACE_SECONDS) -> None:
    """TERM the whole group, wait out the grace period, then KILL it."""
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except (OSError, AttributeError):
        process.terminate()
        return

    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)
    _signal_group(pgid, signal.SIGKILL)


def _signal_group(pgid: int, sig: int) -> None:
    # A group that has already exited is the success case, not an error.
    with contextlib.suppress(OSError, AttributeError):
        os.killpg(pgid, sig)


def reap_until_echild(max_iterations: int = 10_000) -> int:
    """Reap every remaining child. Returns how many were collected.

    PID 1 inherits orphans; without this they accumulate as zombies for the life
    of the container. Zombies are a POSIX concept and this code only ever runs as
    PID 1 inside a Linux runner, so on a non-POSIX host there is nothing to reap.
    """
    if not hasattr(os, "WNOHANG"):
        return 0

    reaped = 0
    for _ in range(max_iterations):
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except OSError as exc:
            if exc.errno == errno.ECHILD:
                return reaped
            raise
        if pid == 0:
            return reaped
        reaped += 1
    return reaped


def install_signal_forwarding(process_getter: Callable[[], subprocess.Popen | None]) -> None:
    """Forward TERM/INT to the child group so a manager kill is not silently absorbed."""

    def handler(signum: int, _frame: object) -> None:
        process = process_getter()
        if process is not None:
            tree_kill(process)
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Not every signal is settable off the main thread or on every platform.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)
