import json

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from werft.config.dispatch import (
    DispatchConfig,
    DispatchConfigCache,
    ProjectDispatch,
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


def test_a_set_path_that_names_no_file_is_loud_about_it(tmp_path):
    """D3: "never park runs on a typo". A mistyped `WERFT_DISPATCH_CONFIG_FILE`
    still boots (the *unset* case has to stay a clean boot) but it must not boot
    *silently*: without a line naming the path, the only symptom is every
    candidate parking with `permanent_error`, one per tick, each needing a
    manual requeue."""
    missing = tmp_path / "typo.json"
    with capture_logs() as logs:
        assert load_dispatch_config(str(missing)) == DispatchConfig()

    warnings = [entry for entry in logs if entry["event"] == "app.dispatch_config_file_missing"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["path"] == str(missing)
    assert warnings[0]["setting"] == "WERFT_DISPATCH_CONFIG_FILE"


def test_an_unset_path_says_nothing_at_all(tmp_path):
    """The counterpart: "no dispatch config" is a legitimate deployment, not an
    operator error, so it must not cry wolf once per sweep."""
    with capture_logs() as logs:
        load_dispatch_config("")
    assert logs == []


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


def test_registries_expand_presets_and_merge_extra_hosts():
    p = ProjectDispatch(
        image_digest="img@sha256:" + "a" * 64,
        model="m",
        registries=["npm", "pypi"],
        extra_hosts=["example.internal.works"],
    )
    hosts = p.egress_hosts()
    assert "registry.npmjs.org" in hosts
    assert "pypi.org" in hosts and "files.pythonhosted.org" in hosts
    assert "example.internal.works" in hosts
    assert hosts == sorted(set(hosts))


def test_unknown_registry_preset_refused_at_validation():
    with pytest.raises(ValidationError):
        ProjectDispatch(
            image_digest="img@sha256:" + "a" * 64, model="m", registries=["maven-central-typo"]
        )


def test_extra_host_shape_refused():
    # hostnames only: no scheme, no slash, no port, no wildcard
    for bad in ["https://x.y", "x.y/path", "x.y:443", "*.y.z", ""]:
        with pytest.raises(ValidationError):
            ProjectDispatch(image_digest="img@sha256:" + "a" * 64, model="m", extra_hosts=[bad])


def test_default_registries_empty_and_hosts_still_sorted():
    p = ProjectDispatch(image_digest="img@sha256:" + "a" * 64, model="m")
    assert p.egress_hosts() == []
