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
    assert "options: [apis, sync, verify, predict]" in text
