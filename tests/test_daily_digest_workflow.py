"""Evaluate the actual job conditions, including manual runs on legacy tag refs."""

import re
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
    for key, value in {"github.event_name": event, "inputs.operation": operation}.items():
        expression = expression.replace(key, repr(value))
    expression = expression.replace("!cancelled()", repr(not cancelled))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    assert eval(expression, {"__builtins__": {}}, {}) == (expected and not cancelled)
    assert "needs: [scoreboard]" in job
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
            "inputs.operation": "digest",
            "github.ref_name": ref,
            "vars.FOOTBALL_FEEDS_RUNNER": "configured",
            "needs.apis.result": "skipped",
        }.items():
            expression = expression.replace(key, repr(value))
        expression = (
            expression.replace("!cancelled()", "True").replace("&&", " and ").replace("||", " or ")
        )
        assert not eval(expression, {"__builtins__": {}, "startsWith": str.startswith}, {}), name
