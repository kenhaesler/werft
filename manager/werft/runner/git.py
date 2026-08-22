"""The manager's own git, for the pre-container clone (SPEC §4.3: "the manager
clones the repo into the run workspace **before** container start").

Doing it here removes the clone-failure class from the adapter entirely: a
network failure, a bad base branch or a 404 becomes a manager-side error with a
typed outcome, instead of an exit code the agent's own box reported.

DB-blind by construction (import-linter forbids `werft.db` here) and
credential-safe by construction: the token never appears in `argv`, because
`/proc/<pid>/cmdline` is world-readable on a host that also runs containers.
The remote URL carries only the non-secret username `x-access-token@`; the
password comes from `GIT_ASKPASS`, a 0700 script that `cat`s the same
`secrets/git_token` file the in-container askpass reads — so the manager and
the runner authenticate identically, from one file, re-minted by rename.

A full clone of one branch (`--single-branch --no-tags`), not `--depth 1`: the
agent pushes from this clone, and a push from a shallow clone is an avoidable
edge case at this scale. `base_sha` is read from the working tree rather than
from a second GitHub lookup, which kills the moved-branch race outright.

Error taxonomy: a repository or branch that does not exist is a
`PermanentError` (the run parks); everything else — network, disk, a git
crash, a timeout — is a `GitError` (`TransientError`) and rides the retry
ladder as `infra_failure`.
"""

import asyncio
import os
import shutil
from dataclasses import dataclass

from werft.domain.errors import PermanentError, TransientError
from werft.runner.workspace import ASKPASS_FILENAME, GIT_TOKEN_FILENAME

COMMIT_NAME = "werft"
COMMIT_EMAIL = "werft@localhost"

_PERMANENT_STDERR_MARKERS = (
    "repository not found",
    "does not appear to be a git repository",
    "remote branch",
    "not found in upstream origin",
    "could not read username",
    "authentication failed",
)


class GitError(TransientError):
    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(command[1:3])} failed ({returncode}): {stderr.strip()[:400]}"
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class CloneResult:
    base_sha: str


def remote_url(*, github_web_url: str, owner: str, repo: str) -> str:
    host = github_web_url.split("://", 1)[-1].rstrip("/")
    return f"https://x-access-token@{host}/{owner}/{repo}.git"


def write_askpass(secrets_dir: str) -> str:
    """The runner's exact pattern, on the manager side: one file, read fresh on
    every git invocation, so a re-mint by rename is picked up with no restart.
    Git-for-Windows executes a `.sh` askpass through its bundled shell, so the
    same file works on the dev box and on the rig."""
    path = os.path.join(secrets_dir, ASKPASS_FILENAME)
    token_path = os.path.join(secrets_dir, GIT_TOKEN_FILENAME)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f'#!/bin/sh\ncat "{token_path}"\n')
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def clone_env(*, askpass_path: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_ASKPASS": askpass_path,
            # A prompt in an unattended process is a hang, not a question.
            "GIT_TERMINAL_PROMPT": "0",
            # No /etc/gitconfig on the host may influence a run's clone.
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


async def clone_workspace(
    *,
    remote: str,
    base_branch: str,
    dest: str,
    run_branch: str,
    env: dict[str, str],
    timeout_seconds: float = 600.0,
) -> CloneResult:
    shutil.rmtree(dest, ignore_errors=True)
    await _git(
        [
            "git",
            "clone",
            "--branch",
            base_branch,
            "--single-branch",
            "--no-tags",
            "--",
            remote,
            dest,
        ],
        cwd=None,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    sha = (
        await _git(["git", "rev-parse", "HEAD"], cwd=dest, env=env, timeout_seconds=timeout_seconds)
    ).strip()
    for argv in (
        ["git", "config", "user.name", COMMIT_NAME],
        ["git", "config", "user.email", COMMIT_EMAIL],
        # `-B`: create or force-reset. SPEC §3.2's only dispatch behavior is
        # `retry` — "force-reset branch to base" — so a re-attempt must not
        # inherit the previous attempt's commits.
        ["git", "checkout", "-q", "-B", run_branch],
    ):
        await _git(argv, cwd=dest, env=env, timeout_seconds=timeout_seconds)
    return CloneResult(base_sha=sha)


async def _git(
    command: list[str], *, cwd: str | None, env: dict[str, str], timeout_seconds: float
) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise GitError(command, -1, f"timed out after {timeout_seconds}s") from None
    if process.returncode != 0:
        text = stderr.decode("utf-8", errors="replace")
        if any(marker in text.lower() for marker in _PERMANENT_STDERR_MARKERS):
            raise PermanentError(f"git {command[1]} failed permanently: {text.strip()[:400]}")
        raise GitError(command, process.returncode or -1, text)
    return stdout.decode("utf-8", errors="replace")
