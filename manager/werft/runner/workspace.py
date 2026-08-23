"""The per-run directory tree the manager owns and the container mounts.

    {runs_root}/{run_id}/
        workspace/     -> /work        rw,Z   (the clone; the agent's tree)
        outputs/       -> /outputs     rw,z   (result.json, log.jsonl; T8 collects)
        secrets/       -> /run/secrets ro     (git_token, askpass.sh, prompts)
        task.json      -> /task.json   ro

`build_create_body` refuses any mount source that is not strictly under
`realpath(run_dir)`, so this layout is the thing that makes the create body
constructible at all. `{runs_root}/{run_id}/artifacts/` is deliberately never
created here: that is where T8's collector writes, and a sibling name can never
collide (carried note 5). **T7 never deletes a run directory.**

Re-preparing a run rebuilds `workspace/` and `outputs/` and nothing else. Both
are per-attempt: a re-adopted `claimed` run that inherited the previous
attempt's `result.json` would be classified from a stale file, and one that
inherited a half-clone would be committing somebody else's tree. `secrets/`
survives because its files are replaced in place, by rename.

Every write into `secrets/` is an atomic rename. The in-box `git-askpass.sh`
reads `/run/secrets/git_token` on *every* git invocation, so a re-minted token
is picked up mid-run — but only if the file is replaced whole rather than
truncated and rewritten (SPEC §4.4).
"""

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from werft.contracts.task import TaskSpec
from werft.runner.create_body import RunPlacement

SECRETS_MOUNT = "/run/secrets"
GIT_TOKEN_FILENAME = "git_token"
ASKPASS_FILENAME = "askpass.sh"
PROMPT_FILENAME = "prompt.md"
SYSTEM_PROMPT_FILENAME = "system_prompt.md"
REDACTED = "<redacted>"

DIR_MODE = 0o700
FILE_MODE = 0o600
EXEC_MODE = 0o700

#: Env names whose *values* are credentials, for the teardown scrub. Matched
#: rather than hard-coded to one provider's variable: the spec builds `env`, and
#: a second adapter naming its credential something else must still be scrubbed.
_CREDENTIAL_ENV_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD")

#: `task.json` absent, unreadable, or not JSON. Bound to a name rather than
#: written inline, because `ruff format` at this project's `target-version =
#: "py314"` rewrites an inline `except (OSError, ValueError):` into PEP 758's
#: unparenthesized `except OSError, ValueError:`. That form is valid on the
#: manager's pinned 3.14 — but it reads to a human as Python 2, and it is a
#: SyntaxError on every earlier interpreter, which is exactly the trap
#: `tests/unit/test_adapter_runtime.py::test_adapter_compiles_for_the_runner_images_python`
#: exists to catch on the 3.12 runner side. A named tuple is stable under the
#: formatter and ambiguous to nobody.
_UNREADABLE_TASK_JSON = (OSError, ValueError)


def placement_for(
    run_id: UUID | str, *, runs_root: str, dns_ip: str, proxy_url: str = ""
) -> RunPlacement:
    run_dir = os.path.join(runs_root, str(run_id))
    return RunPlacement(
        run_id=str(run_id),
        container_name=f"werft-run-{run_id}",
        network_name=f"werft-net-{run_id}",
        dns_ip=dns_ip,
        run_dir=run_dir,
        workspace_dir=os.path.join(run_dir, "workspace"),
        outputs_dir=os.path.join(run_dir, "outputs"),
        task_json_path=os.path.join(run_dir, "task.json"),
        secrets_dir=os.path.join(run_dir, "secrets"),
        proxy_url=proxy_url,
    )


def create_run_dirs(placement: RunPlacement) -> None:
    # The run dir first, and hardened like its children: `task.json` and
    # `secrets/` are only as private as the directory they are reached
    # through, and the umask default would leave that one traversable.
    os.makedirs(placement.run_dir, exist_ok=True)
    _harden(placement.run_dir, DIR_MODE)
    for path in (placement.workspace_dir, placement.outputs_dir):
        shutil.rmtree(path, ignore_errors=True)
    for path in (placement.workspace_dir, placement.outputs_dir, placement.secrets_dir):
        os.makedirs(path, exist_ok=True)
        _harden(path, DIR_MODE)


def write_task_json(placement: RunPlacement, task: TaskSpec) -> None:
    """`task.json` carries the provider credential in `env` (SPEC §4.4's
    accepted shared-credential posture), so it is 0600 on the host, ro in the
    box, and scrubbed at teardown."""
    _atomic_write(Path(placement.task_json_path), task.model_dump_json(indent=2) + "\n")


def scrub_task_json(placement: RunPlacement, *, secrets: Sequence[str]) -> None:
    """Rewrite the retained `task.json` with every secret value replaced.

    Runs at teardown and, for the paths no driver survived, from the orphan
    sweep. Never raises: it runs while other failures are already in flight.
    """
    path = Path(placement.task_json_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for secret in secrets:
        if secret:
            raw = raw.replace(json.dumps(secret)[1:-1], REDACTED)
    with contextlib.suppress(OSError):
        _atomic_write(path, raw)


def credential_values(placement: RunPlacement) -> list[str]:
    """Every credential value `task.json`'s `env` block carries, read off the
    **file** rather than off any process's memory — the secret list
    `scrub_task_json` above is meant to be handed.

    Teardown has to scrub the retained `task.json` on every path, including the
    ones that never wrote it. A re-adopted `running` run (crash-window row 6)
    prepares nothing: the driver that built that `env` died with the manager, so
    an in-memory copy of the provider credential does not exist to scrub with,
    and D7's "the retained run dir carries no live credential" would hold only
    for runs that process happened to launch itself. The file is the one thing
    both paths share, and it is the thing being scrubbed.

    Every matching value is collected, not just the first: `env` is the
    provider spec's to compose, and nothing stops a second adapter from
    carrying two.
    """
    try:
        payload = json.loads(Path(placement.task_json_path).read_text(encoding="utf-8"))
    except _UNREADABLE_TASK_JSON:
        return []
    env = payload.get("env") if isinstance(payload, dict) else None
    if not isinstance(env, dict):
        return []
    return [
        value
        for name, value in env.items()
        if isinstance(value, str)
        and any(marker in str(name).upper() for marker in _CREDENTIAL_ENV_MARKERS)
    ]


def write_secret(placement: RunPlacement, filename: str, value: str) -> None:
    _atomic_write(Path(placement.secrets_dir) / filename, value)


def remove_secrets(placement: RunPlacement) -> None:
    """Teardown hygiene: the token is revoked *and* its file is gone."""
    with contextlib.suppress(OSError):
        for entry in Path(placement.secrets_dir).iterdir():
            with contextlib.suppress(OSError):
                if entry.is_file():
                    entry.unlink()


def in_box(filename: str) -> str:
    return f"{SECRETS_MOUNT}/{filename}"


def build_prompt(task: TaskSpec) -> str:
    return (
        f"# {task.issue_title}\n\n"
        f"GitHub issue #{task.issue_number} in `{task.project_slug}`.\n\n"
        f"{task.issue_body}\n\n"
        "---\n\n"
        f"Work in `/work`: a clone of the repository on branch "
        f"`{task.target_branch}`, force-reset to `{task.base_branch}` at "
        f"`{task.base_sha}` at the start of this attempt.\n"
        "When the work is done, commit it and push that branch to `origin`. "
        "Nothing you write is accepted until it is pushed: Werft opens the pull "
        "request from the branch, not from your files, and either an executed "
        "check or the operator decides whether it lands.\n"
        f"Do not open a pull request yourself, and never push to "
        f"`{task.base_branch}` or `main`.\n"
    )


def build_system_prompt(task: TaskSpec) -> str:
    return (
        "You are running unattended inside a Werft runner container.\n\n"
        f"- Your branch is `{task.target_branch}`; its base is `{task.base_branch}`.\n"
        "- Nothing you say decides whether this work lands: an executed check, or "
        "the operator's explicit acceptance, does. Write the tests that let it pass.\n"
        "- The box is yours and disposable: install what you need, run what you "
        "build. Everything outside `/work` is gone when the run ends.\n"
        "- Evidence is collected. Put reports, screenshots and traces under "
        "`/work/.werft-artifacts/`.\n"
        "- There is no human to answer questions mid-run and no interactive "
        "prompt. If a decision is ambiguous, take the smaller reversible option "
        "and say so in the commit message; if the task is impossible, commit what "
        "you have and explain why in your final message.\n"
    )


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _harden(Path(tmp), FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _harden(path: Path | str, mode: int) -> None:
    """POSIX-only. Windows ignores these bits; the dev box is not the
    deployment target and the tests skip the assertion there."""
    if os.name == "nt":
        return
    os.chmod(path, mode)
