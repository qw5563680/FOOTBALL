"""Offline orchestration tests without using any provider API or live archive."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from zucai import web_research as runner

NOW = datetime(2026, 11, 27, 10, tzinfo=UTC)


class Provider:
    name = "test-provider"
    model = "test-model"

    def __init__(self):
        self.calls = []
        self.reserve = Decimal("0.02")
        self.research_cost = Decimal("0.01")

    def research_reserve(self, prompt, material):
        return self.reserve

    def structure_reserve(self, report, material):
        return self.reserve

    def research(self, prompt, material):
        self.calls.append("research")
        assert prompt and "odds" not in material
        return self.step(self.research_cost)

    def structure(self, report, material):
        self.calls.append("structure")
        return self.step(Decimal("0.01"))

    @staticmethod
    def step(cost):
        return SimpleNamespace(
            text="fixture",
            citations=[],
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            tool_calls=0,
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
    (row,) = result["records"]
    assert row["status"] == "ok" and row["structured"] == {"judgment": "h"}
    assert row["forecast_sha256"] == "a" * 64
    assert row["completed_utc"] == NOW.isoformat()
    assert result["prompt_version"] == "v1" and len(result["prompt_sha256"]) == 64
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


def test_failed_request_is_counted_and_never_retried(tmp_path, freeze):
    provider = Provider()

    def failed(*_):
        provider.calls.append("research")
        raise runner.ProviderError("http_error")

    provider.research = failed
    result = run(tmp_path, provider)
    assert provider.calls == ["research"]
    assert result["status"] == "partial" and result["budget_charged"] == "0.02"
    assert result["records"][0]["status"] == "failed"


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
