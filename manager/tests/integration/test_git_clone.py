import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from werft.domain.errors import PermanentError
from werft.runner.git import (
    GitError,
    clone_env,
    clone_workspace,
    remote_url,
    write_askpass,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not on PATH")


@pytest.fixture
def origin(tmp_path) -> tuple[str, str]:
    repo = tmp_path / "origin"
    repo.mkdir()

    def run(*args):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    run("git", "init", "-q", "--initial-branch=main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    run("git", "add", ".")
    run("git", "commit", "-qm", "first")
    run("git", "branch", "unattended")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return Path(repo).as_uri(), head


async def test_clone_checks_out_the_run_branch_and_returns_the_base_sha(origin, tmp_path):
    remote, head = origin
    dest = str(tmp_path / "workspace")

    result = await clone_workspace(
        remote=remote,
        base_branch="unattended",
        dest=dest,
        run_branch="werft/run-abc",
        env=clone_env(askpass_path=""),
    )

    assert result.base_sha == head
    assert (tmp_path / "workspace" / "README.md").read_text(encoding="utf-8") == "hello\n"
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=dest,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "werft/run-abc"


async def test_base_sha_comes_from_the_working_tree_not_a_second_lookup(origin, tmp_path):
    """If `unattended` moves between a GitHub read and the clone, a sha read
    from GitHub names a commit the workspace does not contain — and the run
    branch is then force-reset to it. Reading HEAD from the clone cannot lie."""
    remote, _ = origin
    dest = str(tmp_path / "workspace")
    result = await clone_workspace(
        remote=remote,
        base_branch="unattended",
        dest=dest,
        run_branch="werft/run-abc",
        env=clone_env(askpass_path=""),
    )
    local = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dest, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert result.base_sha == local


async def test_the_clone_sets_a_committer_identity(origin, tmp_path):
    """The agent commits inside the container; a checkout with no user.email
    fails on the first commit with an error that reads like a Werft bug."""
    remote, _ = origin
    dest = str(tmp_path / "workspace")
    await clone_workspace(
        remote=remote,
        base_branch="unattended",
        dest=dest,
        run_branch="werft/run-abc",
        env=clone_env(askpass_path=""),
    )
    email = subprocess.run(
        ["git", "config", "user.email"], cwd=dest, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert email


async def test_a_re_driven_clone_wipes_a_dirty_partial_workspace(origin, tmp_path):
    remote, _ = origin
    dest = tmp_path / "workspace"
    dest.mkdir()
    (dest / "junk").write_text("half a clone", encoding="utf-8")

    await clone_workspace(
        remote=remote,
        base_branch="unattended",
        dest=str(dest),
        run_branch="werft/run-abc",
        env=clone_env(askpass_path=""),
    )

    assert not (dest / "junk").exists()


async def test_a_missing_branch_is_permanent_not_transient(origin, tmp_path):
    """SPEC §3.2's "PermanentError pre-attempt (bad config, repo 404)", applied
    one step later: it parks rather than riding the retry ladder forever."""
    remote, _ = origin
    with pytest.raises(PermanentError):
        await clone_workspace(
            remote=remote,
            base_branch="does-not-exist",
            dest=str(tmp_path / "ws"),
            run_branch="werft/run-abc",
            env=clone_env(askpass_path=""),
        )


async def test_a_missing_remote_is_permanent(tmp_path):
    with pytest.raises(PermanentError):
        await clone_workspace(
            remote=(tmp_path / "nope").as_uri(),
            base_branch="unattended",
            dest=str(tmp_path / "ws"),
            run_branch="werft/run-abc",
            env=clone_env(askpass_path=""),
        )


async def test_a_timeout_is_a_transient_git_error(origin, tmp_path):
    remote, _ = origin
    with pytest.raises(GitError):
        await clone_workspace(
            remote=remote,
            base_branch="unattended",
            dest=str(tmp_path / "ws"),
            run_branch="werft/run-abc",
            env=clone_env(askpass_path=""),
            timeout_seconds=0.001,
        )


def test_the_remote_url_carries_only_the_non_secret_username():
    url = remote_url(github_web_url="https://github.com", owner="ken", repo="elastic")
    assert url == "https://x-access-token@github.com/ken/elastic.git"


def test_the_clone_env_never_carries_a_token_value():
    """`/proc/*/cmdline` and the environment of a process on a container host
    are both readable; the password only ever lives in a 0600 file the askpass
    reads."""
    env = clone_env(askpass_path="/x/askpass.sh")
    assert env["GIT_ASKPASS"] == "/x/askpass.sh"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert not any("ghs_" in value for value in env.values())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_askpass_is_owner_executable_and_reads_the_token_file(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    path = write_askpass(str(secrets))
    assert os.stat(path).st_mode & 0o077 == 0
    assert os.access(path, os.X_OK)
    assert "git_token" in Path(path).read_text(encoding="utf-8")
