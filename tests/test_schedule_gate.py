"""Offline SFC early schedule decisions, with no database or collector imports."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from zucai.schedule_gate import decide_early_run

ROOT = Path(__file__).parents[1]
CASES = json.loads((ROOT / "data/fixtures/t39/schedule_cases.json").read_text())["cases"]


def archive_case(tmp_path, case):
    archive = tmp_path / "archive"
    if case["states"] is not None:
        state = archive / "state/sfc"
        state.mkdir(parents=True)
        for name, payload in case["states"].items():
            raw = payload if isinstance(payload, str) else json.dumps(payload)
            (state / name).write_text(raw, encoding="utf-8")
    return archive


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_archive_deadline_gate(tmp_path, case):
    archive = archive_case(tmp_path, case)
    decision = decide_early_run(
        archive,
        now=datetime.fromisoformat(case["now"]),
        archive_available=case.get("archive_available", True),
    )
    assert decision.run_early is case["run_early"]
    assert decision.scope == case["scope"]
    assert decision.reason


@pytest.mark.parametrize("failure", ["directory", "file", "encoding"])
def test_unreadable_scope_is_unknown_and_runs(tmp_path, monkeypatch, failure):
    archive = archive_case(tmp_path, CASES[3])
    if failure == "encoding":
        (archive / "state/sfc/26157.json").write_bytes(b"\xff")
    else:
        method = "iterdir" if failure == "directory" else "read_text"

        def unavailable(*args, **kwargs):
            raise PermissionError("offline unreadable archive")

        monkeypatch.setattr(Path, method, unavailable)
    decision = decide_early_run(archive, now=datetime.fromisoformat(CASES[3]["now"]))
    assert decision.run_early and decision.scope == "unknown"


@pytest.mark.parametrize("case", [CASES[0], CASES[3], CASES[8]], ids=lambda case: case["id"])
def test_standalone_cli_emits_actions_outputs_without_project_dependencies(tmp_path, case):
    archive = archive_case(tmp_path, case)
    output, summary = tmp_path / "output", tmp_path / "summary"
    # -S removes site-packages: this must remain usable before any pip install.
    subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "src/zucai/schedule_gate.py"),
            "--archive",
            str(archive),
            "--now",
            case["now"],
            "--archive-status",
            "success" if case.get("archive_available", True) else "failure",
        ],
        env=os.environ | {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)},
        check=True,
        capture_output=True,
    )
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "run_early": str(case["run_early"]).lower(),
        "scope": case["scope"],
    }
    assert "北京时间" in summary.read_text()


def test_gate_uses_known_official_deadline_even_for_announced_future_sale(tmp_path):
    # The extra run is triggered by the deadline day, including sales that open later.
    issue = {
        "issue_no": "26160",
        "sale_end_utc": "2026-11-10T14:00:00Z",
        "official_verification": {
            "checked_at": "2026-11-05T05:00:00Z",
            "official": {
                "sale_start_utc": "2026-11-06T01:00:00Z",
                "sale_end_utc": "2026-11-06T14:00:00Z",
            },
        },
    }
    archive = archive_case(
        tmp_path, {"states": {"26160.json": {"schema_version": 1, "issue": issue}}}
    )
    now = datetime.fromisoformat("2026-11-05T07:40:00Z")
    assert decide_early_run(archive, now=now).scope == "present"
    before_check = datetime.fromisoformat("2026-11-05T04:59:59Z")
    assert decide_early_run(archive, now=before_check).scope == "absent"
