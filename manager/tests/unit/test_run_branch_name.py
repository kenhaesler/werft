"""The run-branch name is a pinned, byte-exact cross-plane contract (SPEC
§6.1: `werft/run-<id>`, ephemeral, force-reset per attempt, deleted on
merge/terminal).

Three planes that never share a call stack have to agree on it exactly:
`finalize.open_pr_and_wait` names the head branch when it opens the PR,
`merge_flow` names it again to delete it, and T7's dispatcher creates and
force-resets it before a container ever starts. Two of those used to spell
the same f-string inline; `werft.domain.runs.run_branch_name` is the one
definition, and these tests are what stop it drifting back apart.
"""

import inspect
from types import SimpleNamespace
from uuid import UUID

from werft.domain.runs import run_branch_name
from werft.orchestrator import finalize, merge_flow
from werft.orchestrator.finalize import open_pr_and_wait


def test_run_branch_name_is_exactly_werft_slash_run_dash_id() -> None:
    run_id = UUID("0198f4a1-0000-7000-8000-000000000001")
    assert run_branch_name(run_id) == "werft/run-0198f4a1-0000-7000-8000-000000000001"


def test_merge_flow_falls_back_to_run_branch_name_when_no_dispatcher_recorded_one() -> None:
    """`merge_flow._branch_name` prefers a dispatcher-recorded
    `run.branch_name` (T7) and otherwise derives the same deterministic
    name — never its own f-string."""
    run_id = UUID("0198f4a1-0000-7000-8000-000000000002")
    run = SimpleNamespace(id=run_id, branch_name=None)
    assert merge_flow._branch_name(run) == run_branch_name(run_id)
    recorded = SimpleNamespace(id=run_id, branch_name="werft/run-recorded-by-t7")
    assert merge_flow._branch_name(recorded) == "werft/run-recorded-by-t7"


def test_no_production_call_site_re_spells_the_format_inline() -> None:
    """Grep-proof: neither consumer may interpolate the name itself. Prose
    mentions of `werft/run-<id>` in docstrings are fine — an *f-string*
    building one is exactly the drift this pins shut."""
    assert 'f"werft/run-' not in inspect.getsource(merge_flow)
    assert 'f"werft/run-' not in inspect.getsource(finalize)
    assert "werft/run-" not in inspect.getsource(open_pr_and_wait)
