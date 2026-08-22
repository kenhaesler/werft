import json
import os
import sys
import uuid

import pytest

from werft.contracts.task import TaskSpec
from werft.runner.create_body import ProjectRunnerConfig, build_create_body
from werft.runner.workspace import (
    GIT_TOKEN_FILENAME,
    PROMPT_FILENAME,
    build_prompt,
    build_system_prompt,
    create_run_dirs,
    in_box,
    placement_for,
    remove_secrets,
    scrub_task_json,
    write_secret,
    write_task_json,
)

RUN_ID = uuid.UUID("0198f000-0000-7000-8000-000000000001")
DIGEST = "werft-runner-elastic@sha256:" + "c" * 64


def task_spec(**over) -> TaskSpec:
    base = dict(
        run_id=str(RUN_ID),
        project_slug="elastic",
        provider="claude",
        repo_remote="https://github.com/ken/elastic.git",
        base_branch="unattended",
        base_sha="a" * 40,
        target_branch=f"werft/run-{RUN_ID}",
        issue_number=12,
        issue_title="build the harness",
        issue_body="details",
        model="claude-sonnet-4-6",
        timeout_seconds=5400,
    )
    return TaskSpec(**(base | over))


def test_every_path_is_strictly_under_the_run_dir(tmp_path):
    """`build_create_body` refuses any mount source that is not strictly under
    `realpath(run_dir)` — this is the producer side of that contract."""
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="10.90.7.53")
    create_run_dirs(placement)
    write_task_json(placement, task_spec())

    body = build_create_body(
        placement,
        ProjectRunnerConfig(image_digest=DIGEST, memory_bytes=2 << 30, nano_cpus=1_000_000_000),
        entrypoint=["/opt/werft/adapter/bin/werft-adapter"],
    )

    assert body["Labels"] == {"werft.run_id": str(RUN_ID)}
    assert body["HostConfig"]["Dns"] == ["10.90.7.53"]
    assert body["HostConfig"]["Binds"][0].endswith(":/work:rw,Z")
    assert any(b.endswith(":/run/secrets:ro") for b in body["HostConfig"]["Binds"])
    assert placement.container_name == f"werft-run-{RUN_ID}"
    assert placement.network_name == f"werft-net-{RUN_ID}"


def test_preparing_again_rebuilds_workspace_and_outputs_but_never_the_run_dir(tmp_path):
    """A re-adopted `claimed` run must not inherit a half-clone *or* a previous
    attempt's result.json — it would be classified from a stale file. The run
    directory itself is never removed: T8's collector reads this tree, and
    `artifacts/` is its sibling (carried note 5)."""
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)
    open(os.path.join(placement.workspace_dir, "half-clone"), "w").close()
    open(os.path.join(placement.outputs_dir, "result.json"), "w").close()
    keepsake = os.path.join(placement.run_dir, "artifacts")
    os.makedirs(keepsake)

    create_run_dirs(placement)

    assert os.listdir(placement.workspace_dir) == []
    assert os.listdir(placement.outputs_dir) == []
    assert os.path.isdir(keepsake)  # T8's tree survives a re-prepare
    assert os.path.isdir(placement.secrets_dir)


def test_task_json_carries_the_keys_the_adapter_actually_reads(tmp_path):
    """`runners/adapter/werft_adapter/main.py` reads `argv`, `env` and
    `timeout_seconds` off task.json; `TaskSpec` forbade extras and declared
    none of the first two. This is that drift, closed (decision 18)."""
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)
    task = task_spec(
        argv=["claude", "-p", "@/run/secrets/prompt.md"],
        env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-test", "CI": "true"},
    )

    write_task_json(placement, task)

    with open(placement.task_json_path, encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["argv"] == ["claude", "-p", "@/run/secrets/prompt.md"]
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-test"
    assert payload["timeout_seconds"] == 5400
    assert payload["base_sha"] == task.base_sha


def test_the_scrub_replaces_secret_values_and_leaves_the_file_readable(tmp_path):
    """D7: SPEC §8 retains this tree and restic ships it offsite. A live OAuth
    token must not survive the run that used it."""
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)
    write_task_json(
        placement, task_spec(argv=["claude", "-p"], env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-test"})
    )

    scrub_task_json(placement, secrets=["sk-test", "ghs_run_token"])

    with open(placement.task_json_path, encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "<redacted>"
    assert payload["argv"] == ["claude", "-p"]
    assert payload["run_id"] == str(RUN_ID)


def test_scrubbing_a_missing_file_is_a_no_op(tmp_path):
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)
    scrub_task_json(placement, secrets=["sk-test"])  # must not raise


def test_a_secret_write_is_a_rename_so_the_in_box_askpass_sees_the_new_value(tmp_path):
    """SPEC §4.4: the adapter's `git-askpass.sh` reads `/run/secrets/git_token`
    on *every* git invocation, so a truncate-then-write leaves a window where a
    concurrent read gets an empty password."""
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)

    write_secret(placement, GIT_TOKEN_FILENAME, "ghs_one")
    write_secret(placement, GIT_TOKEN_FILENAME, "ghs_two")

    token_path = os.path.join(placement.secrets_dir, GIT_TOKEN_FILENAME)
    with open(token_path, encoding="utf-8") as f:
        assert f.read() == "ghs_two"
    assert [n for n in os.listdir(placement.secrets_dir) if n.startswith(".")] == []

    remove_secrets(placement)
    assert os.listdir(placement.secrets_dir) == []
    remove_secrets(placement)  # idempotent, never raises


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_secrets_and_task_json_are_not_group_or_world_readable(tmp_path):
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)
    write_task_json(placement, task_spec(env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-test"}))
    write_secret(placement, GIT_TOKEN_FILENAME, "ghs_one")

    assert os.stat(placement.task_json_path).st_mode & 0o077 == 0
    assert os.stat(os.path.join(placement.secrets_dir, GIT_TOKEN_FILENAME)).st_mode & 0o077 == 0
    assert os.stat(placement.secrets_dir).st_mode & 0o077 == 0
    # The run dir itself: everything above is only as private as the directory
    # it is reached through.
    assert os.stat(placement.run_dir).st_mode & 0o077 == 0


def test_prompts_go_to_the_read_only_mount_not_the_git_tree(tmp_path):
    """`/work` is the tree the agent is about to commit; a prompt file there
    would dirty its own `git status` and could be committed into the run's PR."""
    placement = placement_for(RUN_ID, runs_root=str(tmp_path), dns_ip="")
    create_run_dirs(placement)
    write_secret(placement, PROMPT_FILENAME, "p")

    assert in_box(PROMPT_FILENAME) == "/run/secrets/prompt.md"
    assert os.listdir(placement.workspace_dir) == []


def test_the_prompt_names_the_issue_the_branch_and_the_artifacts_dir():
    task = task_spec()
    prompt, system = build_prompt(task), build_system_prompt(task)

    assert "#12" in prompt and task.issue_title in prompt and "details" in prompt
    assert task.target_branch in prompt
    assert task.base_branch in system
    assert ".werft-artifacts/" in system
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in prompt + system  # no secret in a prompt, ever
