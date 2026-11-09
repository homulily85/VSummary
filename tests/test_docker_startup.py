import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires a POSIX shell")

STARTUP_SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "start-vsummary.sh"


@pytest.fixture
def container(tmp_path, monkeypatch):
    home = tmp_path / "notebooklm"
    profile = home / "profiles" / "default"
    profile.mkdir(parents=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    trace = tmp_path / "trace"

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(home))
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "default")
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.delenv("REFRESH_FAIL", raising=False)
    monkeypatch.setenv("STARTUP_TRACE", str(trace))
    monkeypatch.setenv("PATH", f"{commands}{os.pathsep}{os.environ['PATH']}")

    for name, body in {
        "notebooklm": (
            'echo "notebooklm $*" >> "$STARTUP_TRACE"\n'
            'if [ "${REFRESH_FAIL:-}" = "1" ]; then\n'
            '    echo "fake-cookie"\n'
            '    echo "fake-master-token" >&2\n'
            "    exit 1\n"
            "fi\n"
            'echo "{}" > "$NOTEBOOKLM_HOME/profiles/$NOTEBOOKLM_PROFILE/storage_state.json"\n'
        ),
        "vsummary": 'echo "vsummary" >> "$STARTUP_TRACE"\n',
    }.items():
        executable = commands / name
        executable.write_text(f"#!/bin/sh\n{body}")
        executable.chmod(0o700)

    return profile, trace


def run_startup():
    return subprocess.run(
        ["sh", str(STARTUP_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_startup_mints_missing_cookies_then_runs_bot(container):
    profile, trace = container
    (profile / "master_token.json").write_text("{}")

    result = run_startup()

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "notebooklm auth refresh --verify",
        "vsummary",
    ]
    assert (profile / "storage_state.json").stat().st_mode & 0o777 == 0o600


def test_startup_reuses_saved_cookies(container):
    profile, trace = container
    (profile / "master_token.json").write_text("{}")
    cookies = profile / "storage_state.json"
    cookies.write_text("previous session")

    result = run_startup()

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["vsummary"]
    assert cookies.read_text() == "previous session"


def test_startup_requires_master_token_even_with_saved_cookies(container):
    profile, trace = container
    (profile / "storage_state.json").write_text("{}")

    result = run_startup()

    assert result.returncode != 0
    assert "master_token.json" in result.stderr
    assert not trace.exists()


def test_startup_stops_on_refresh_failure_without_logging_credentials(
    container, monkeypatch
):
    profile, trace = container
    (profile / "master_token.json").write_text("{}")
    monkeypatch.setenv("REFRESH_FAIL", "1")

    result = run_startup()

    assert result.returncode != 0
    assert "NotebookLM" in result.stderr
    assert "fake-cookie" not in result.stdout + result.stderr
    assert "fake-master-token" not in result.stdout + result.stderr
    assert trace.read_text().splitlines() == ["notebooklm auth refresh --verify"]


def test_startup_rejects_inline_auth_override(container, monkeypatch):
    profile, trace = container
    (profile / "master_token.json").write_text("{}")
    (profile / "storage_state.json").write_text("{}")
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "fake-inline-cookie")

    result = run_startup()

    assert result.returncode != 0
    assert "NOTEBOOKLM_AUTH_JSON" in result.stderr
    assert "fake-inline-cookie" not in result.stderr
    assert not trace.exists()
