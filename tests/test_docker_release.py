import subprocess
import sys
from pathlib import Path

import pytest

RELEASE_VERSION_SCRIPT = (
    Path(__file__).resolve().parents[1] / "docker" / "release_version.py"
)


@pytest.mark.parametrize(
    ("project_version", "tag", "expected"),
    [("1.6", "v1.6.0", "1.6.0"), ("1.6.2", "v1.6.2", "1.6.2")],
)
def test_release_version_matches_project(tmp_path, project_version, tag, expected):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nversion = "{project_version}"\n'
    )

    result = subprocess.run(
        [sys.executable, str(RELEASE_VERSION_SCRIPT), tag],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("tag", ["v1.6", "v1.6.0-rc.1", "v1.6.1", "v01.6.0"])
def test_release_version_rejects_invalid_or_mismatched_tag(tmp_path, tag):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.6"\n')

    result = subprocess.run(
        [sys.executable, str(RELEASE_VERSION_SCRIPT), tag],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Expected release tag v1.6.0" in result.stderr
    assert not result.stdout
