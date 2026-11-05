"""Evaluate the actual job conditions, including manual runs on legacy tag refs."""

import os
import re
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/football-feeds.yml"


@pytest.mark.parametrize(
    "event,operation,expected",
    [
        ("schedule", "", True),
        ("workflow_dispatch", "predict", True),
        ("workflow_dispatch", "scoreboard", True),
        ("workflow_dispatch", "digest", True),
        ("workflow_dispatch", "apis", False),
        ("workflow_dispatch", "sync", False),
        ("workflow_dispatch", "backfill", False),
        ("push", "", False),
    ],
)
@pytest.mark.parametrize("cancelled", [False, True])
def test_digest_follows_scoreboard_or_explicit_dispatch(event, operation, expected, cancelled):
    text = WORKFLOW.read_text()
    job = re.search(r"^  digest:\n(.*?)(?=^  \w+:\n|\Z)", text, re.M | re.S).group(1)
    expression = re.search(r"if: \$\{\{ (.*?) \}\}", job).group(1)
    for key, value in {
        "github.event_name": event,
        "github.event.schedule": "40 2 * * *",
        "inputs.operation": operation,
        "needs.schedule-gate.outputs.run_early": "",
        "needs.schedule-gate.result": "skipped",
    }.items():
        expression = expression.replace(key, repr(value))
    expression = expression.replace("!cancelled()", repr(not cancelled))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    assert eval(expression, {"__builtins__": {}}, {}) == (expected and not cancelled)
    assert "needs: [schedule-gate, scoreboard, sporttery]" in job
    assert "API_FOOTBALL_KEY" not in job and "FOOTBALL_DATA_ORG_TOKEN" not in job
    assert "secrets.GITHUB_TOKEN" in job
    assert "zucai.cloud_feeds" not in job and "zucai.cloud_predict" not in job
    assert (
        job.index("zucai.daily_digest")
        < job.index("publish-football-feeds.sh")
        < job.index("zucai.digest_notify")
    )
    permissions = re.search(r"^permissions:\n((?:  .*\n)+)", text, re.M).group(1)
    assert set(re.findall(r"^  (\S+): (\S+)", permissions, re.M)) == {
        ("contents", "write"),
    }

    assert "permissions:\n      contents: write\n      issues: write" in job
    assert text.count("issues: write") == 1


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-test", "football-ingest-verify-test"])
def test_digest_dispatch_skips_all_collect_train_score_jobs(ref):
    text = WORKFLOW.read_text()
    jobs = dict(
        re.findall(
            r"^  (apis|sporttery|predict|scoreboard):\n(.*?)(?=^  \w+:\n|\Z)", text, re.M | re.S
        )
    )
    for name, job in jobs.items():
        expression = re.search(r"if: \$\{\{ (.*?) \}\}", job).group(1)
        for key, value in {
            "github.event_name": "workflow_dispatch",
            "github.event.schedule": "",
            "inputs.operation": "digest",
            "github.ref_name": ref,
            "vars.FOOTBALL_FEEDS_RUNNER": "configured",
            "needs.apis.result": "skipped",
            "needs.schedule-gate.outputs.run_early": "",
            "needs.schedule-gate.result": "skipped",
        }.items():
            expression = expression.replace(key, repr(value))
        expression = (
            expression.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
        )
        assert not eval(expression, {"__builtins__": {}, "startsWith": str.startswith}, {}), name


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped", ""])
@pytest.mark.parametrize("cancelled", [True, False])
def test_notification_receipt_publish_runs_after_comment_success_even_if_cleanup_fails(
    outcome, cancelled
):
    from test_stats_workflow import condition, evaluate, jobs, step

    job = jobs()["digest"]
    render = step("Render daily digest", job)
    notify = step("Update monthly report Issue", job)
    receipt = step("Publish successful notification receipts", job)
    assert "--require-notified-history" in render
    assert "--history-root cloud-data/daily" in notify and "id: notify_digest" in notify
    assert job.index(render) < job.index(notify) < job.index(receipt)
    expression = condition(receipt).replace("!cancelled()", repr(not cancelled))
    assert evaluate(expression, **{"steps.notify_digest.outcome": outcome}) is (
        not cancelled and outcome in {"success", "failure"}
    )
    assert "secrets." not in receipt
    assert "publish-football-feeds.sh" in receipt


@pytest.mark.parametrize("has_receipt", [False, True])
def test_receipt_publish_script_only_pushes_when_a_success_record_was_written(
    tmp_path, has_receipt
):
    from test_stats_workflow import jobs, script, step

    receipt = step("Publish successful notification receipts", jobs()["digest"])
    for name, source in {
        "git": '#!/bin/bash\nprintf "%s\\n" "$@" > "$STATUS_ARGS"\nprintf "%s" "$RECEIPT_STATUS"\n',
        "bash": '#!/bin/bash\nprintf "%s\\n" "$@" > "$PUBLISH_ARGS"\n',
    }.items():
        recorder = tmp_path / name
        recorder.write_text(source)
        recorder.chmod(0o755)
    publish_args, status_args = tmp_path / "publish", tmp_path / "status"
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(receipt)],
        cwd=tmp_path,
        env=os.environ
        | {
            "PATH": str(tmp_path) + os.pathsep + os.defpath,
            "PUBLISH_ARGS": str(publish_args),
            "STATUS_ARGS": str(status_args),
            "RECEIPT_STATUS": "?? daily/2026-11-03/run.notification.json" if has_receipt else "",
        },
        check=True,
        capture_output=True,
    )
    assert status_args.read_text().splitlines() == [
        "status",
        "--porcelain",
        "--",
        ":(glob)daily/*/*.notification.json",
    ]
    assert publish_args.exists() is has_receipt
    if has_receipt:
        assert publish_args.read_text().splitlines() == ["../app/scripts/publish-football-feeds.sh"]
