"""Evaluate the checked-in Action routing without dispatching any runs."""

import pytest

from test_stats_workflow import WORKFLOW, condition, evaluate, jobs, script, step


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-test", "football-ingest-verify-test"])
def test_manual_research_only_starts_its_own_job_on_main(ref):
    assert {
        name
        for name, job in jobs().items()
        if evaluate(condition(job), **{"inputs.operation": "web-research", "github.ref_name": ref})
    } == ({"web-research"} if ref == "main" else set())
    assert "web-research" in WORKFLOW.read_text().split("      source:", 1)[0]


@pytest.mark.parametrize("event,operation", [("schedule", ""), ("workflow_dispatch", "daily")])
def test_daily_research_requires_explicit_enable_and_confirmed_forecast(event, operation):
    expression = condition(jobs()["web-research"])
    context = {"github.event_name": event, "inputs.operation": operation}
    assert not evaluate(expression, **context)
    context["vars.FOOTBALL_WEB_RESEARCH_ENABLED"] = "true"
    assert not evaluate(expression, **context)
    context["needs.predict.result"] = "success"
    assert not evaluate(expression, **context)
    context["needs.predict.outputs.publication_confirmed"] = "success"
    assert evaluate(expression, **context)
    assert (
        "publication_confirmed: ${{ steps.publish_confirmed_forecasts.outcome }}"
        in jobs()["predict"]
    )


def test_research_has_scoped_key_and_fixed_one_match_manual_invocation():
    job = jobs()["web-research"]
    runner = step("Run independent prematch research", job)
    assert "needs: [predict]" in job
    assert "timeout-minutes: 12" in job
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in runner
    assert WORKFLOW.read_text().count("secrets.DEEPSEEK_API_KEY") == 1
    assert "args+=(--max-matches 1)" in script(runner)
    assert '--forecast-run-id "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"' in script(runner)
    assert "needs: [schedule-gate, predict, web-research]" in jobs()["scoreboard"]
    assert "needs.web-research.result" not in condition(jobs()["scoreboard"])
    assert "!cancelled()" in condition(jobs()["scoreboard"])


def test_manual_research_refuses_a_tag_named_main():
    assert not evaluate(
        condition(jobs()["web-research"]),
        **{"inputs.operation": "web-research", "github.ref_name": "main", "github.ref_type": "tag"},
    )
