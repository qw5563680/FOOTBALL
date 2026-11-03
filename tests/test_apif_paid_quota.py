import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zucai.sources.apif_quota import DailyQuota, QuotaExceeded

CASES = json.loads((Path(__file__).parents[1] / "data/fixtures/t22/quota_cases.json").read_bytes())
NOW = datetime(2026, 11, 3, 2, tzinfo=UTC)


def quota(tmp_path, state=None, **kwargs):
    path = tmp_path / "quota.json"
    if state is not None:
        path.write_text(json.dumps(state))
    return DailyQuota(path, now=lambda: NOW, **kwargs)


def test_paid_headers_and_large_reserve(tmp_path):
    q = quota(tmp_path, reserve=7000)
    q.observe(CASES["paid_headers"])
    q.before_request()
    saved = json.loads(q.path.read_bytes())
    assert saved["limit_day"] == 7500 and saved["remaining"] == 7497
    assert saved["observed_utc"] == NOW.isoformat()
    assert q.describe()["quota_fallback"] is False


def test_missing_headers_and_legacy_budget_not_reset(tmp_path):
    q = quota(tmp_path, CASES["legacy"])
    q.observe({})
    q.before_request()
    assert q.remaining == 87
    assert q.describe()["limit_day"] == 100 and q.describe()["quota_fallback"]
    q = quota(tmp_path, {**CASES["paid"], "remaining": None})
    q.before_request()
    assert q.remaining == 7497


def test_paid_rollover_keeps_plan_but_resets_counters(tmp_path):
    q = quota(tmp_path, {**CASES["paid"], "date_utc": "2026-11-02", "remaining": 0})
    q.before_request()
    assert q.remaining == 7499 and q.describe()["plan"] == "Pro"


@pytest.mark.parametrize("reserve", [-1, True, 7500])
def test_reserve_boundary(tmp_path, reserve):
    with pytest.raises(ValueError, match="reserve"):
        quota(tmp_path, CASES["paid"], reserve=reserve).before_request()


def test_local_lower_cap_and_header_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("ZUCAI_APIF_DAILY_CAP", "3")
    q = quota(tmp_path, CASES["paid"], reserve=0)
    q.before_request()
    q.observe(CASES["paid_headers"])
    with pytest.raises(QuotaExceeded):
        q.before_request()
    assert q._read()["used"] == 3


def test_downgrade_and_invalid_headers_do_not_grant_budget(tmp_path):
    q = quota(tmp_path, {**CASES["paid"], "used": 101})
    q.observe({"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": "90"})
    with pytest.raises(QuotaExceeded):
        q.before_request()
    previous = q.path.read_bytes()
    with pytest.raises(ValueError):
        q.observe({"x-ratelimit-requests-limit": "7500", "x-ratelimit-requests-remaining": "9999"})
    assert q.path.read_bytes() == previous


@pytest.mark.parametrize(
    "field,value",
    [
        ("limit_day", True),
        ("limit_day", 0),
        ("plan", []),
        ("observed_utc", "yesterday"),
        ("observed_utc", "2026-11-03T01:00:00"),
        ("remaining", 7501),
    ],
)
def test_new_state_validation(tmp_path, field, value):
    with pytest.raises(ValueError, match="配额"):
        quota(tmp_path, {**CASES["paid"], field: value}).before_request()


def test_invalid_quota_header_keeps_safe_response(tmp_path, monkeypatch):
    import responses

    from zucai.sources.api_football import ApiFootball
    from zucai.sources.base import RawStore

    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-paid-placeholder")
    raw = (Path(__file__).parents[1] / "data/fixtures/t10/synthetic_page1.json").read_bytes()
    client = ApiFootball(
        store=RawStore(tmp_path / "raw"), quota_path=tmp_path / "q.json", min_interval=0
    )
    with responses.RequestsMock() as http:
        http.get(
            client.request_url(league_id=98, season=2024),
            body=raw,
            headers={
                "x-ratelimit-requests-limit": "7500",
                "x-ratelimit-requests-remaining": "9999",
            },
        )
        with pytest.raises(ValueError, match="配额"):
            client.fetch_raw(league_id=98, season=2024)
    assert len(client.pages) == 1 and client.pages[0].read_bytes() == raw
    assert client.quota._read()["used"] == 1


def test_status_updates_plan_limit_current(tmp_path, monkeypatch):
    import responses

    from zucai.sources.api_football import API, ApiFootball
    from zucai.sources.base import RawStore

    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-paid-placeholder")
    client = ApiFootball(
        store=RawStore(tmp_path / "raw"), quota_path=tmp_path / "q.json", min_interval=0
    )
    with responses.RequestsMock() as http:
        http.get(API + "/status", json=CASES["status"], headers=CASES["paid_headers"])
        observation = client.status()
    assert client.quota.describe()["plan"] == "Pro"
    assert observation["current"] == 2 and observation["limit_day"] == 7500
    assert len(observation["sha256"]) == 64
    assert client.quota._read()["used"] == 1


def test_conflicting_status_limit_cannot_replenish_reserve(tmp_path):
    q = quota(tmp_path, {**CASES["paid"], "remaining": 11})
    q.before_request()
    headers = {"x-ratelimit-requests-limit": "7500"}
    q.observe(headers)
    doc = json.loads(json.dumps(CASES["status"]))
    doc["response"]["requests"] = {"limit_day": 100, "current": 99}
    saved = q.path.read_bytes()
    with pytest.raises(ValueError, match="status"):
        q.observe_status(doc, headers)
    assert q.path.read_bytes() == saved and q.remaining == 10
    with pytest.raises(QuotaExceeded):
        q.before_request()


def test_status_body_applied_before_other_client_consumes_quota(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import responses

    from zucai.sources.api_football import API, ApiFootball
    from zucai.sources.base import RawStore

    other = quota(tmp_path, {**CASES["paid"], "remaining": 12})
    client = ApiFootball(store=RawStore(tmp_path / "raw"), quota_path=other.path, min_interval=0)
    client.quota.now = lambda: NOW
    monkeypatch.setenv("API_FOOTBALL_KEY", "offline-race-placeholder")
    doc = json.loads(json.dumps(CASES["status"]))
    doc["response"]["requests"]["current"] = 7489
    body_ready, release_body, other_consumed = Event(), Event(), Event()
    apply_body = client.quota.observe_status

    def delayed_body(document, headers):
        body_ready.set()
        assert release_body.wait(2)
        apply_body(document, headers)

    def consume():
        other.before_request()
        other_consumed.set()

    monkeypatch.setattr(client.quota, "observe_status", delayed_body)
    with responses.RequestsMock() as http, ThreadPoolExecutor(max_workers=2) as pool:
        http.get(API + "/status", json=doc)
        first = pool.submit(client.status)
        assert body_ready.wait(1)
        second = pool.submit(consume)
        try:
            assert not other_consumed.wait(0.1)
        finally:
            release_body.set()
        first.result()
        second.result()
    assert other._read()["remaining"] == 10
    with pytest.raises(QuotaExceeded):
        other.before_request()
