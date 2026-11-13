"""Execute checked-in shell with offline argv recorders and evaluate job conditions."""

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/football-feeds.yml"
OPERATIONS = (
    "apis",
    "sync",
    "verify",
    "predict",
    "backfill",
    "scoreboard",
    "probe",
    "identity",
    "reconcile",
    "relink-fdorg",
    "relink-jc",
    "digest",
    "stats-backfill",
)


def jobs():
    return dict(
        re.findall(
            r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)",
            WORKFLOW.read_text().split("jobs:\n", 1)[1],
            re.M | re.S,
        )
    )


def step(title, job=None):
    return next(s for s in (job or WORKFLOW.read_text()).split("      - name:") if title in s)


def script(value):
    return textwrap.dedent(value.split("        run: |\n", 1)[1])


def evaluate(expression, **overrides):
    context = {
        "github.event_name": "workflow_dispatch",
        "github.event.schedule": "",
        "inputs.operation": "stats-backfill",
        "inputs.gate": False,
        "github.ref_name": "main",
        "vars.FOOTBALL_FEEDS_RUNNER": "configured",
        "needs.apis.result": "success",
        "needs.schedule-gate.outputs.run_early": "",
        "needs.schedule-gate.result": "skipped",
        "env.FEED_MODE": "apis",
        "steps.collect.outputs.slot": "late",
    } | overrides
    for key, value in context.items():
        expression = expression.replace(key, repr(value))
    expression = (
        expression.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
    )
    return eval(expression, {"__builtins__": {}, "startsWith": str.startswith}, {})


def condition(value):
    return re.search(r"if: \$\{\{ (.*?) \}\}", value).group(1)


def test_dispatch_has_exactly_ten_inputs_and_legacy_options_mapping():
    text = WORKFLOW.read_text()
    inputs = text.split("    inputs:\n", 1)[1].split("  push:", 1)[0]
    assert set(re.findall(r"^      (\w+):", inputs, re.M)) == {
        "operation",
        "source",
        "leagues",
        "seasons",
        "options",
        "endpoints",
        "max_requests",
        "resume",
        "days",
        "gate",
    }
    assert "default: '{}'" in inputs
    assert "default: 7000" in inputs and "type: boolean\n        default: true" in inputs
    for env, key in (("BACKFILL_FROM", "from_season"), ("BACKFILL_TO", "to_season")):
        assert f"{env}: ${{{{ fromJSON(inputs.options || '{{}}').{key} }}}}" in text
    assert "PROBE_KIND: ${{ fromJSON(inputs.options || '{}').probe_kind || 'seasons' }}" in text
    assert "PROBE_PER_SCOPE: ${{ fromJSON(inputs.options || '{}').per_scope || 1 }}" in text
    assert re.findall(r"- cron: '(.*?)'", text) == ["40 2 * * *", "40 7 * * *", "40 9 * * *"]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_all_dispatch_operation_job_conditions(operation):
    expected = {
        "schedule-gate": False,
        "apis": operation
        in {
            "apis",
            "verify",
            "backfill",
            "probe",
            "identity",
            "reconcile",
            "relink-fdorg",
            "relink-jc",
        },
        "sporttery": operation == "sync",
        "predict": operation == "predict",
        "scoreboard": operation in {"predict", "scoreboard"},
        "digest": operation in {"predict", "scoreboard", "digest"},
        "stats-backfill": operation == "stats-backfill",
        "candidate-review": False,
    }
    assert {
        name: bool(evaluate(condition(job), **{"inputs.operation": operation}))
        for name, job in jobs().items()
    } == expected


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-test", "football-ingest-verify-test"])
def test_manual_stats_dispatch_runs_only_its_own_job_even_on_legacy_tag(ref):
    assert {
        name for name, job in jobs().items() if evaluate(condition(job), **{"github.ref_name": ref})
    } == {"stats-backfill"}
    assert not evaluate(condition(jobs()["stats-backfill"]), **{"github.event_name": "schedule"})
    assert not evaluate(condition(jobs()["stats-backfill"]), **{"github.event_name": "push"})


def test_stats_job_lock_timeout_secret_boundaries_and_artifact():
    text = WORKFLOW.read_text()
    job = jobs()["stats-backfill"]
    assert "timeout-minutes: 300" in job
    assert "group: write-football-feed-data\n  cancel-in-progress: false" in text
    permissions = text.split("permissions:\n", 1)[1].split("# One lock", 1)[0]
    assert permissions.strip() == "contents: write"
    assert "FOOTBALL_DATA_ORG_TOKEN" not in job and "permissions:" not in job
    collector = step("Restore and backfill selected technical data", job)
    assert "API_FOOTBALL_KEY: ${{ secrets.API_FOOTBALL_KEY }}" in collector
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in collector
    assert job.count("secrets.") == 2
    assert "zucai.cloud_stats" not in job.replace(collector, "")
    assert "persist-credentials: false" in job and "ref: data/football-feeds" in job
    assert "ZUCAI_APIF_DAILY_CAP: ${{ vars.ZUCAI_APIF_DAILY_CAP }}" in job
    assert "bash ../app/scripts/publish-football-feeds.sh" in job
    assert "retention-days: 90" in job and "!cancelled()" in job
    for path in ("data/raw", "data/state", "data/stats_assets"):
        assert path in job
        assert path in jobs()["apis"]
    for chunk in text.split("      - name:"):
        if "        run:" in chunk:
            assert "${{ inputs." not in chunk.split("        run:", 1)[1]


@pytest.mark.parametrize("outcome", ["success", "failure", "skipped", "cancelled", ""])
@pytest.mark.parametrize("cancelled", [False, True])
def test_stats_publish_preserves_charged_quota_after_collector_failure(outcome, cancelled):
    job = jobs()["stats-backfill"]
    collector = step("Restore and backfill selected technical data", job)
    publish = step("Publish verified technical index and audit", job)
    assert "id: collect_stats" in collector
    assert "continue-on-error" not in collector
    expression = condition(publish).replace("!cancelled()", repr(not cancelled))
    assert bool(evaluate(expression, **{"steps.collect_stats.outcome": outcome})) is (
        not cancelled and outcome in {"success", "failure"}
    )
    assert "secrets." not in publish
    assert "bash ../app/scripts/publish-football-feeds.sh" in publish


@pytest.mark.parametrize(
    "mode,slot,expected",
    [
        ("apis", "late", True),
        ("apis", "early", False),
        ("verify", "late", False),
        ("backfill", "late", False),
        ("relink-jc", "late", False),
        ("apis", "", False),
    ],
)
def test_daily_stats_only_runs_after_normal_late_collection(mode, slot, expected):
    job = jobs()["apis"]
    daily = step("Collect late technical data", job)
    assert (
        bool(
            evaluate(
                condition(daily), **{"env.FEED_MODE": mode, "steps.collect.outputs.slot": slot}
            )
        )
        is expected
    )
    assert "continue-on-error: true" in daily
    assert "--max-requests 300" in daily and 'cd "$WORK_DIR"' in daily
    assert "mkdir" not in daily and "FOOTBALL_DATA_ORG_TOKEN" not in daily
    assert daily.count("secrets.") == 2
    assert (
        job.index('zucai.cloud_feeds "$FEED_MODE"')
        < job.index("zucai.cloud_stats daily")
        < job.index("publish-football-feeds.sh")
    )


@pytest.mark.parametrize("resume", ["true", "false"])
@pytest.mark.parametrize(
    "leagues,seasons",
    [("", ""), ("39,140", "2024,2025"), ("39;$(touch injected)", "2025`touch injected`")],
)
def test_stats_backfill_shell_preserves_arguments_without_interpolation(
    tmp_path, resume, leagues, seasons
):
    collector = step("Restore and backfill selected technical data", jobs()["stats-backfill"])
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    capture = tmp_path / "argv"
    env = dict(
        PATH=str(tmp_path) + os.pathsep + os.defpath,
        WORK_DIR=str(tmp_path / "work"),
        CAPTURE_ARGS=str(capture),
        GITHUB_RUN_ID="offline",
        GITHUB_RUN_ATTEMPT="2",
        STATS_ENDPOINTS="index,fd,statistics",
        STATS_LEAGUES=leagues,
        STATS_SEASONS=seasons,
        STATS_MAX_REQUESTS="7000",
        STATS_RESUME=resume,
    )
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(collector)], env=env, check=True, capture_output=True
    )
    argv = capture.read_bytes().decode().split("\0")[:-1]
    expected = [
        "-m",
        "zucai.cloud_stats",
        "backfill",
        "--archive",
        "../cloud-data",
        "--run-id",
        "offline-2",
        "--endpoints",
        "index,fd,statistics",
        "--max-requests",
        "7000",
    ]
    if leagues:
        expected += ["--leagues", leagues]
    if seasons:
        expected += ["--seasons", seasons]
    expected += ["--resume" if resume == "true" else "--no-resume"]
    assert argv == expected
    assert not (tmp_path / "work/injected").exists()


@pytest.mark.parametrize(
    "options,valid",
    [
        ("{}", True),
        ('{"slot":"early"}', True),
        ('{"slot":"late"}', True),
        ('{"slot":"auto"}', True),
        ('{"slot":"invalid"}', False),
        ('{"slot":false}', False),
        ('{"slot":[]}', False),
        ('{"from_season":"2022","to_season":"2026","probe_kind":"stats","per_scope":2}', True),
        ('{"typo":1}', False),
        ("[]", False),
        ("null", False),
        ("bad", False),
        ('{"probe_kind":"bad"}', False),
        ('{"per_scope":0}', False),
        ('{"per_scope":true}', False),
        ('{"from_season":[]}', False),
    ],
)
def test_unknown_or_invalid_options_fail_offline_before_any_collector(tmp_path, options, valid):
    for name in ("apis", "stats-backfill"):
        job = jobs()[name]
        validate = step("Validate dispatch options", job)
        assert "secrets." not in validate
        assert job.index("Validate dispatch options") < job.index("python -m zucai.")
        env = os.environ | {"DISPATCH_OPTIONS": options}
        result = subprocess.run(
            ["/bin/bash", "-eu", "-c", script(validate)], cwd=tmp_path, env=env, capture_output=True
        )
        assert (result.returncode == 0) is valid
        assert not list(tmp_path.iterdir())


def test_windows_sporttery_does_not_validate_unused_dispatch_options():
    job = jobs()["sporttery"]
    assert "Validate dispatch options" not in job
    assert "inputs.options" not in job
    for title in ("Install project", "Restore data, collect fixtures"):
        assert "shell: bash" in step(title, job)


def test_daily_shell_reuses_existing_workdir_and_passes_fixed_budget(tmp_path):
    daily = step("Collect late technical data", jobs()["apis"])
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    (work / "data").mkdir()
    (work / "data/zucai.db").write_bytes(b"already restored")
    capture = tmp_path / "argv"
    env = dict(
        PATH=str(tmp_path) + os.pathsep + os.defpath,
        WORK_DIR=str(work),
        CAPTURE_ARGS=str(capture),
        GITHUB_RUN_ID="daily-offline",
        GITHUB_RUN_ATTEMPT="3",
    )
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(daily)],
        env=env,
        check=True,
        capture_output=True,
    )
    assert capture.read_bytes().decode().split("\0")[:-1] == [
        "-m",
        "zucai.cloud_stats",
        "daily",
        "--archive",
        "../cloud-data",
        "--run-id",
        "daily-offline-3",
        "--max-requests",
        "300",
    ]
    assert (work / "data/zucai.db").read_bytes() == b"already restored"
