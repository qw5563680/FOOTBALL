"""Exercise the shipped probe Bash with an argv recorder, never Actions or the API."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/football-feeds.yml"


@pytest.mark.parametrize("leagues,seasons,count", [("3,848", "2024", "3"), ("", "", "")])
def test_stats_probe_workflow_preserves_inputs_and_uses_cli_defaults(
    tmp_path, leagues, seasons, count
):
    step = next(
        s
        for s in WORKFLOW.read_text().split("      - name:")
        if "Probe API-Football permissions" in s
    )
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    capture = tmp_path / "args.txt"
    env = dict(
        PATH=str(tmp_path) + os.pathsep + os.defpath,
        WORK_DIR=str(tmp_path / "work"),
        CAPTURE_ARGS=str(capture),
        GITHUB_RUN_ID="offline",
        GITHUB_RUN_ATTEMPT="1",
        PROBE_KIND="stats",
        PROBE_LEAGUES=leagues,
        PROBE_SEASONS=seasons,
        PROBE_PER_SCOPE=count,
    )
    subprocess.run(["bash", "-eu", "-c", script], env=env, check=True, capture_output=True)
    argv = capture.read_text().splitlines()
    assert argv[:3] == ["-m", "zucai.cloud_feeds", "probe"]
    assert argv[argv.index("--probe-kind") + 1] == "stats"
    assert argv[argv.index("--source") + 1] == "apif"
    for option, value in (("--leagues", leagues), ("--seasons", seasons), ("--per-scope", count)):
        assert (option in argv) == bool(value)
        if value:
            assert argv[argv.index(option) + 1] == value
