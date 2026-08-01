"""The in-container adapter runtime (SPEC §4.3/§4.4, [BP§P3.3]).

These limits are hygiene, not containment — a root agent can patch this code.
The tests still matter: hygiene that silently stops working is how a token ends
up in a retained transcript.
"""

import ast
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "runners", "adapter"))

import werft_adapter.main as main_module  # noqa: E402
from werft_adapter import (  # noqa: E402
    EXIT_CLI_UNSTARTABLE,
    EXIT_CONTRACT_FULFILLED,
    EXIT_RESULT_SERIALIZATION_FAILURE,
    EXIT_WORKSPACE_GIT_FAILURE,
)
from werft_adapter.atomic import write_json_atomic  # noqa: E402
from werft_adapter.main import run_cli  # noqa: E402
from werft_adapter.process import (  # noqa: E402
    reap_until_echild,
    start_in_own_process_group,
    tree_kill,
)
from werft_adapter.redact import PLACEHOLDER, Redactor  # noqa: E402

LEGACY_GHS = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # 36 chars after the prefix
LONG_GHS = "ghs_" + "1234567.eyJhbGciOiJSUzI1NiJ9." + ("x" * 460)


def test_adapter_compiles_for_the_runner_images_python():
    """The adapter runs on the runner image's python3.12, not the manager's 3.14.

    Python 3.14 accepts unparenthesized `except A, B:` (PEP 758) and 3.12 does
    not, so syntax that passes every local check can still be a SyntaxError at
    PID 1 inside the container — which is exactly what shipped once. Compiling
    against the container's feature version is the guard.
    """
    package = pathlib.Path(__file__).parents[3] / "runners" / "adapter" / "werft_adapter"
    sources = sorted(package.glob("*.py"))
    assert len(sources) >= 5, f"expected the adapter package, found {sources}"
    for source in sources:
        ast.parse(
            source.read_text(encoding="utf-8"),
            filename=str(source),
            feature_version=(3, 12),  # the runner image's interpreter
        )


def test_adapter_sources_are_clean_utf8():
    """Mojibake in these files has bitten this package twice."""
    package = pathlib.Path(__file__).parents[3] / "runners" / "adapter" / "werft_adapter"
    for source in sorted(package.glob("*.py")):
        raw = source.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{source.name} has a UTF-8 BOM"
        text = raw.decode("utf-8")
        for marker in ("Â§", "â€", "Ã¢"):
            assert marker not in text, f"{source.name} contains double-encoded UTF-8"


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


# --- run_cli: the two ways a naive implementation hangs -----------------------


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="POSIX process groups only")
def test_run_cli_does_not_deadlock_on_a_chatty_stderr(tmp_path):
    """Draining stdout to EOF *then* stderr deadlocks once the child fills the
    64 KiB stderr pipe buffer — and `claude` reports account-level failures on
    stderr, which is exactly what the classifier depends on.
    """
    script = tmp_path / "chatty.sh"
    script.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ $i -lt 4000 ]; do\n"
        '  echo "stderr line $i padding padding padding padding padding" >&2\n'
        "  i=$((i+1))\n"
        "done\n"
        'echo \'{"type":"result","subtype":"success"}\'\n'
    )
    log = tmp_path / "log.jsonl"

    exit_code, stderr_tail = run_cli(
        ["/bin/sh", str(script)],
        dict(os.environ),
        log_path=str(log),
        ceiling_seconds=30,
        cwd=str(tmp_path),
    )

    assert exit_code == 0, "a chatty stderr must not deadlock the adapter"
    assert "stderr line" in stderr_tail
    assert '"type":"result"' in log.read_text()


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="POSIX process groups only")
def test_run_cli_ceiling_fires_on_a_silent_child(tmp_path):
    """A deadline checked inside the stdout loop never runs for a child that
    prints nothing, so the ceiling has to live on its own thread."""
    script = tmp_path / "silent.sh"
    script.write_text("#!/bin/sh\nsleep 60\n")
    log = tmp_path / "log.jsonl"

    started = time.monotonic()
    exit_code, _ = run_cli(
        ["/bin/sh", str(script)],
        dict(os.environ),
        log_path=str(log),
        ceiling_seconds=2,
        cwd=str(tmp_path),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 30, "the ceiling must fire without any output from the child"
    assert exit_code != 0


def test_secrets_include_the_provider_token_from_the_environment(tmp_path, monkeypatch):
    """SPEC §8 retains transcripts, including offsite. A tool call that dumps
    env, or an auth error echoing the token, must not land verbatim in one."""
    monkeypatch.setattr(main_module, "GIT_TOKEN_PATH", str(tmp_path / "absent"))
    secrets = main_module._read_secrets({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-SECRET"})
    assert "sk-ant-oat01-SECRET" in secrets


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="POSIX process groups only")
def test_redaction_is_actually_wired_into_the_log_tee(tmp_path):
    """The Redactor being correct in isolation proves nothing if run_cli never
    calls it — five mutations to the wiring used to pass the whole suite."""
    token = "ghs_" + "W1r3dInT0Th3T33AAAAAAAAAAAAAAAAAAAAA"
    script = tmp_path / "leaky.sh"
    script.write_text(f"#!/bin/sh\necho 'pushing with {token}'\n")
    log = tmp_path / "log.jsonl"

    run_cli(
        ["/bin/sh", str(script)],
        dict(os.environ),
        log_path=str(log),
        ceiling_seconds=20,
        secrets=[token],
        cwd=str(tmp_path),
    )

    contents = log.read_text()
    assert token not in contents, "the token reached the retained transcript"
    assert PLACEHOLDER in contents


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="POSIX process groups only")
def test_redaction_covers_stderr_too(tmp_path):
    token = "ghs_" + "OnStdErrAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    script = tmp_path / "leaky_err.sh"
    script.write_text(f"#!/bin/sh\necho 'auth failed for {token}' >&2\nexit 1\n")

    _exit_code, stderr_tail = run_cli(
        ["/bin/sh", str(script)],
        dict(os.environ),
        log_path=str(tmp_path / "log.jsonl"),
        ceiling_seconds=20,
        secrets=[token],
        cwd=str(tmp_path),
    )

    assert token not in stderr_tail
    assert PLACEHOLDER in stderr_tail


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="POSIX process groups only")
def test_invalid_utf8_from_the_cli_does_not_kill_the_adapter(tmp_path):
    """One bad byte from any subprocess used to raise UnicodeDecodeError out of
    the read loop and take the whole run with it."""
    script = tmp_path / "binary.sh"
    script.write_text("#!/bin/sh\nprintf 'before \\377\\376 after\\n'\n")

    exit_code, _ = run_cli(
        ["/bin/sh", str(script)],
        dict(os.environ),
        log_path=str(tmp_path / "log.jsonl"),
        ceiling_seconds=20,
        cwd=str(tmp_path),
    )
    assert exit_code == 0


def test_run_cli_reports_an_unstartable_cli_as_exit_2(tmp_path):
    """SPEC §4.3: exit 2 means the CLI could not be started."""
    exit_code, detail = run_cli(
        ["/nonexistent/definitely-not-a-real-binary"],
        dict(os.environ),
        log_path=str(tmp_path / "log.jsonl"),
        ceiling_seconds=5,
        cwd=str(tmp_path),
    )
    assert exit_code == EXIT_CLI_UNSTARTABLE
    assert "could not start" in detail
