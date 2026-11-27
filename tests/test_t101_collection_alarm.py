"""The collection alarm's runtime and installation can be rehearsed offline."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
WORKFLOW_URL = (
    "https://api.github.com/repos/qw5563680/FOOTBALL/actions/workflows/football-watchdog.yml"
)


def test_alarm_scripts_are_ascii_with_only_two_github_endpoints():
    alarm = (SCRIPTS / "football-collection-alarm.ps1").read_bytes().decode("ascii")
    installer = (SCRIPTS / "install-football-collection-alarm.ps1").read_bytes().decode("ascii")
    assert set(re.findall(r"https?://[^\s'\"]+", alarm + installer)) == {
        f"{WORKFLOW_URL}/runs?per_page=100",
        f"{WORKFLOW_URL}/dispatches",
    }


def test_alarm_runtime_and_installation_offline(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None and os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("PowerShell is required for the offline rehearsal")
    assert pwsh is not None, "GitHub's Linux check image must have PowerShell"
    completed = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tests" / "t101_collection_alarm.ps1"),
        ],
        env={**os.environ, "LOCALAPPDATA": str(tmp_path), "TZ": "America/Los_Angeles"},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Offline collection alarm checks passed." in completed.stdout
