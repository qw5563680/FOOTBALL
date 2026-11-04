"""Technical requests use mocked responses and independent minute/day budgets."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import responses

from zucai.sources.api_football import ApiFootball
from zucai.sources.base import RawStore
from zucai.stats_client import LineupWindowMissed, StatsClient, StatsStopped

FIX = Path(__file__).parents[1] / "data/fixtures"


class Clock:
    def __init__(self):
        self.value = datetime.fromisoformat(
            json.loads((FIX / "t32/runtime.json").read_text())["now"]
        )
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


def client(tmp_path, monkeypatch, *, remaining=7500, max_requests=7000):
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-test-placeholder")
    runtime = json.loads((FIX / "t32/runtime.json").read_text())
    runtime["quota"]["remaining"] = remaining
    quota = tmp_path / "quota.json"
    quota.write_text(json.dumps(runtime["quota"]))
    clock = Clock()
    source = StatsClient(
        store=RawStore(tmp_path / "raw"),
        quota_path=quota,
        max_requests=max_requests,
        now=clock.now,
        sleep=clock.sleep,
    )
    return source, clock, runtime


def request(source):
    return source.fetch("statistics", league_id=39, season=2024, fixture_id="102")


def test_minute_headers_wait_next_minute_and_default_client_stays_unchanged(tmp_path, monkeypatch):
    source, clock, runtime = client(tmp_path, monkeypatch)
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures/statistics?fixture=102",
            body=(FIX / "t30/statistics.json").read_bytes(),
            headers=runtime["headers"],
        )
        path = request(source)
        request(source)
        assert len(http.calls) == 2
    assert clock.sleeps == [50.0]
    assert source.store.load_metadata(path)["quota_headers"]["x-ratelimit-limit"] == "300"
    assert source.request_count == 2 and ApiFootball.limiter.min_interval_s == 6.1


def test_minimum_one_second_without_minute_headers(tmp_path, monkeypatch):
    source, clock, _ = client(tmp_path, monkeypatch)
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures/statistics?fixture=102",
            body=(FIX / "t30/statistics.json").read_bytes(),
        )
        request(source)
        request(source)
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("remaining", [310, 309, 0])
def test_daily_reserve_includes_300_and_does_not_request(tmp_path, monkeypatch, remaining):
    source, _, _ = client(tmp_path, monkeypatch, remaining=remaining)
    with pytest.raises(StatsStopped, match="日常预留"):
        request(source)
    assert source.request_count == 0


def test_429_archived_and_immediately_stops(tmp_path, monkeypatch):
    source, _, _ = client(tmp_path, monkeypatch)
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures/statistics?fixture=102",
            body=(FIX / "t30/statistics.json").read_bytes(),
            status=429,
        )
        with pytest.raises(StatsStopped, match="429"):
            request(source)
        with pytest.raises(StatsStopped):
            request(source)
        assert len(http.calls) == 1
    assert source.request_count == 1
    assert source.store.load_metadata(source.pages[-1])["status_code"] == 429


def test_max_requests_and_endpoint_scope(tmp_path, monkeypatch):
    source, _, _ = client(tmp_path, monkeypatch, max_requests=1)
    with pytest.raises(ValueError):
        source.fetch("predictions", league_id=39, season=2024, fixture_id="102")
    with pytest.raises(ValueError):
        source.fetch("injuries", league_id=94, season=2024)
    with pytest.raises(ValueError):
        source.fetch("fixtures", league_id=3, season=2024)
    assert source.request_count == 0
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures/statistics?fixture=102",
            body=(FIX / "t30/statistics.json").read_bytes(),
        )
        request(source)
        with pytest.raises(StatsStopped, match="max_requests"):
            request(source)
        assert len(http.calls) == 1


def test_non_json_429_stops_without_archiving_unsafe_body(tmp_path, monkeypatch):
    source, _, runtime = client(tmp_path, monkeypatch)
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures/statistics?fixture=102",
            body=runtime["non_json_error"],
            status=429,
        )
        with pytest.raises(StatsStopped, match="429"):
            request(source)
        with pytest.raises(StatsStopped, match="429"):
            request(source)
        assert len(http.calls) == 1
    assert source.request_count == 1 and source.pages == [] and source.last_http == 429


def test_pregame_window_rechecked_after_minute_wait_before_charging(tmp_path, monkeypatch):
    source, clock, runtime = client(tmp_path, monkeypatch)
    kickoff = clock.now() + timedelta(minutes=30, seconds=30)
    with responses.RequestsMock() as http:
        http.get(
            "https://v3.football.api-sports.io/fixtures/statistics?fixture=102",
            body=(FIX / "t30/statistics.json").read_bytes(),
            headers=runtime["headers"],
        )
        request(source)
        with pytest.raises(LineupWindowMissed):
            source.fetch(
                "lineups",
                league_id=39,
                season=2024,
                fixture_id="102",
                kickoff_utc=kickoff.isoformat(),
            )
        assert len(http.calls) == 1
    assert clock.sleeps == [50.0] and source.request_count == 1
