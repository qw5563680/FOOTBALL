"""Exercise daily dispatch through the real YAML DAG and offline shell recorders."""

import json
import os
import re
import subprocess

import pytest

from test_early_schedule_workflow import FOLLOWUPS, run_job_conditions
from test_stats_workflow import OPERATIONS, WORKFLOW, condition, evaluate, jobs, script, step

CHAIN = FOLLOWUPS | {"apis"}
CASES = json.loads((WORKFLOW.parents[2] / "data/fixtures/t43/daily_dispatch.json").read_text())


def daily(**context):
    return {
        "github.event_name": "workflow_dispatch",
        "inputs.operation": "daily",
        "inputs.gate": False,
        **context,
    }


@pytest.mark.parametrize("cron", ["40 2 * * *", "40 9 * * *"])
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_daily_matches_scheduled_chain_even_with_failed_or_skipped_dependencies(cron, case):
    arguments = {k: v for k, v in case.items() if k != "name"}
    scheduled = run_job_conditions(cron=cron, **arguments)
    dispatched = run_job_conditions(cron="", **arguments, **daily())
    assert dispatched == scheduled
    assert dispatched["schedule-gate"] == dispatched["stats-backfill"] == "skipped"
    if case["name"] == "all_success":
        assert all(dispatched[name] == "success" for name in CHAIN)


@pytest.mark.parametrize("gate", [False, True])
@pytest.mark.parametrize("output", ["true", "false", ""])
@pytest.mark.parametrize("outcome", ["success", "failure", "skipped"])
def test_daily_optional_gate_controls_every_job_and_fails_open(gate, output, outcome):
    results = run_job_conditions(
        cron="",
        gate_output=output,
        outcomes={"schedule-gate": outcome},
        **daily(**{"inputs.gate": gate}),
    )
    assert results["schedule-gate"] == (outcome if gate else "skipped")
    expected = "skipped" if gate and output == "false" and outcome == "success" else "success"
    assert all(results[name] == expected for name in CHAIN)
    assert results["stats-backfill"] == "skipped"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_gate_does_not_change_any_existing_manual_operation(operation):
    context = {"github.event_name": "workflow_dispatch", "inputs.operation": operation}
    original = run_job_conditions(cron="", **context, **{"inputs.gate": False})
    gated = run_job_conditions(cron="", **context, **{"inputs.gate": True})
    assert gated == original
    assert gated["schedule-gate"] == "skipped"


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-test", "football-ingest-verify-test"])
def test_daily_on_legacy_tag_still_collects_and_publishes_apis(ref):
    context = daily(**{"github.ref_name": ref})
    mode = re.search(r"FEED_MODE: \$\{\{ (.*?) \}\}", WORKFLOW.read_text()).group(1)
    assert evaluate(mode, **context) == "apis"
    results = run_job_conditions(cron="", **context)
    assert all(results[name] == "success" for name in CHAIN)


def expression(value, name):
    return re.search(rf"{name}: \$\{{\{{ (.*?) \}}\}}", value).group(1)


@pytest.mark.parametrize("hour,expected", [(8, "early"), (9, "late")])
def test_daily_uses_scheduled_parameters_and_exports_actual_slot(tmp_path, hour, expected):
    collector = step("Restore and collect APIs", jobs()["apis"])
    context = daily(**{"inputs.days": 12, "fromJSON(inputs.options || '{}').slot": "late"})
    slot = evaluate(expression(collector, "FEED_SLOT"), **context)
    assert slot == "auto"
    # Daily does not use overrides meant for individual manual collection operations.
    for job, title in (
        ("apis", "Restore and collect APIs"),
        ("sporttery", "Restore data, collect"),
    ):
        lookback = expression(step(title, jobs()[job]), "LOOKBACK")
        assert str(evaluate(lookback, **context)) == "3"
    for name, content in {
        "date": '#!/bin/bash\nprintf "%s\\n" "$FAKE_HOUR"\n',
        "python": '#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n',
    }.items():
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o755)
    output, capture = tmp_path / "output", tmp_path / "argv"
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(collector)],
        env={
            "PATH": str(tmp_path) + os.pathsep + os.defpath,
            "FAKE_HOUR": f"{hour:02}",
            "GITHUB_OUTPUT": str(output),
            "CAPTURE_ARGS": str(capture),
            "WORK_DIR": str(tmp_path / "work"),
            "FEED_MODE": "apis",
            "LOOKBACK": "3",
            "FEED_SLOT": slot,
            "GITHUB_RUN_ID": "offline",
            "GITHUB_RUN_ATTEMPT": "1",
        },
        check=True,
        capture_output=True,
    )
    assert output.read_text().strip() == f"slot={expected}"
    argv = capture.read_text().splitlines()
    assert argv[argv.index("--slot") + 1] == "auto"
    assert argv[argv.index("--days") + 1] == "3"


@pytest.mark.parametrize("gate", [False, True])
def test_daily_summary_header_in_every_active_job_including_gate_only_skip(tmp_path, gate):
    workflow = WORKFLOW.read_text()
    context = daily(**{"inputs.gate": gate})
    flag = evaluate(expression(workflow, "ZUCAI_DAILY_GATE"), **context)
    assert flag == str(gate).lower()
    for name in CHAIN | {"schedule-gate"}:
        job = jobs()[name]
        header = step("Identify manual daily chain", job).split("\n      - ", 1)[0]
        assert evaluate(condition(header), **context)
        assert not evaluate(condition(header), **{"github.event_name": "schedule"})
        assert not evaluate(condition(header), **{"inputs.operation": "predict"})
        assert "secrets." not in header
        if name == "sporttery":
            assert job.index("Expose Git Bash") < job.index(header) < job.index("Install project")
            assert "shell: bash" in header
        else:
            assert job.index(header) < job.index("actions/checkout@")
        path = tmp_path / name
        subprocess.run(
            ["/bin/bash", "-eu", "-c", script(header)],
            env={"GITHUB_STEP_SUMMARY": str(path), "ZUCAI_DAILY_GATE": flag},
            check=True,
            capture_output=True,
        )
        assert path.read_text().splitlines()[0] == f"手动整条链（gate={flag}）"


def test_daily_preserves_failure_guards_status_arguments_and_run_provenance():
    job = jobs()["predict"]
    assert "needs.apis.result != 'failure'" in condition(job)
    assert "!cancelled()" in condition(job)
    assert "needs: [schedule-gate, apis, sporttery]" in job
    prediction = step("Train configured leagues", job)
    assert "SPORTTERY_STATUS: ${{ needs.sporttery.result }}" in prediction
    assert '--sporttery-status "$SPORTTERY_STATUS"' in prediction
    job = jobs()["digest"]
    assert "needs: [schedule-gate, scoreboard, sporttery]" in job
    render = step("Render daily digest", job)
    assert "SPORTTERY_STATUS: ${{ needs.sporttery.result }}" in render
    assert '--jc-sync-status "$SPORTTERY_STATUS" --jc-sync-run-id "$GITHUB_RUN_ID"' in render
    assert "--require-notified-history" in render
    assert "ZUCAI_OPERATION: ${{ inputs.operation }}" in WORKFLOW.read_text()
    inputs = WORKFLOW.read_text().split("    inputs:\n", 1)[1].split("  push:", 1)[0]
    assert "daily]" in inputs
    gate = inputs.split("      gate:\n", 1)[1]
    assert "type: boolean" in gate and "default: false" in gate


def test_daily_ignores_options_for_other_operations():
    validate = step("Validate dispatch options", jobs()["apis"])
    assert not evaluate(condition(validate), **daily())
    assert evaluate(condition(validate), **{"inputs.operation": "apis"})


@pytest.mark.parametrize("slot", ["auto", "early", "late"])
def test_manual_apis_slot_keeps_same_meaning_in_options(slot):
    collector = step("Restore and collect APIs", jobs()["apis"])
    context = {"inputs.operation": "apis", "fromJSON(inputs.options || '{}').slot": slot}
    assert evaluate(expression(collector, "FEED_SLOT"), **context) == slot
