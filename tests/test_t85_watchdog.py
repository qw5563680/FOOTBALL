"""Offline examples of GitHub Actions run and job responses."""

import json
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest

from zucai import watchdog

ROOT = Path(__file__).parents[1]
RUNS = json.loads((ROOT / "data/fixtures/t85/runs.json").read_text(encoding="utf-8"))


def check(clock, runs, cron="5 10 * * *"):
    return watchdog.decide(datetime.fromisoformat(f"2026-11-26T{clock}:00Z"), runs, cron=cron)


@pytest.mark.parametrize(
    ("clock", "cron", "slot", "inputs"),
    [
        ("03:05", "5 3 * * *", 2, {"operation": "daily"}),
        ("03:35", "35 3 * * *", 2, {"operation": "daily"}),
        ("08:05", "5 8 * * *", 7, {"operation": "daily", "gate": True}),
        ("08:35", "35 8 * * *", 7, {"operation": "daily", "gate": True}),
        ("10:05", "5 10 * * *", 9, {"operation": "daily"}),
        ("10:35", "35 10 * * *", 9, {"operation": "daily"}),
        ("08:36", None, 7, {"operation": "daily", "gate": True}),
    ],
)
def test_missing_run_selects_slot_and_existing_inputs(clock, cron, slot, inputs):
    result = check(clock, [], cron)
    assert (result.slot, result.inputs) == (slot, inputs)


@pytest.mark.parametrize("status", ["queued", "in_progress", "pending", "waiting", "requested"])
def test_even_yesterdays_other_branch_run_blocks_dispatch(status):
    assert check("10:05", [dict(RUNS["active"], status=status)]).inputs is None


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("2026-11-25T09:40:00Z", {"operation": "daily"}),
        ("2026-11-26T09:29:59Z", {"operation": "daily"}),
        ("2026-11-26T09:30:00Z", None),
        ("2026-11-26T10:05:01Z", {"operation": "daily"}),
    ],
)
def test_scheduled_run_must_be_in_the_slot_at_the_check_time(created_at, expected):
    assert check("10:05", [dict(RUNS["scheduled"], created_at=created_at)]).inputs == expected


def test_late_watchdog_cannot_dispatch_an_expired_slot_or_a_future_slot():
    assert check("09:31", [RUNS["scheduled"]], "35 8 * * *").inputs is None
    assert check("08:05", [], "5 10 * * *").inputs is None
    assert check("02:50", [], None).inputs is None


def test_second_early_check_sees_first_checks_daily_dispatch_even_when_gate_skips_chain():
    assert check("08:05", [], "5 8 * * *").inputs == {"operation": "daily", "gate": True}
    assert check("08:35", [RUNS["daily"]], "35 8 * * *").inputs is None


def test_manual_digest_does_not_count_as_daily():
    assert check("10:05", [RUNS["manual_digest"]]).inputs == {"operation": "daily"}
    assert check("10:05", [RUNS["manual_predict"]], None).inputs == {"operation": "daily"}


def test_visible_skipped_prediction_is_repaired_once():
    scheduled = RUNS["skipped_predict"]
    assert check("10:05", [scheduled]).inputs == {"operation": "predict"}
    assert check("10:35", [scheduled, RUNS["manual_predict"]], "35 10 * * *").inputs is None
    assert check("10:35", [scheduled, RUNS["scheduled"]], "35 10 * * *").inputs is None


def test_delayed_gate_schedule_is_not_a_nine_oclock_prediction_failure():
    assert check("10:05", [RUNS["delayed_gate"]]).inputs == {"operation": "daily"}
    scheduled = dict(RUNS["delayed_gate"], created_at="2026-11-26T07:40:00Z")
    assert check("08:05", [scheduled], "5 8 * * *").inputs is None


def test_missing_job_or_unreadable_api_does_not_prove_a_missing_or_skipped_run():
    assert check("10:05", None).inputs is None
    scheduled = dict(RUNS["skipped_predict"], jobs=RUNS["skipped_predict"]["jobs"][:1])
    assert check("10:05", [scheduled]).inputs is None


@pytest.mark.parametrize(("error", "method"), [(403, "GET"), (429, "GET"), (403, "POST")])
def test_api_refusal_writes_one_summary_without_retrying_dispatch(
    tmp_path, monkeypatch, error, method
):
    summary = tmp_path / "summary"
    previous_calls = []

    def request(self, path, *, query=None, body=None):
        seen = "POST" if body is not None else "GET"
        previous_calls.append(seen)
        if seen == method:
            raise HTTPError("https://api.github.com/", error, "details", None, None)
        return {"workflow_runs": [], "total_count": 0}

    monkeypatch.setattr(watchdog.GitHub, "request", request)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("WATCHDOG_CRON", "5 8 * * *")
    watchdog.run(datetime.fromisoformat("2026-11-26T08:05:00Z"))
    lines = summary.read_text().splitlines()
    assert len(lines) == 1 and "07:40" in lines[0] and str(error) in lines[0]
    assert previous_calls.count("POST") == int(method == "POST")


def test_adapter_sends_boolean_gate_to_main_and_rechecks_completed_run(tmp_path, monkeypatch):
    summary = tmp_path / "summary"
    first = datetime.fromisoformat("2026-11-26T08:05:00Z")
    second = datetime.fromisoformat("2026-11-26T08:35:00Z")
    calls = []
    base = "/repos/example/repo/actions"
    workflow = f"{base}/workflows/football-feeds.yml"

    def request(self, path, *, query=None, body=None):
        if body is not None:
            assert path == f"{workflow}/dispatches"
            calls.append(body)
            return None
        if path == f"{workflow}/runs":
            if query.get("status"):
                assert "branch" not in query and "created" not in query
                return {"workflow_runs": [], "total_count": 0}
            assert query["branch"] == "main"
            assert query["created"] == ">=2026-11-26T07:30:00+00:00"
            rows = [{k: v for k, v in RUNS["daily"].items() if k != "jobs"}] if calls else []
            return {"workflow_runs": rows, "total_count": len(rows)}
        assert path == f"{base}/runs/104/attempts/1/jobs"
        return {"jobs": RUNS["daily"]["jobs"], "total_count": len(RUNS["daily"]["jobs"])}

    monkeypatch.setattr(watchdog.GitHub, "request", request)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("WATCHDOG_CRON", "5 8 * * *")
    watchdog.run(first)
    monkeypatch.setenv("WATCHDOG_CRON", "35 8 * * *")
    watchdog.run(second)

    assert calls == [{"ref": "main", "inputs": {"operation": "daily", "gate": True}}]
    first_line, second_line = summary.read_text().splitlines()
    assert "07:40" in first_line and "已请求补跑 daily" in first_line
    assert "07:40" in second_line and "本档已有手动整条链" in second_line
