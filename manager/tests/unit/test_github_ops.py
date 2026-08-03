"""Protocol-level tests for `RepoOps` (SPEC §8 GitHub integration,
`werft/github/ops.py`): ref/PR/check/label/protection request shapes and
bodies, the two adopt-on-422 fallbacks, the typed merge errors, and the
`oracle_check` status/conclusion mapping table — all against a small fake
transport that enqueues canned `(status, body)` responses and records every
request, following `test_github_client.py`'s `httpx.MockTransport` style.
"""

import json
from typing import Any

import httpx
import pytest

from werft.domain.errors import PermanentError
from werft.github.client import ConditionalResult, GitHubClient
from werft.github.ops import CheckState, MergeBlocked, MergeShaMismatch, RepoOps

API_URL = "https://api.github.test"
OWNER = "ken"
REPO = "widgets"

PR_JSON: dict[str, Any] = {
    "number": 42,
    "state": "open",
    "merged": False,
    "head": {"ref": "werft/run-abc", "sha": "sha-head-1"},
    "base": {"ref": "unattended"},
    "mergeable": None,
    "mergeable_state": "unknown",
    "html_url": "https://github.com/ken/widgets/pull/42",
}


class MockTransport:
    """Records every request in arrival order; replays one canned
    `httpx.Response` per request, FIFO. Enqueue with `.enqueue(status,
    body)`; inspect sent requests (method, path, params, JSON body) via
    `.requests`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._queue: list[httpx.Response] = []

    def enqueue(self, status_code: int, body: Any = None) -> None:
        response = (
            httpx.Response(status_code) if body is None else httpx.Response(status_code, json=body)
        )
        self._queue.append(response)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._queue.pop(0)

    @property
    def httpx_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


@pytest.fixture
def transport() -> MockTransport:
    return MockTransport()


@pytest.fixture
def ops(transport: MockTransport) -> RepoOps:
    async def token_provider() -> str:
        return "ghs_test"

    http = httpx.AsyncClient(transport=transport.httpx_transport)
    client = GitHubClient(http, api_url=API_URL, token_provider=token_provider)
    return RepoOps(client, OWNER, REPO)


# -- refs -------------------------------------------------------------


async def test_get_ref_sha_returns_sha_when_present(ops, transport):
    transport.enqueue(200, {"object": {"sha": "sha-1"}})
    assert await ops.get_ref_sha("unattended") == "sha-1"
    assert transport.requests[0].url.path.endswith("/git/ref/heads/unattended")


async def test_get_ref_sha_returns_none_on_404(ops, transport):
    transport.enqueue(404, {"message": "Not Found"})
    assert await ops.get_ref_sha("missing") is None


async def test_ensure_branch_creates_when_absent(ops, transport):
    transport.enqueue(201, {"ref": "refs/heads/werft/run-1", "object": {"sha": "sha-new"}})
    sha = await ops.ensure_branch("werft/run-1", "sha-base")
    assert sha == "sha-new"
    body = json.loads(transport.requests[0].content)
    assert body == {"ref": "refs/heads/werft/run-1", "sha": "sha-base"}


async def test_ensure_branch_adopts_existing_sha_on_422(ops, transport):
    transport.enqueue(422, {"message": "Reference already exists"})
    transport.enqueue(200, {"object": {"sha": "sha-existing"}})
    sha = await ops.ensure_branch("werft/run-1", "sha-base")
    assert sha == "sha-existing"
    assert transport.requests[1].url.path.endswith("/git/ref/heads/werft/run-1")


async def test_ensure_branch_raises_permanent_error_when_422_but_ref_not_found(ops, transport):
    transport.enqueue(422, {"message": "Reference already exists"})
    transport.enqueue(404, {"message": "Not Found"})
    with pytest.raises(PermanentError):
        await ops.ensure_branch("werft/run-1", "sha-base")


async def test_force_reset_ref_sends_sha_and_force_true(ops, transport):
    transport.enqueue(200, {})
    await ops.force_reset_ref("werft/run-1", "sha-x")
    request = transport.requests[0]
    assert request.method == "PATCH"
    assert request.url.path.endswith("/git/refs/heads/werft/run-1")
    assert json.loads(request.content) == {"sha": "sha-x", "force": True}


async def test_delete_ref_sends_delete_request(ops, transport):
    transport.enqueue(204)
    await ops.delete_ref("werft/run-1")
    assert transport.requests[0].method == "DELETE"
    assert transport.requests[0].url.path.endswith("/git/refs/heads/werft/run-1")


async def test_delete_ref_swallows_404(ops, transport):
    transport.enqueue(404, {"message": "Not Found"})
    await ops.delete_ref("werft/run-1")  # must not raise


# -- pull requests ------------------------------------------------------


async def test_open_pr_returns_parsed_pr_on_201(ops, transport):
    transport.enqueue(201, PR_JSON)
    pr = await ops.open_pr("werft/run-abc", "unattended", "t", "b")
    assert pr.number == PR_JSON["number"]
    assert pr.head_ref == PR_JSON["head"]["ref"]
    assert pr.head_sha == PR_JSON["head"]["sha"]
    assert pr.base_ref == PR_JSON["base"]["ref"]
    body = json.loads(transport.requests[0].content)
    assert body == {"head": "werft/run-abc", "base": "unattended", "title": "t", "body": "b"}


async def test_open_pr_adopts_on_422(ops, transport):
    transport.enqueue(
        422,
        {
            "message": "Validation Failed",
            "errors": [
                {
                    "resource": "PullRequest",
                    "code": "custom",
                    "message": "A pull request already exists for ken:werft/run-abc.",
                }
            ],
        },
    )
    transport.enqueue(200, [PR_JSON])  # list by head+base finds the survivor
    pr = await ops.open_pr("werft/run-abc", "unattended", "t", "b")
    assert pr.number == PR_JSON["number"]
    assert transport.requests[1].url.params["head"] == "ken:werft/run-abc"


async def test_open_pr_raises_permanent_error_when_adopt_list_is_empty(ops, transport):
    transport.enqueue(422, {"message": "Validation Failed"})
    transport.enqueue(200, [])
    with pytest.raises(PermanentError):
        await ops.open_pr("werft/run-abc", "unattended", "t", "b")


async def test_get_pr_returns_parsed_pr(ops, transport):
    transport.enqueue(200, PR_JSON)
    pr = await ops.get_pr(PR_JSON["number"])
    assert pr.state == PR_JSON["state"]
    assert pr.mergeable is None
    assert pr.mergeable_state == "unknown"


async def test_get_pr_returns_none_on_404(ops, transport):
    transport.enqueue(404, {"message": "Not Found"})
    assert await ops.get_pr(999) is None


async def test_close_pr_sends_patch_with_closed_state(ops, transport):
    # GitHub's update endpoint is idempotent on `state`: closing an
    # already-closed PR still returns 200, so this same single call is
    # also the "already-closed -> no-op" behavior, with no pre-read needed.
    transport.enqueue(200, {**PR_JSON, "state": "closed"})
    await ops.close_pr(PR_JSON["number"])
    request = transport.requests[0]
    assert request.method == "PATCH"
    assert request.url.path.endswith(f"/pulls/{PR_JSON['number']}")
    assert json.loads(request.content) == {"state": "closed"}


async def test_update_branch_sends_expected_head_sha(ops, transport):
    transport.enqueue(202, {"message": "Updating pull request branch."})
    await ops.update_branch(PR_JSON["number"], "sha-expected")
    request = transport.requests[0]
    assert request.method == "PUT"
    assert request.url.path.endswith(f"/pulls/{PR_JSON['number']}/update-branch")
    assert json.loads(request.content) == {"expected_head_sha": "sha-expected"}


async def test_update_branch_raises_merge_sha_mismatch_on_422(ops, transport):
    transport.enqueue(422, {"message": "head sha mismatch"})
    with pytest.raises(MergeShaMismatch) as exc_info:
        await ops.update_branch(PR_JSON["number"], "sha-stale")
    assert exc_info.value.status == 422


async def test_squash_merge_sends_body_and_returns_merge_sha(ops, transport):
    transport.enqueue(200, {"sha": "merge-sha-1", "merged": True, "message": "ok"})
    sha = await ops.squash_merge(PR_JSON["number"], "sha-head", "title: squash")
    assert sha == "merge-sha-1"
    body = json.loads(transport.requests[0].content)
    assert body == {"merge_method": "squash", "sha": "sha-head", "commit_title": "title: squash"}


async def test_squash_merge_raises_merge_blocked_on_405(ops, transport):
    transport.enqueue(405, {"message": "Method Not Allowed"})
    with pytest.raises(MergeBlocked) as exc_info:
        await ops.squash_merge(PR_JSON["number"], "sha-head", "t")
    assert exc_info.value.status == 405


async def test_squash_merge_raises_merge_sha_mismatch_on_409(ops, transport):
    transport.enqueue(409, {"message": "Head branch was modified."})
    with pytest.raises(MergeShaMismatch) as exc_info:
        await ops.squash_merge(PR_JSON["number"], "sha-head", "t")
    assert exc_info.value.status == 409


# -- checks -------------------------------------------------------------


def _check_runs_body(status: str | None, conclusion: str | None) -> dict[str, Any]:
    if status is None:
        return {"total_count": 0, "check_runs": []}
    return {"total_count": 1, "check_runs": [{"status": status, "conclusion": conclusion}]}


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        (None, None, CheckState.ABSENT),
        ("queued", None, CheckState.PENDING),
        ("in_progress", None, CheckState.PENDING),
        ("completed", None, CheckState.PENDING),  # null conclusion -> not a verdict yet
        ("completed", "success", CheckState.SUCCESS),
        ("completed", "failure", CheckState.FAILURE),
        ("completed", "neutral", CheckState.FAILURE),  # doctrine #1: not green
        ("completed", "skipped", CheckState.FAILURE),  # doctrine #1: not green
        ("completed", "cancelled", CheckState.FAILURE),
        ("completed", "timed_out", CheckState.FAILURE),
        ("completed", "action_required", CheckState.FAILURE),
        ("completed", "stale", CheckState.FAILURE),
    ],
)
async def test_oracle_check_maps_status_and_conclusion(
    ops, transport, status, conclusion, expected
):
    transport.enqueue(200, _check_runs_body(status, conclusion))
    assert await ops.oracle_check("abc123") == expected
    request = transport.requests[0]
    assert request.url.path.endswith("/commits/abc123/check-runs")
    assert request.url.params["check_name"] == "werft-oracle"
    assert request.url.params["filter"] == "latest"


# -- backlog issues -----------------------------------------------------


async def test_list_ready_issues_filters_pull_request_items(ops, transport):
    transport.enqueue(
        200,
        [
            {"number": 1, "title": "issue one"},
            {"number": 2, "title": "pr disguised as issue", "pull_request": {"url": "..."}},
        ],
    )
    result = await ops.list_ready_issues()
    assert result.modified is True
    assert [item["number"] for item in result.data] == [1]
    request = transport.requests[0]
    assert request.url.params["labels"] == "werft:ready"
    assert request.url.params["state"] == "open"


async def test_list_ready_issues_passes_through_not_modified(ops, transport):
    transport.enqueue(304)
    result = await ops.list_ready_issues()
    assert result == ConditionalResult(modified=False, data=None)


# -- labels ---------------------------------------------------------------


async def test_ensure_label_creates_when_absent(ops, transport):
    transport.enqueue(201, {"name": "werft:ready", "color": "0e8a16"})
    await ops.ensure_label("werft:ready", "0e8a16")
    body = json.loads(transport.requests[0].content)
    assert body == {"name": "werft:ready", "color": "0e8a16"}


async def test_ensure_label_is_a_no_op_when_it_already_exists(ops, transport):
    transport.enqueue(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
    await ops.ensure_label("werft:ready", "0e8a16")  # must not raise


# -- branch protection ---------------------------------------------------


async def test_partial_protection_body_is_verbatim(ops, transport):
    transport.enqueue(200, {})
    await ops.apply_partial_protection("unattended")
    body = json.loads(transport.requests[0].content)
    assert body == {
        "required_status_checks": None,
        "enforce_admins": True,
        "required_pull_request_reviews": None,
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }


async def test_strict_protection_requires_werft_oracle_strict(ops, transport):
    transport.enqueue(200, {})
    await ops.apply_strict_protection("unattended")
    body = json.loads(transport.requests[0].content)
    assert body["required_status_checks"] == {
        "strict": True,
        "checks": [{"context": "werft-oracle"}],
    }
    assert body["enforce_admins"] is True
