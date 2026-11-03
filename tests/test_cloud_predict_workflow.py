"""Evaluate the checked-in Actions job condition, without network or extra YAML deps."""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/football-feeds.yml"


@pytest.mark.parametrize(
    ("event", "operation", "cron", "apis", "cancelled", "expected"),
    [
        ("schedule", "", "40 9 * * *", "success", False, True),
        ("schedule", "", "40 9 * * *", "failure", False, False),
        ("schedule", "", "40 2 * * *", "success", False, False),
        ("workflow_dispatch", "predict", "", "skipped", False, True),
        ("workflow_dispatch", "predict", "", "skipped", True, False),
        ("workflow_dispatch", "apis", "", "success", False, False),
        ("workflow_dispatch", "sync", "", "skipped", False, False),
        ("workflow_dispatch", "verify", "", "success", False, False),
        ("workflow_dispatch", "backfill", "", "success", False, False),
        ("workflow_dispatch", "probe", "", "success", False, False),
        ("push", "", "", "success", False, False),
    ],
)
def test_prediction_job_only_runs_after_late_schedule_or_explicit_dispatch(
    event, operation, cron, apis, cancelled, expected
):
    text = WORKFLOW.read_text()
    assert "  predict:\n" in text
    job = text.split("  predict:\n", 1)[1]
    expression = re.search(r"if: \$\{\{ (.*?) \}\}", job).group(1)
    for key, value in {
        "github.event_name": event,
        "inputs.operation": operation,
        "github.event.schedule": cron,
        "needs.apis.result": apis,
    }.items():
        expression = expression.replace(key, repr(value))
    expression = expression.replace("!cancelled()", repr(not cancelled))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    assert eval(expression, {"__builtins__": {}}, {}) is expected
    assert "needs: [apis, sporttery]" in job
    assert "runs-on: ubuntu-latest" in job and "timeout-minutes: 45" in job
    assert "retention-days: 90" in job
    assert "${{ github.workspace }}/app/data/model/params.json" in job
    assert 'pip install -e "./app[model]"' in job
    assert "needs.sporttery.result" in job
    assert "zucai.cloud_predict run" in job
    assert "publish-football-feeds.sh" in job
    assert "group: write-football-feed-data" in text
    assert "options: [apis, sync, verify, predict, backfill, scoreboard, probe]" in text


@pytest.mark.parametrize(
    ("operation", "ref_name"),
    [
        *[
            (operation, "codex/T18-fd-backfill")
            for operation in ("backfill", "apis", "verify", "sync", "predict")
        ],
        ("backfill", "football-ingest-run-t18"),
        ("backfill", "football-ingest-verify-t18"),
        ("probe", "codex/T22-apif-paid-plan"),
        ("probe", "football-ingest-run-t22"),
        ("probe", "football-ingest-verify-t22"),
        ("scoreboard", "codex/T19-scoreboard"),
        ("scoreboard", "football-ingest-run-t19"),
        ("scoreboard", "football-ingest-verify-t19"),
    ],
)
def test_backfill_is_dispatch_only_apis_job_with_existing_publish_and_artifact(operation, ref_name):
    text = WORKFLOW.read_text()
    assert "options: [apis, sync, verify, predict, backfill, scoreboard, probe]" in text
    jobs = dict(
        re.findall(r"^  (apis|sporttery|predict):\n(.*?)(?=^  \w+:\n|\Z)", text, re.M | re.S)
    )
    context = {
        "github.event_name": "workflow_dispatch",
        "inputs.operation": operation,
        "github.event.schedule": "",
        "github.ref_name": ref_name,
        "vars.FOOTBALL_FEEDS_RUNNER": "football-local",
        "needs.apis.result": "success",
    }

    def evaluate(expression):
        for key, value in context.items():
            expression = expression.replace(key, repr(value))
        expression = (
            expression.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
        )
        return eval(
            expression, {"__builtins__": {}, "startsWith": lambda a, b: a.startswith(b)}, {}
        )

    expected = {
        "apis": operation in {"apis", "verify", "backfill", "probe"},
        "sporttery": operation == "sync",
        "predict": operation == "predict",
    }
    for name, job in jobs.items():
        expression = re.search(r"if: \$\{\{ (.*?) \}\}", job).group(1)
        assert evaluate(expression) is expected[name]
    mode = re.search(r"FEED_MODE: \$\{\{ (.*?) \}\}", text).group(1)
    if operation != "scoreboard":  # This job never consumes the collection-mode variable.
        assert evaluate(mode) == (
            operation if operation in {"verify", "sync", "backfill", "probe"} else "apis"
        )
    assert "zucai.cloud_feeds backfill" in jobs["apis"]
    assert "timeout-minutes: 60" in jobs["apis"]
    assert "retention-days: 90" in jobs["apis"]
    assert "env.FEED_MODE == 'apis' || env.FEED_MODE == 'backfill'" in jobs["apis"]
    assert text.count("- cron:") == 2


def test_probe_publishes_quota_audit_without_other_operations():
    text = WORKFLOW.read_text()
    steps = text.split("      - name:")
    probe = next(step for step in steps if "Probe API-Football permissions" in step)
    assert "env.FEED_MODE == 'probe'" in probe
    assert (
        '"$PROBE_SOURCE"' in probe and '"$PROBE_LEAGUES"' in probe and '"$PROBE_SEASONS"' in probe
    )
    assert "FOOTBALL_DATA_ORG_TOKEN" not in probe
    assert "API_FOOTBALL_KEY: ${{ secrets.API_FOOTBALL_KEY }}" in probe
    for title in ("Publish verified data", "Keep collection originals"):
        step = next(step for step in steps if title in step)
        assert "env.FEED_MODE == 'probe'" in step
    collect = next(step for step in steps if "Restore and collect APIs" in step)
    assert "env.FEED_MODE != 'probe'" in collect
    job = text.split("  scoreboard:\n", 1)[1].split("  sporttery:\n", 1)[0]
    expression = re.search(r"if: \$\{\{ (.*?) \}\}", job).group(1)
    expression = expression.replace("github.event_name", repr("workflow_dispatch")).replace(
        "inputs.operation", repr("probe")
    )
    expression = (
        expression.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
    )
    assert eval(expression, {"__builtins__": {}}, {}) is False
