import json

import pytest

from werft.config.dispatch import (
    DispatchConfig,
    DispatchConfigCache,
    dispatch_for,
    load_dispatch_config,
)
from werft.domain.errors import PermanentError

ENTRY = {
    "image_digest": "werft-runner-elastic@sha256:" + "a" * 64,
    "model": "claude-sonnet-4-6",
}


def write(tmp_path, payload) -> str:
    path = tmp_path / "dispatch.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_an_unset_path_is_an_empty_config_not_a_boot_failure():
    """A manager with no dispatch config must still serve /api/v1 and its
    pollers; it simply dispatches nothing, and says so per run."""
    assert load_dispatch_config("") == DispatchConfig()


def test_a_missing_file_is_an_empty_config(tmp_path):
    assert load_dispatch_config(str(tmp_path / "nope.json")) == DispatchConfig()


def test_a_project_entry_round_trips_with_defaults(tmp_path):
    entry = dispatch_for(
        load_dispatch_config(write(tmp_path, {"projects": {"elastic": ENTRY}})), "elastic"
    )
    assert entry.model == "claude-sonnet-4-6"
    assert entry.timeout_seconds == 5400
    assert entry.memory_bytes == 4 << 30


def test_an_unknown_key_is_refused_rather_than_ignored(tmp_path):
    """`extra="forbid"` keeps the vocabulary closed, the same discipline
    `ProjectRunnerConfig` uses: `privileged`, `cap_add`, `mounts` and friends
    are not expressible at any privilege level."""
    with pytest.raises(PermanentError):
        load_dispatch_config(write(tmp_path, {"projects": {"e": ENTRY | {"privileged": True}}}))


def test_a_tag_pinned_image_is_refused_at_load_not_at_the_first_claim(tmp_path):
    """`build_create_body` rejects tags too — but failing here means the
    operator learns at boot, not after a reservation was taken and a run
    parked at 03:00."""
    with pytest.raises(PermanentError, match="@sha256:"):
        load_dispatch_config(
            write(tmp_path, {"projects": {"e": ENTRY | {"image_digest": "x:latest"}}})
        )


def test_memory_below_the_floor_is_refused(tmp_path):
    with pytest.raises(PermanentError):
        load_dispatch_config(write(tmp_path, {"projects": {"e": ENTRY | {"memory_bytes": 1024}}}))


def test_invalid_json_is_a_permanent_error(tmp_path):
    path = tmp_path / "dispatch.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PermanentError):
        load_dispatch_config(str(path))


def test_a_project_with_no_entry_names_the_slug_and_the_setting():
    with pytest.raises(PermanentError, match="WERFT_DISPATCH_CONFIG_FILE"):
        dispatch_for(DispatchConfig(), "elastic")


def test_the_cache_picks_up_an_edited_file_without_a_restart(tmp_path):
    """D3: `image_digest` changes every time the operator rebuilds a project's
    base image on the VM (SPEC §4.1), so a rebuild must take effect on the next
    sweep."""
    path = write(tmp_path, {"projects": {"elastic": ENTRY}})
    cache = DispatchConfigCache(path)
    assert cache.current().for_slug("elastic").model == "claude-sonnet-4-6"

    write(tmp_path, {"projects": {"elastic": ENTRY | {"model": "claude-opus-4-6"}}})
    assert cache.current().for_slug("elastic").model == "claude-opus-4-6"


def test_a_malformed_file_mid_flight_keeps_the_last_good_config(tmp_path):
    """Never crash the loop, never park runs on a typo. A malformed file *at
    startup* is a different matter — `create_app` calls `load_dispatch_config`
    directly and fails boot loudly, where the operator is watching."""
    path = write(tmp_path, {"projects": {"elastic": ENTRY}})
    cache = DispatchConfigCache(path)
    good = cache.current()

    (tmp_path / "dispatch.json").write_text("{ broken", encoding="utf-8")

    assert cache.current() == good
