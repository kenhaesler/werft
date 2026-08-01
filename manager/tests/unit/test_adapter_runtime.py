"""The in-container adapter runtime (SPEC §4.3/§4.4, [BP§P3.3]).

These limits are hygiene, not containment — a root agent can patch this code.
The tests still matter: hygiene that silently stops working is how a token ends
up in a retained transcript.
"""

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "runners", "adapter"))

from werft_adapter import (  # noqa: E402
    EXIT_CLI_UNSTARTABLE,
    EXIT_CONTRACT_FULFILLED,
    EXIT_RESULT_SERIALIZATION_FAILURE,
    EXIT_WORKSPACE_GIT_FAILURE,
)
from werft_adapter.atomic import write_json_atomic  # noqa: E402
from werft_adapter.process import (  # noqa: E402
    reap_until_echild,
    start_in_own_process_group,
    tree_kill,
)
from werft_adapter.redact import PLACEHOLDER, Redactor  # noqa: E402

LEGACY_GHS = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # 36 chars after the prefix
LONG_GHS = "ghs_" + "1234567.eyJhbGciOiJSUzI1NiJ9." + ("x" * 460)


def test_exit_codes_match_the_spec_contract():
    assert (
        EXIT_CONTRACT_FULFILLED,
        EXIT_CLI_UNSTARTABLE,
        EXIT_WORKSPACE_GIT_FAILURE,
        EXIT_RESULT_SERIALIZATION_FAILURE,
    ) == (0, 2, 4, 5)


# --- redaction ----------------------------------------------------------------


def test_redacts_the_legacy_36_char_token_by_exact_value():
    redact = Redactor([LEGACY_GHS])
    assert LEGACY_GHS not in redact(f"remote: pushing with {LEGACY_GHS} ok")
    assert PLACEHOLDER in redact(f"token={LEGACY_GHS}")


def test_redacts_the_long_ghs_appid_jwt_token():
    """The 2026 format is ~520 chars and variable — a {36} pattern would miss it."""
    redact = Redactor([LONG_GHS])
    assert LONG_GHS not in redact(f"Authorization: Bearer {LONG_GHS}")


def test_does_not_pattern_match_unknown_tokens():
    """Exact-value only (SPEC §4.4). Redacting by shape is what silently stops
    working when the format changes; this asserts we never do it."""
    redact = Redactor([LEGACY_GHS])
    other = "ghs_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"
    assert other in redact(f"a different token {other}")


def test_handles_multiple_secrets_longest_first():
    short = "ghs_shortershortershortershorterAAAA"
    longer = short + "EXTRA"
    redact = Redactor([short, longer])
    assert redact(longer) == PLACEHOLDER


def test_ignores_trivially_short_values():
    """Redacting a 3-character string would shred the log without protecting anything."""
    redact = Redactor(["abc"])
    assert redact("abc def") == "abc def"


def test_a_remint_can_be_registered_mid_run():
    redact = Redactor([LEGACY_GHS])
    fresh = LONG_GHS
    redact.add(fresh)
    assert fresh not in redact(f"after remint {fresh}")
    assert LEGACY_GHS not in redact(f"and the old one {LEGACY_GHS}")


# --- atomic result writing ----------------------------------------------------


def test_write_json_atomic_produces_a_complete_document(tmp_path):
    target = tmp_path / "result.json"
    write_json_atomic(str(target), {"status": "success"})
    assert json.loads(target.read_text()) == {"status": "success"}


def test_write_json_atomic_leaves_no_partial_file_on_failure(tmp_path):
    target = tmp_path / "result.json"
    target.write_text('{"status": "previous"}')

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        write_json_atomic(str(target), {"bad": {Unserialisable(): 1}})

    assert json.loads(target.read_text()) == {"status": "previous"}, (
        "a reader must see the old file or the complete new one, never a prefix"
    )
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".result-")]
    assert leftovers == [], "the temp file must be cleaned up"


def test_write_json_atomic_replaces_an_existing_result(tmp_path):
    target = tmp_path / "result.json"
    write_json_atomic(str(target), {"status": "failure"})
    write_json_atomic(str(target), {"status": "success"})
    assert json.loads(target.read_text())["status"] == "success"


# --- process supervision ------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="POSIX process groups only")
def test_tree_kill_kills_the_whole_group(tmp_path):
    """A CLI that spawns a build that spawns a test runner must not survive."""
    script = tmp_path / "spawner.sh"
    script.write_text(
        "#!/bin/sh\nsleep 60 &\necho $! > '%s'\nsleep 60\n" % (tmp_path / "child.pid")
    )
    process = start_in_own_process_group(
        ["/bin/sh", str(script)], env=dict(os.environ), cwd=str(tmp_path)
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (tmp_path / "child.pid").exists():
        time.sleep(0.05)
    grandchild = int((tmp_path / "child.pid").read_text().strip())

    tree_kill(process, grace_seconds=1.0)
    process.wait(timeout=10)

    time.sleep(0.3)
    with pytest.raises(OSError):
        os.kill(grandchild, 0)  # gone: the whole group went down


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="POSIX process groups only")
def test_reap_until_echild_collects_orphans(tmp_path):
    finished = subprocess.Popen(["/bin/true"])
    finished.wait()
    assert reap_until_echild() >= 0  # must return, never hang


def test_reap_until_echild_returns_when_there_are_no_children():
    """Must terminate rather than block — a hung reaper would hold the container
    open past the die event the manager is waiting for."""
    assert reap_until_echild() >= 0
