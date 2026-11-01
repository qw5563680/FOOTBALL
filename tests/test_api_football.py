import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1] / "data/fixtures/t10"


def source(tmp_path, **kwargs):
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.base import RawStore

    return ApiFootball(
        store=RawStore(tmp_path / "raw"), quota_path=tmp_path / "quota.json", **kwargs
    )


def test_scores_statuses_and_rejections(tmp_path):
    s = source(tmp_path)
    games = s.parse((ROOT / "synthetic_season.json").read_bytes(), league_id=98, season=2024)
    assert games[0].provider == "apif"
    assert games[0].full == (1, 1)  # goals includes extra time
    assert games[0].kickoff_utc == datetime.fromtimestamp(1726473600, UTC)
    assert {g.status for g in games} >= {"AET", "PST", "NS", "TBD", "ABD", "FT", "PEN"}
    assert len(s.rejected) == 2
    assert all("AWD" in r["reason"] or "WO" in r["reason"] for r in s.rejected)


def test_quota_headers_authority_reset_and_fail_closed(tmp_path):
    from zucai.sources.apif_quota import DailyQuota, QuotaExceeded

    now = datetime(2026, 11, 1, tzinfo=UTC)
    p = tmp_path / "quota.json"
    q = DailyQuota(p, reserve=10, now=lambda: now)
    q.before_request()
    q.observe({"X-RateLimit-Requests-Remaining": "11"})
    assert q.remaining == 11
    q.before_request()
    with pytest.raises(QuotaExceeded):
        q.before_request()
    q.observe({"x-ratelimit-requests-remaining": "80"})
    assert q.remaining == 80
    later = DailyQuota(p, now=lambda: datetime(2026, 11, 2, tzinfo=UTC))
    later.before_request()
    assert later.remaining == 99
    p.write_text("{}")
    with pytest.raises(ValueError):
        later.before_request()


def transport(monkeypatch, pages):
    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kw):
            self.calls.append((url, kw))
            r = requests.Response()
            r.status_code = 200
            r.url = url
            r._content = pages.pop(0)
            r.headers["x-ratelimit-requests-remaining"] = str(99 - len(self.calls))
            return r

        def close(self):
            pass

    monkeypatch.setenv("API_FOOTBALL_KEY", uuid4().hex)
    return Session()


def test_pages_count_archive_and_keep_first_on_failure(tmp_path, monkeypatch):
    session = transport(
        monkeypatch,
        [
            (ROOT / "synthetic_page1.json").read_bytes(),
            (ROOT / "synthetic_page2.json").read_bytes(),
        ],
    )
    s = source(tmp_path, session=session, min_interval=0)
    pages = list(s.iter_raw(league_id=98, season=2024))
    assert len(pages) == 2 and s.request_count == 2
    assert "page=2" in session.calls[1][0]
    assert all("x-apisports-key" in kw["headers"] for _, kw in session.calls)
    assert all(p.exists() for p in pages)
    assert s.parse(pages[1].read_bytes())[0].status == "PST"


def test_missing_key_and_echo_never_archive(tmp_path, monkeypatch):
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    with pytest.raises(ValueError, match="API_FOOTBALL_KEY"):
        source(tmp_path).fetch_raw(league_id=98, season=2024)
    secret = uuid4().hex
    escaped = "".join(f"\\u{ord(c):04x}" for c in secret)
    session = transport(monkeypatch, [('{"echo":"' + escaped + '"}').encode()])
    monkeypatch.setenv("API_FOOTBALL_KEY", secret)
    with pytest.raises(ValueError, match="安全"):
        source(tmp_path, session=session, min_interval=0).fetch_raw(league_id=98, season=2024)
    assert not list((tmp_path / "raw").rglob("*.json"))


def test_http200_plan_error_is_failure(tmp_path, monkeypatch):
    session = transport(monkeypatch, [(ROOT / "synthetic_errors.json").read_bytes()])
    with pytest.raises(ValueError, match="API.*错误"):
        source(tmp_path, session=session, min_interval=0).fetch_raw(league_id=98, season=2026)


@pytest.mark.parametrize("mutation", ["scope", "count", "paging", "missing_fulltime"])
def test_bad_response_scope_or_score(tmp_path, mutation):
    doc = json.loads((ROOT / "synthetic_page1.json").read_bytes())
    doc["paging"] = {"current": 1, "total": 1}
    if mutation == "scope":
        doc["parameters"]["season"] = "2023"
    if mutation == "count":
        doc["results"] = 7
    if mutation == "paging":
        doc["paging"]["current"] = 0
    if mutation == "missing_fulltime":
        del doc["response"][0]["score"]["fulltime"]
    s = source(tmp_path)
    if mutation == "missing_fulltime":
        assert s.parse(json.dumps(doc).encode()) == [] and len(s.rejected) == 1
    else:
        with pytest.raises(ValueError):
            s.parse(json.dumps(doc).encode(), league_id=98, season=2024)


def test_daily_local_hard_cap_even_if_headers_jump_up(tmp_path):
    from zucai.sources.apif_quota import DailyQuota, QuotaExceeded

    q = DailyQuota(tmp_path / "quota.json", reserve=0)
    for _ in range(100):
        q.before_request()
        q.observe({"x-ratelimit-requests-remaining": "100"})
    with pytest.raises(QuotaExceeded):
        q.before_request()


def test_malformed_numeric_ids_are_row_rejections(tmp_path):
    doc = json.loads((ROOT / "synthetic_page1.json").read_bytes())
    doc["response"][0]["fixture"]["id"] = {"bad": 1}
    s = source(tmp_path)
    assert s.parse(json.dumps(doc).encode()) == []
    assert len(s.rejected) == 1


def test_live_extra_time_fixture_has_different_goals_and_fulltime(tmp_path):
    doc = json.loads((ROOT / "live_season.json").read_bytes())
    row = next(r for r in doc["response"] if r["fixture"]["status"]["short"] == "AET")
    games = source(tmp_path).parse(json.dumps(doc).encode(), league_id=292, season=2024)
    game = next(g for g in games if g.provider_match_id == row["fixture"]["id"])
    assert game.full == (2, 1) and row["goals"] == {"home": 3, "away": 1}


def test_real_date_states_and_unknown_league_filter(tmp_path):
    doc = json.loads((ROOT / "live_date.json").read_bytes())
    config = {str(r["league"]["id"]): {"league": r["league"]["name"]} for r in doc["response"]}
    s = source(tmp_path, leagues=config)
    games = s.parse(json.dumps(doc).encode(), date="2026-11-01")
    assert {"AET", "PST", "FT", "NS"} <= {g.status for g in games}
    assert len(s.rejected) == 1 and "AWD" in s.rejected[0]["reason"]
    selected = source(tmp_path).parse(json.dumps(doc).encode(), date="2026-11-01")
    assert all(g.competition_code in source(tmp_path).leagues for g in selected)


def test_real_mls_cancel_case_variant_is_still_cancelled(tmp_path):
    s = source(tmp_path)
    games = s.parse((ROOT / "live_status_variant.json").read_bytes(), league_id=253, season=2023)
    assert len(games) == 2 and not s.rejected
    assert all(g.status == "CANC" and g.full is None for g in games)


def test_fetch_raw_assembles_all_original_pages_with_provenance(tmp_path, monkeypatch):
    session = transport(
        monkeypatch,
        [
            (ROOT / "synthetic_page1.json").read_bytes(),
            (ROOT / "synthetic_page2.json").read_bytes(),
        ],
    )
    s = source(tmp_path, session=session, min_interval=0)
    p = s.fetch_raw(league_id=98, season=2024)
    doc = json.loads(p.read_bytes())
    assert doc["results"] == 2 and len(doc["archive_pages"]) == 2
    assert len(s.parse(p.read_bytes(), league_id=98, season=2024)) == 2
    assert all(page.exists() for page in s.pages)


def test_later_page_error_keeps_safe_original_page(tmp_path, monkeypatch):
    session = transport(
        monkeypatch,
        [
            (ROOT / "synthetic_page1.json").read_bytes(),
            (ROOT / "synthetic_errors.json").read_bytes(),
        ],
    )
    s = source(tmp_path, session=session, min_interval=0)
    with pytest.raises(ValueError, match="API.*错误"):
        s.fetch_raw(league_id=98, season=2024)
    assert s.request_count == 2
    assert (
        len(s.pages) == 2
        and s.pages[0].read_bytes() == (ROOT / "synthetic_page1.json").read_bytes()
    )


def test_shared_quota_serializes_threads_and_counts_every_request(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from zucai.sources.apif_quota import DailyQuota

    q = DailyQuota(tmp_path / "quota.json", reserve=0)
    held, release, entered = Event(), Event(), Event()

    def first():
        with q.locked():
            held.set()
            release.wait(2)
            q.before_request()

    def second():
        with q.locked():
            entered.set()
            q.before_request()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        assert held.wait(1)
        b = pool.submit(second)
        try:
            assert not entered.wait(0.1)
        finally:
            release.set()
        a.result()
        b.result()
    assert json.loads(q.path.read_bytes())["used"] == 2
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: q.before_request(), range(98)))
    assert json.loads(q.path.read_bytes())["used"] == 100


def test_unarchivable_response_still_updates_authoritative_quota(tmp_path, monkeypatch):
    from zucai.sources.apif_quota import QuotaExceeded

    session = transport(monkeypatch, [(ROOT / "synthetic_non_json.txt").read_bytes()])
    get = session.get

    def limited(url, **kwargs):
        response = get(url, **kwargs)
        response.status_code = 429
        response.headers["x-ratelimit-requests-remaining"] = "0"
        return response

    session.get = limited
    s = source(tmp_path, session=session, min_interval=0)
    with pytest.raises(ValueError, match="安全"):
        s.fetch_raw(league_id=98, season=2024)
    assert not list((tmp_path / "raw").rglob("*.json"))
    with pytest.raises(QuotaExceeded):
        s.quota.before_request()
