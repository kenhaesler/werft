"""`task.json` — manager to runner (SPEC §4.3).

One schema, two consumers: the manager writes it, the in-container adapter reads
it, and both import this module. That is what keeps them from drifting.

The runner is DB-blind: this file plus the workspace is everything it knows.
"""

from pydantic import BaseModel, ConfigDict, Field


class TaskSpec(BaseModel):
    """The issue snapshot, repo, branch and config the run works from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    project_slug: str
    provider: str

    repo_remote: str
    base_branch: str
    base_sha: str
    target_branch: str

    issue_number: int
    issue_title: str
    issue_body: str = ""
    issue_labels: list[str] = Field(default_factory=list)

    #: SPEC §5: "a config value per project/work-type; this spec hardcodes no model IDs."
    model: str

    #: The adapter enforces min(this, the manager's ceiling); the manager enforces
    #: the real ceiling over the Docker API, because the adapter is not trusted (§4.3).
    timeout_seconds: int = Field(gt=0, le=90 * 60)

    #: The provider CLI's argv, built manager-side by the adapter *spec*
    #: (`providers/claude.py::build_argv`) and executed by the in-container
    #: adapter (`runners/adapter/werft_adapter/main.py`: `task.get("argv")`).
    #: It lives in the contract rather than in the image so a model or flag
    #: change is a manager deploy, not an image rebuild — and because the
    #: runner is provider-blind: this file plus the workspace is everything it
    #: knows (SPEC §4.3).
    argv: list[str] = Field(default_factory=list)
    #: Environment overlay for the CLI process (same call site:
    #: `env.update(task.get("env") or {})`). SPEC §4.4's provider credential
    #: rides here — `task.json` is itself a read-only mount, the create body
    #: has no `Env` key, and the adapter redacts these values out of the
    #: retained transcript by exact match.
    env: dict[str, str] = Field(default_factory=dict)
