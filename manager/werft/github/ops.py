"""RepoOps: the closed set of repo operations (SPEC §6 GitHub integration;
plan Task 3/A3).

Every repo-scoped GitHub call the orchestrator or dispatcher needs — refs,
pull requests, checks, backlog issues, labels, branch protection — goes
through `RepoOps`, never through `GitHubClient` directly: this module is the
one place that knows GitHub's REST shapes (create-ref vs update-ref vs
delete-ref bodies, the PR lifecycle, the oracle check-run query, the two
protection bodies) so callers only ever see typed inputs/outputs
(`PullRequest`, `CheckState`, plain `str | None`).

Two idempotency seams matter enough to be load-bearing, not incidental:

- **Adopt-on-422** (`open_pr`, `ensure_branch`): a crash between "create"
  and "record the result" is a normal failure mode for a poller-driven
  system with no distributed transaction across GitHub and Werft's own DB
  (SPEC §6.2: PR-open is idempotent, adopt-on-422). Re-driving the same
  create call hits GitHub's own duplicate-resource 422; rather than treat
  that as fatal, both methods fall back to *reading* the resource GitHub
  says already exists and returning it, so a re-driven caller converges on
  the same state a first-try success would have left.
- **Doctrine #1, mechanically** (`oracle_check`): "no LLM opinion exists
  anywhere in this path" (lineage ARCHITECTURE-v1.4 §6.4/§8.2) means a
  check-run conclusion of `neutral` or `skipped` must never read as green —
  only `completed` + `conclusion == "success"` does. Treating a skipped
  check as passing would let an agent's own PR route around verification
  by making the oracle a no-op, silently.

No retry loops, no DB access, no orchestration logic: one documented GitHub
call (occasionally two, for the adopt fallback) per method — the
orchestrator's tick owns retries and state transitions.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from werft.domain.errors import PermanentError
from werft.github.client import ConditionalResult, GitHubApiError, GitHubClient


class MergeBlocked(GitHubApiError):
    """`PUT .../merge` returned 405: branch protection or another
    merge-method restriction is blocking the squash merge outright — not a
    stale head, which is `MergeShaMismatch`."""


class MergeShaMismatch(GitHubApiError):
    """The caller's expected/guarded head sha is stale: `update_branch`'s
    422 or `squash_merge`'s 409, both meaning the PR's head moved since the
    caller last read it. Never retried inline here — the caller re-reads
    and re-drives on its own next tick."""


class CheckState(StrEnum):
    """`oracle_check`'s result. `ABSENT` and `PENDING` both mean "not a
    verdict yet"; only `SUCCESS` is green — see `oracle_check`'s docstring
    for why `neutral`/`skipped` conclusions map to `FAILURE`, not
    `SUCCESS`."""

    ABSENT = "absent"
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class PullRequest:
    """The fields Werft's orchestrator actually reads off a GitHub PR
    object — never the raw dict, so a GitHub schema change is caught at one
    parse site (`_parse_pr`), not at every call site."""

    number: int
    state: str
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    mergeable: bool | None
    mergeable_state: str
    html_url: str


def _parse_pr(data: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=data["number"],
        state=data["state"],
        merged=bool(data.get("merged", False)),
        head_ref=data["head"]["ref"],
        head_sha=data["head"]["sha"],
        base_ref=data["base"]["ref"],
        mergeable=data.get("mergeable"),
        mergeable_state=data.get("mergeable_state", "unknown"),
        html_url=data["html_url"],
    )


#: The operator-owned dispatch label (SPEC §6.3 onboarding, SPEC §6.2
#: intake). Read here by `list_ready_issues`, written by onboarding's
#: `ensure_label`, and removed by `merge_flow` once a run's work has landed
#: — one definition, because all three have to name the same string.
READY_LABEL = "werft:ready"

#: GitHub's maximum `per_page` for the issues API. Anything smaller means
#: `list_ready_issues` pages more often; GitHub's *default* (30) is what made
#: it silently truncate before it paged at all.
_READY_ISSUE_PAGE_SIZE = 100

#: Hard ceiling on `list_ready_issues`' page walk (10 000 ready issues). A
#: malformed or self-referential `Link: rel="next"` header must cost a
#: bounded number of rate-limit units, not an unbounded loop inside one poll.
MAX_READY_ISSUE_PAGES = 100


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])
    return response.text


#: `apply_partial_protection`'s exact body (SPEC §6.3: the protection
#: `unattended` gets at repo creation, before any run PR — and thus any
#: `werft-oracle` check — exists): enforce-admins and no
#: force-push/deletions from day one, but no required check yet, since
#: there is no `werft-oracle` context to require.
_PARTIAL_PROTECTION_BODY: dict[str, Any] = {
    "required_status_checks": None,
    "enforce_admins": True,
    "required_pull_request_reviews": None,
    "restrictions": None,
    "allow_force_pushes": False,
    "allow_deletions": False,
}

#: `apply_strict_protection`'s exact body (SPEC §3.1: the automatic
#: doctrine-#1 flip on the first green `werft-oracle` check): same base,
#: `required_status_checks` now requires the branch be up to date
#: (`strict`) against exactly the `werft-oracle` context — the
#: merged-result guarantee (SPEC §6.2 `strict_serialized`) depends on
#: `strict: True` specifically.
_STRICT_PROTECTION_BODY: dict[str, Any] = {
    **_PARTIAL_PROTECTION_BODY,
    "required_status_checks": {"strict": True, "checks": [{"context": "werft-oracle"}]},
}


class RepoOps:
    """One `(owner, repo)`'s worth of GitHub repo operations, built on a
    shared `GitHubClient`. Stateless beyond that binding."""

    def __init__(self, client: GitHubClient, owner: str, repo: str) -> None:
        self._client = client
        self._owner = owner
        self._repo = repo

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self._owner}/{self._repo}{suffix}"

    def invalidate_conditional(self) -> None:
        """Retract every ETag this repo's conditional GETs have stored.

        Called by an orchestrator unit whose transaction rolled back *after*
        a 200: the ETag advanced in memory but the rows it described did
        not survive, so the next poll must re-fetch rather than take a free
        304 over lost writes. Scoped to this repo's own paths — one
        project's failed unit must not cost another project its ETags on a
        shared client."""
        self._client.invalidate_conditional(self._repo_path(""))

    # -- refs -------------------------------------------------------------

    async def get_ref_sha(self, branch: str) -> str | None:
        """The current head sha of `refs/heads/<branch>`, or `None` if the
        branch doesn't exist. The cheap read `ensure_branch`'s adopt path
        and a dispatcher's pre-flight both use."""
        response = await self._client.request(
            "GET", self._repo_path(f"/git/ref/heads/{branch}"), expect=(200, 404)
        )
        if response.status_code == 404:
            return None
        return response.json()["object"]["sha"]

    async def ensure_branch(self, branch: str, from_sha: str) -> str:
        """Create `branch` at `from_sha`; return the sha actually left in
        place. Adopt-on-422: a duplicate-ref 422 (this branch already
        exists — a re-driven caller after a create-then-crash) falls back
        to reading the branch's current sha rather than failing. The
        follow-up read coming back empty means the 422 wasn't actually
        "already exists" in any recoverable sense — not adoptable, so a
        `PermanentError`."""
        response = await self._client.request(
            "POST",
            self._repo_path("/git/refs"),
            json={"ref": f"refs/heads/{branch}", "sha": from_sha},
            expect=(201, 422),
        )
        if response.status_code == 201:
            return response.json()["object"]["sha"]
        existing = await self.get_ref_sha(branch)
        if existing is None:
            raise PermanentError(
                f"ensure_branch: GitHub reported {branch!r} already exists (422) "
                "but a follow-up read found no such ref"
            )
        return existing

    async def force_reset_ref(self, branch: str, sha: str) -> None:
        """Force `refs/heads/<branch>` to point at `sha` (SPEC §6.1/§3.2: every
        dispatch attempt force-resets the run branch to `unattended` HEAD)."""
        await self._client.request(
            "PATCH",
            self._repo_path(f"/git/refs/heads/{branch}"),
            json={"sha": sha, "force": True},
            expect=(200,),
        )

    async def delete_ref(self, branch: str) -> None:
        """Delete `refs/heads/<branch>`; already-gone (404) is a no-op —
        terminal-path cleanup (SPEC §6.1/§3.2) may race a repo's own
        auto-delete-on-merge."""
        await self._client.request(
            "DELETE", self._repo_path(f"/git/refs/heads/{branch}"), expect=(204, 404)
        )

    # -- pull requests ------------------------------------------------------

    async def open_pr(self, head: str, base: str, title: str, body: str) -> PullRequest:
        """Open `head -> base`. Adopt-on-422: a duplicate-PR 422 means a
        re-driven caller already created this PR before crashing — list
        open PRs by `head`/`base` and adopt the survivor instead of
        failing. An empty list means the 422 wasn't a duplicate-PR after
        all; that's not adoptable, so it's a `PermanentError`."""
        response = await self._client.request(
            "POST",
            self._repo_path("/pulls"),
            json={"head": head, "base": base, "title": title, "body": body},
            expect=(201, 422),
        )
        if response.status_code == 201:
            return _parse_pr(response.json())
        listing = await self._client.request(
            "GET",
            self._repo_path("/pulls"),
            params={"head": f"{self._owner}:{head}", "base": base, "state": "open"},
            expect=(200,),
        )
        candidates = listing.json()
        if not candidates:
            raise PermanentError(
                f"open_pr: 422 opening {head!r} -> {base!r} but no open PR to adopt"
            )
        return _parse_pr(candidates[0])

    async def get_pr(self, number: int) -> PullRequest | None:
        """The PR by number, or `None` if it doesn't exist (404)."""
        response = await self._client.request(
            "GET", self._repo_path(f"/pulls/{number}"), expect=(200, 404)
        )
        if response.status_code == 404:
            return None
        return _parse_pr(response.json())

    async def close_pr(self, number: int) -> None:
        """Close PR `number`. GitHub's update endpoint is idempotent on
        `state` — closing an already-closed PR still returns 200 — so this
        is naturally a no-op on a repeat call; no pre-read needed."""
        await self._client.request(
            "PATCH", self._repo_path(f"/pulls/{number}"), json={"state": "closed"}, expect=(200,)
        )

    async def update_branch(self, number: int, expected_head_sha: str) -> None:
        """Update PR `number`'s branch from its base (SPEC §6.2
        `strict_serialized`: the pre-merge refresh that makes
        green-on-updated-head mean green-on-merged-result).
        `expected_head_sha` guards the update against a head that moved
        since the caller last read it; a stale guard raises
        `MergeShaMismatch` rather than updating the wrong commit."""
        response = await self._client.request(
            "PUT",
            self._repo_path(f"/pulls/{number}/update-branch"),
            json={"expected_head_sha": expected_head_sha},
            expect=(202, 422),
        )
        if response.status_code == 422:
            raise MergeShaMismatch(422, _error_message(response))

    async def squash_merge(self, number: int, head_sha: str, commit_title: str) -> str:
        """Squash-merge PR `number`; returns the merge commit sha. `sha`
        guards the merge against the same race `update_branch` guards
        against (409 → `MergeShaMismatch`); 405 means branch protection (or
        another repo setting) is blocking the merge method outright
        (`MergeBlocked`), independent of any sha race."""
        response = await self._client.request(
            "PUT",
            self._repo_path(f"/pulls/{number}/merge"),
            json={"merge_method": "squash", "sha": head_sha, "commit_title": commit_title},
            expect=(200, 405, 409),
        )
        if response.status_code == 405:
            raise MergeBlocked(405, _error_message(response))
        if response.status_code == 409:
            raise MergeShaMismatch(409, _error_message(response))
        return response.json()["sha"]

    # -- checks -------------------------------------------------------------

    async def oracle_check(self, ref: str) -> CheckState:
        """The latest `werft-oracle` check-run's state for `ref`.

        No matching run → `ABSENT` (the oracle hasn't started, or never ran
        on this ref). Any non-`completed` status → `PENDING`. `completed`
        with a `null` conclusion is a defensive `PENDING` too (GitHub's
        schema allows it transiently; it is never a verdict). `completed` +
        `conclusion == "success"` → `SUCCESS` — the only green outcome.
        Everything else `completed` — `failure`, `cancelled`, `timed_out`,
        `action_required`, `stale`, and critically `neutral`/`skipped` — is
        `FAILURE`: doctrine #1 ("no LLM opinion exists anywhere in this
        path", lineage ARCHITECTURE-v1.4 §6.4/§8.2) means a check that
        didn't affirmatively pass must never read as green, or an agent's
        own PR could route around verification by making the oracle a
        no-op.
        """
        response = await self._client.request(
            "GET",
            self._repo_path(f"/commits/{ref}/check-runs"),
            params={"check_name": "werft-oracle", "filter": "latest"},
            expect=(200,),
        )
        runs = response.json().get("check_runs", [])
        if not runs:
            return CheckState.ABSENT
        run = runs[0]
        if run.get("status") != "completed":
            return CheckState.PENDING
        conclusion = run.get("conclusion")
        if conclusion is None:
            return CheckState.PENDING
        if conclusion == "success":
            return CheckState.SUCCESS
        return CheckState.FAILURE

    # -- backlog issues -------------------------------------------------------

    async def list_ready_issues(self) -> ConditionalResult:
        """*Every* open issue labeled `werft:ready`, ETag-conditional on the
        first page (SPEC §6.2: free 304s on the 60 s backlog poll).

        Pagination is not an optimization here, it is correctness:
        `sync_backlog` reads "absent from this fetch" as "no longer
        labeled" and flips those rows `is_eligible=False`. At GitHub's
        default page size of 30, a project with 35 ready issues had its 5
        oldest actively de-eligibilized on every poll and silently dropped
        out of intake, recovering only if enough newer issues closed. So
        the walk asks for the maximum page size and follows `Link:
        rel="next"` to exhaustion (bounded by `MAX_READY_ISSUE_PAGES`
        against a malformed or looping header).

        Subsequent pages go through plain `request` GETs, re-derived from
        this repo's own path with an explicit `page` param rather than
        following the absolute URL GitHub hands back: the installation
        token must only ever be sent to a URL this client constructed. They
        carry no `If-None-Match` either — the stored ETag belongs to page
        one's URL alone, and sending it here would 304 away a page that has
        not been read. A 304 on page one still short-circuits the whole
        walk, which is exactly what it asserts: nothing about this
        collection changed.

        Items that are actually pull requests (GitHub's issues API returns
        both; a `pull_request` key is the tell) are filtered out of every
        page — belt-and-braces with A4's DB-level assertion of the same
        filter.
        """
        params: dict[str, Any] = {
            "labels": READY_LABEL,
            "state": "open",
            "per_page": _READY_ISSUE_PAGE_SIZE,
        }
        result = await self._client.get_conditional(self._repo_path("/issues"), params=params)
        if result.data is None:
            return result

        issues = list(result.data)
        links = result.links or {}
        page = 1
        while "next" in links and page < MAX_READY_ISSUE_PAGES:
            page += 1
            response = await self._client.request(
                "GET", self._repo_path("/issues"), params={**params, "page": page}, expect=(200,)
            )
            issues.extend(response.json())
            links = response.links

        filtered = [item for item in issues if "pull_request" not in item]
        return ConditionalResult(modified=result.modified, data=filtered)

    # -- labels + protection --------------------------------------------------

    async def ensure_label(self, name: str, color: str) -> None:
        """Create label `name` if absent; a 422 (already exists) is a
        no-op — onboarding (lineage ARCHITECTURE-v1.4 §8.6) re-runs idempotently."""
        await self._client.request(
            "POST",
            self._repo_path("/labels"),
            json={"name": name, "color": color},
            expect=(201, 422),
        )

    async def remove_label(self, issue_number: int, name: str) -> None:
        """Drop label `name` from issue `issue_number`. A 404 (the issue
        never carried it, or an operator already unlabeled it by hand) is
        the desired end state, so it is a no-op rather than an error —
        which also makes a re-driven caller after a crash safe."""
        await self._client.request(
            "DELETE", self._repo_path(f"/issues/{issue_number}/labels/{name}"), expect=(200, 404)
        )

    async def apply_partial_protection(self, branch: str) -> None:
        """The protection `unattended` gets at repo creation, before any
        run PR (and thus any `werft-oracle` check) exists: enforce-admins
        and no force-push/deletions, but no required status check yet."""
        await self._client.request(
            "PUT",
            self._repo_path(f"/branches/{branch}/protection"),
            json=_PARTIAL_PROTECTION_BODY,
            expect=(200,),
        )

    async def apply_strict_protection(self, branch: str) -> None:
        """The protection the doctrine-#1 flip (SPEC §3.1) applies on the
        first green `werft-oracle` check: same base as partial, plus a
        `strict` (must-be-up-to-date) required check on exactly
        `werft-oracle` — the merged-result guarantee (SPEC §6.2) depends
        on."""
        await self._client.request(
            "PUT",
            self._repo_path(f"/branches/{branch}/protection"),
            json=_STRICT_PROTECTION_BODY,
            expect=(200,),
        )
