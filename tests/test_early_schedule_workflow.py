"""Evaluate the real workflow DAG and run its gate with offline archive fixtures."""

import os
import re
import subprocess
from datetime import datetime

import pytest

from test_schedule_gate import CASES, ROOT, archive_case
from test_stats_workflow import condition, evaluate, jobs, script, step
from zucai.schedule_gate import decide_early_run

EARLY = "40 7 * * *"
FOLLOWUPS = {"sporttery", "predict", "scoreboard", "digest"}


def run_job_conditions(
    *, cron=EARLY, gate_output="true", outcomes=None, cancelled=False, **context
):
    """Model the default success() guard as well as each checked-in job expression."""
    outcomes = outcomes or {}
    results = {}
    for name in (
        "schedule-gate",
        "apis",
        "stats-backfill",
        "sporttery",
        "predict",
        "scoreboard",
        "digest",
    ):
        job = jobs()[name]
        expression = condition(job)
        needs = re.search(r"^    needs: \[(.*?)\]$", job, re.M)
        dependencies = needs.group(1).split(", ") if needs else []
        # Without a status function Actions implicitly adds success(), which would
        # suppress manual jobs and the early chain after intentionally skipped apis.
        default_success = bool(
            re.search(r"\b(?:success|failure|cancelled|always)\(\)", expression)
        ) or all(results[dependency] == "success" for dependency in dependencies)
        values = {
            "github.event_name": "schedule",
            "github.event.schedule": cron,
            "inputs.operation": "",
            "needs.schedule-gate.outputs.run_early": gate_output,
            **{f"needs.{dependency}.result": result for dependency, result in results.items()},
            **context,
        }
        enabled = default_success and evaluate(
            expression.replace("!cancelled()", repr(not cancelled)), **values
        )
        results[name] = outcomes.get(name, "success") if enabled else "skipped"
    return results


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_early_schedule_runs_only_when_a_period_is_present_or_unknown(tmp_path, case):
    archive = archive_case(tmp_path, case)
    decision = decide_early_run(
        archive,
        now=datetime.fromisoformat(case["now"]),
        archive_available=case.get("archive_available", True),
    )
    results = run_job_conditions(gate_output=str(decision.run_early).lower())
    assert results["schedule-gate"] == "success"
    assert results["apis"] == results["stats-backfill"] == "skipped"
    assert {name for name in FOLLOWUPS if results[name] == "success"} == (
        FOLLOWUPS if case["run_early"] else set()
    )


@pytest.mark.parametrize("outcome", ["failure", "skipped"])
@pytest.mark.parametrize("output", ["", "false", "true"])
def test_unavailable_gate_does_not_suppress_the_early_chain(outcome, output):
    results = run_job_conditions(gate_output=output, outcomes={"schedule-gate": outcome})
    assert all(results[name] == "success" for name in FOLLOWUPS)
    assert results["apis"] == "skipped"


@pytest.mark.parametrize("cron", ["40 2 * * *", "40 9 * * *"])
def test_normal_schedules_keep_api_collection_and_followups_despite_skipped_gate(cron):
    results = run_job_conditions(cron=cron, gate_output="")
    assert results["schedule-gate"] == results["stats-backfill"] == "skipped"
    assert all(results[name] == "success" for name in FOLLOWUPS | {"apis"})


@pytest.mark.parametrize("operation", ["predict", "scoreboard", "digest"])
def test_manual_followups_survive_skipped_dependencies(operation):
    results = run_job_conditions(
        cron="",
        gate_output="",
        **{"github.event_name": "workflow_dispatch", "inputs.operation": operation},
    )
    expected = {"predict", "scoreboard", "digest"}
    if operation == "scoreboard":
        expected.remove("predict")
    if operation == "digest":
        expected = {"digest"}
    assert {name for name, result in results.items() if result == "success"} == expected


@pytest.mark.parametrize("cron", [EARLY, "40 2 * * *", "40 9 * * *"])
def test_prediction_failure_still_scores_and_reports_existing_batches(cron):
    results = run_job_conditions(cron=cron, outcomes={"predict": "failure"})
    assert results["scoreboard"] == results["digest"] == "success"
    cancelled = run_job_conditions(cron=cron, cancelled=True)
    assert all(cancelled[name] == "skipped" for name in FOLLOWUPS)


def test_early_prediction_continues_if_sporttery_runner_is_unconfigured():
    results = run_job_conditions(**{"vars.FOOTBALL_FEEDS_RUNNER": ""})
    assert results["sporttery"] == "skipped"
    assert all(results[name] == "success" for name in FOLLOWUPS - {"sporttery"})


@pytest.mark.parametrize("status", ["success", "failure"])
def test_workflow_gate_script_runs_before_project_install_and_without_keys(tmp_path, status):
    job = jobs()["schedule-gate"]
    assert "pip install" not in job and "secrets." not in job
    assert "sparse-checkout: /state/sfc/" in job
    assert "sparse-checkout-cone-mode: false" in job
    assert "continue-on-error: true" in job
    assert "ARCHIVE_STATUS: ${{ steps.archive.outcome }}" in job
    archive = archive_case(tmp_path, CASES[3])
    archive.rename(tmp_path / "cloud-data")
    (tmp_path / "app").symlink_to(ROOT, target_is_directory=True)
    output, summary = tmp_path / "output", tmp_path / "summary"
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(step("Check archived SFC deadlines", job))],
        cwd=tmp_path,
        env=os.environ
        | {
            "ARCHIVE_STATUS": status,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        check=True,
        capture_output=True,
    )
    assert "run_early=" in output.read_text()
    if status == "failure":
        assert "run_early=true\nscope=unknown" in output.read_text()


def test_sporttery_originals_remain_in_source_artifact_with_separate_recovery_bundle():
    job = jobs()["sporttery"]
    artifact = step("Keep official originals and audit", job)
    assert job.count("actions/upload-artifact@") == 2
    assert "!cancelled()" in artifact
    assert "name: sporttery-source-${{ github.run_id }}-${{ github.run_attempt }}" in artifact
    assert "${{ env.WORK_DIR }}/data/raw" in artifact
    assert "${{ env.WORK_DIR }}/data/state" in artifact
    assert "retention-days: 90" in artifact


def test_publication_confirmation_requires_successful_push_and_remote_history():
    job = jobs()["predict"]
    publish = step("Publish frozen models and forecasts", job)
    confirm = step("Confirm forecast and SFC publication", job)
    backfill = step("Publish confirmed forecast and SFC publication evidence", job)
    assert "id: publish_forecasts" in publish
    assert job.index(publish) < job.index(confirm) < job.index(backfill)
    assert "fetch-depth: 0" in job
    assert "git -C cloud-data fetch origin" in confirm
    assert "--published-ref refs/remotes/origin/data/football-feeds" in confirm
    assert confirm.index("git -C cloud-data fetch") < confirm.index("--confirm-publication")
    assert ' --summary "$GITHUB_STEP_SUMMARY"' in confirm
    assert "publish-football-feeds.sh" in publish and "publish-football-feeds.sh" in backfill
    for status in ("success", "failure", "skipped", "cancelled"):
        expression = condition(confirm).replace("success()", repr(status == "success"))
        assert evaluate(expression, **{"steps.publish_forecasts.outcome": status}) is (
            status == "success"
        )


@pytest.mark.parametrize("fetch_status", ["0", "1"])
def test_publication_confirmation_never_reads_an_unverified_local_commit(tmp_path, fetch_status):
    confirm = step("Confirm forecast and SFC publication", jobs()["predict"])
    for name, text in {
        "git": '#!/bin/bash\nprintf "%s\\n" "$@" > "$FETCH_ARGS"\nexit "$FETCH_STATUS"\n',
        "python": '#!/bin/bash\nprintf "%s\\n" "$@" > "$CONFIRM_ARGS"\n',
    }.items():
        recorder = tmp_path / name
        recorder.write_text(text)
        recorder.chmod(0o755)
    fetch_args, confirm_args = tmp_path / "fetch", tmp_path / "confirm"
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", script(confirm)],
        cwd=tmp_path,
        env=os.environ
        | {
            "PATH": str(tmp_path) + os.pathsep + os.defpath,
            "FETCH_STATUS": fetch_status,
            "FETCH_ARGS": str(fetch_args),
            "CONFIRM_ARGS": str(confirm_args),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
    )
    assert fetch_args.read_text().splitlines() == [
        "-C",
        "cloud-data",
        "fetch",
        "origin",
        "refs/heads/data/football-feeds:refs/remotes/origin/data/football-feeds",
    ]
    assert (result.returncode == 0) is (fetch_status == "0")
    assert confirm_args.exists() is (fetch_status == "0")
    if confirm_args.exists():
        args = confirm_args.read_text().splitlines()
        assert args[:3] == ["-m", "zucai.sfc_reports", "--archive"]
        assert "--confirm-publication" in args
        assert args[args.index("--published-ref") + 1] == "refs/remotes/origin/data/football-feeds"


@pytest.mark.parametrize("status", ["success", "failure", "cancelled", "skipped"])
def test_digest_shell_passes_the_actual_jc_job_status_and_run(tmp_path, status):
    render = step("Render daily digest", jobs()["digest"])
    assert "SPORTTERY_STATUS: ${{ needs.sporttery.result }}" in render
    command = render.split("        run: ", 1)[1].strip()
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    capture = tmp_path / "args"
    subprocess.run(
        ["/bin/bash", "-eu", "-c", command],
        cwd=tmp_path,
        env=os.environ
        | {
            "PATH": str(tmp_path) + os.pathsep + os.defpath,
            "CAPTURE_ARGS": str(capture),
            "GITHUB_RUN_ID": "offline",
            "GITHUB_RUN_ATTEMPT": "2",
            "SPORTTERY_STATUS": status,
        },
        check=True,
        capture_output=True,
    )
    args = capture.read_text().splitlines()
    assert args[args.index("--run-id") + 1] == "offline-2"
    assert args[args.index("--jc-sync-status") + 1] == status
    assert args[args.index("--jc-sync-run-id") + 1] == "offline"
    assert "--require-notified-history" in args
