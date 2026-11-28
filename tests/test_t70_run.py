"""Offline orchestration tests without using any provider API or live archive."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import responses

from zucai import web_research as runner

NOW = datetime(2026, 11, 27, 10, tzinfo=UTC)


class Provider:
    name = "test-provider"
    model = "test-model"

    def __init__(self):
        self.calls = []
        self.reserve = Decimal("0.02")
        self.research_cost = Decimal("0.01")
        self.web_calls = 1

    def research_reserve(self, prompt, material):
        return self.reserve

    def structure_reserve(self, report, material):
        return self.reserve

    def research(self, prompt, material):
        self.calls.append("research")
        assert prompt and not {"odds", "identity_context"} & material.keys()
        return self.step(self.research_cost, self.web_calls)

    def structure(self, report, material):
        self.calls.append("structure")
        assert not {"odds", "identity_context"} & material.keys()
        return self.step(Decimal("0.01"))

    @staticmethod
    def step(cost, tool_calls=0):
        return SimpleNamespace(
            text="fixture",
            citations=[],
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=tool_calls,
            estimated_cost=cost,
            billed_cost=None,
            billed_currency=None,
            raw={"content": "fixture"},
        )


def target(number=1, kickoff=None):
    at = kickoff or NOW + timedelta(hours=1)
    identity = {
        "match_id": f"jc:{number}",
        "jc_num": "周五001",
        "home": "主队",
        "away": "客队",
        "league": "测试联赛",
        "kickoff_utc": at.isoformat(),
        "kickoff_beijing": "2026-11-27T19:00:00+08:00",
    }
    return {
        "match_id": identity["match_id"],
        "forecast_run_id": "one-1",
        "forecast_path": "forecasts/2026-11-27/one-1/forecast.json",
        "forecast_published_utc": (NOW - timedelta(minutes=2)).isoformat(),
        "forecast_sha256": "a" * 64,
        "kickoff_utc": at.isoformat(),
        "model_probabilities": {"h": 0.5, "d": 0.3, "a": 0.2},
        "market_probabilities": None,
        "material": {**identity, "odds": {"h": 2, "d": 3, "a": 4}, "recent": {}},
        "identity_context": {"target": {"match_id": identity["match_id"]}},
    }


@pytest.fixture
def freeze(monkeypatch):
    def publish(archive, run, artifacts, **_):
        return {**run, "artifacts": artifacts}

    monkeypatch.setattr(runner.archive_api, "read_run", lambda *_: None)
    monkeypatch.setattr(runner.archive_api, "publish", publish)
    monkeypatch.setattr(runner.targets, "select", lambda *_, **__: [target()])

    def parse(text, material, citations, **_):
        assert text == "fixture" and material["odds"]["h"] == 2
        assert material["identity_context"]["target"]["match_id"] == material["match_id"]
        return "ok", {"judgment": "h"}, {"warning_count": 0}

    monkeypatch.setattr(runner.validation, "parse", parse)


def run(tmp_path, provider, *, cost="0.10", clock=lambda: NOW, limit=5):
    return runner.run(
        tmp_path,
        "research-1",
        max_cost=cost,
        max_matches=limit,
        clock=clock,
        client=provider,
    )


def test_research_then_structure_freezes_originals_and_cost(tmp_path, freeze):
    provider = Provider()
    result = run(tmp_path, provider)
    assert provider.calls == ["research", "structure"]
    assert (result["status"], result["estimated_spend"]) == ("complete", "0.02")
    assert result["budget_charged"] == "0.02"
    assert result["budget_reserved"] == "0"
    (row,) = result["records"]
    assert row["status"] == "ok" and row["structured"] == {"judgment": "h"}
    assert "identity_context" not in row
    assert "identity_context" not in result["artifacts"]["jc:1"]["material"]
    assert row["forecast_sha256"] == "a" * 64
    assert row["completed_utc"] == NOW.isoformat()
    assert result["prompt_version"] == "v2"
    assert (
        result["prompt_sha256"]
        == "26d1134c00016afd335da511d5596bf5dc75069f09ace329e19c6ddfec21ee54"
    )
    assert result["valid_record_count"] == 1
    assert result["artifacts"]["jc:1"]["report_text"] == "fixture"
    assert result["artifacts"]["jc:1"]["structure_response"] == {"content": "fixture"}


@pytest.mark.parametrize(
    "limit,estimate,expected_calls,reason,row_status",
    [
        ("0.01", Decimal("0.01"), [], "cost_limit", None),
        ("0.025", Decimal("0.01"), ["research"], "cost_limit", "partial"),
        ("0.10", None, ["research"], "unknown_cost", "partial"),
        ("0.10", Decimal("0.03"), ["research"], "reserve_exceeded", "partial"),
    ],
)
def test_budget_stops_before_next_provider_request(
    tmp_path, freeze, limit, estimate, expected_calls, reason, row_status
):
    provider = Provider()
    provider.research_cost = estimate
    result = run(tmp_path, provider, cost=limit)
    assert provider.calls == expected_calls
    assert (result["status"], result["stop_reason"]) == ("partial", reason)
    assert [row["status"] for row in result["records"]] == ([row_status] if row_status else [])
    assert Decimal(result["estimated_spend"]) == Decimal(result["budget_charged"])
    assert result["budget_reserved"] == ("0.02" if reason == "unknown_cost" else "0")


@responses.activate
def test_failed_request_preserves_diagnostics_and_reservation_without_charging(
    tmp_path, freeze, monkeypatch
):
    key = "synthetic-credential-not-production"
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    responses.post(
        "https://api.deepseek.com/chat/completions",
        status=422,
        body=f'{{"error":"invalid tools, key={key}"}}',
    )
    provider = runner.DeepSeek()
    result = run(tmp_path, provider)
    assert len(responses.calls) == 1
    assert result["status"] == "partial" and result["estimated_spend"] == "0"
    assert result["budget_charged"] == "0"
    assert Decimal(result["budget_reserved"]) > 0
    (row,) = result["records"]
    assert row["status"] == "failed"
    assert row["provider_error"] == {
        "code": "http",
        "http_status": 422,
        "detail": '{"error":"invalid tools, key=[redacted]"}',
    }
    assert key not in repr(result)


def test_expiration_during_request_refuses_artifacts_after_kickoff(tmp_path, freeze, monkeypatch):
    provider = Provider()
    kickoff = NOW + timedelta(minutes=1)
    monkeypatch.setattr(runner.targets, "select", lambda *_, **__: [target(kickoff=kickoff)])
    clock = lambda: kickoff if provider.calls else NOW  # noqa: E731
    result = run(tmp_path, provider, clock=clock)
    assert provider.calls == ["research"]
    assert result["status"] == "partial" and result["stop_reason"] == "kickoff_passed"
    assert result["records"] == [] and result["artifacts"] == {}


def test_existing_run_is_idempotent_and_match_limit_is_explicit(tmp_path, freeze, monkeypatch):
    provider = Provider()
    monkeypatch.setattr(runner.targets, "select", lambda *_, **__: [target(1), target(2)])
    result = run(tmp_path, provider, limit=1)
    assert result["stop_reason"] == "match_limit" and len(result["records"]) == 1
    monkeypatch.setattr(runner.archive_api, "read_run", lambda *_: result)
    assert run(tmp_path, provider, limit=1) == result
    assert provider.calls == ["research", "structure"]


@pytest.mark.parametrize("web_calls", [0, None])
def test_search_must_happen_before_recording_a_web_prediction(tmp_path, freeze, web_calls):
    provider = Provider()
    provider.web_calls = web_calls
    result = run(tmp_path, provider)
    assert provider.calls == ["research"] and result["valid_record_count"] == 0
    (row,) = result["records"]
    assert row["status"] == "unparsed" and row["checks"]["reason"] == "web_lookup_unconfirmed"
    assert result["artifacts"][row["match_id"]]["report_text"] == "fixture"


def test_server_meter_can_confirm_web_lookup_when_call_array_is_empty():
    step = Provider.step(Decimal("0.01"))
    step.usage["server_tool_use"] = {"tool_search": 8, "tool_fetch": 0}
    assert runner._used_web_tools(step)


@pytest.mark.parametrize("count, expected", [(0, "有效 0/1"), (None, "有效场数未记录")])
def test_action_summary_reports_accepted_records_and_can_read_older_runs(
    tmp_path, monkeypatch, count, expected
):
    manifest = dict(
        status="partial",
        candidate_count=2,
        record_count=1,
        provider_requests=2,
        estimated_spend="0.008",
        stop_reason="match_limit",
    )
    if count is not None:
        manifest["valid_record_count"] = count
    monkeypatch.setattr(runner, "run", lambda *args, **kwargs: manifest)
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert runner.main(["--archive", str(tmp_path), "--run-id", "existing", "--max-cost", "1"]) == 0
    assert expected in summary.read_text(encoding="utf-8")
