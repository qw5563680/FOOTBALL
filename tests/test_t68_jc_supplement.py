"""T68 item 12: JC daily supplement pages archive raw originals only, via the online entry."""

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from test_t61_catalogue_archive import database_at as database_at
from test_t61_jc_catalogue import actions as actions
from zucai.cloud_feeds import archive_info, export_feeds, main
from zucai.sources.sporttery import API

FIXTURES = Path(__file__).parents[1] / "data" / "fixtures" / "t68-supplement"
API_URL = API + "getUniformMatchResultV1.qry"
PAGES = {
    "2026-11-15": "day-a",
    "2026-11-16": "day-b",
    "2026-11-17": "day-c",
    "2026-11-18": "day-d",
}
TEST_NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)  # Beijing 2026-11-19 → end 11-18
TEST_FETCH = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)  # after every day-end in window


def fake_datetime(fixed):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    return FakeDateTime


def sidecar_bytes(day, page, raw):
    url = (
        f"{API_URL}?matchBeginDate={day}&matchEndDate={day}&leagueId=&pageSize=30"
        f"&pageNo={page}&isFix=0&matchPage=1&pcOrWap=1"
    )
    return json.dumps(
        {
            "url": url,
            "fetched_at_utc": "2026-11-16T08:00:00Z",
            "status_code": 200,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    ).encode()


def seed_archive(tmp_path, *, census=True, pre_archived=()):
    seed = tmp_path / "seed"
    source = database_at(seed / "data" / "zucai.db")
    if census:
        jc_raw = json.dumps(
            {
                "collection": "catalogue",
                "provider": "jc",
                "run_id": "census-1",
                "window": {"start_beijing": "2025-11-01", "end_beijing": "2026-11-14"},
            }
        ).encode()
        folder = seed / "data" / "state" / "catalogue" / "census-1"
        folder.mkdir(parents=True)
        (folder / "jc.json").write_bytes(jc_raw)
        latest = {
            "version": 1,
            "providers": {
                "jc": {
                    "run_id": "census-1",
                    "path": "state/catalogue/census-1/jc.json",
                    "sha256": hashlib.sha256(jc_raw).hexdigest(),
                }
            },
        }
        (seed / "data" / "state" / "catalogue" / "latest.json").write_text(
            json.dumps(latest), encoding="utf-8"
        )
    for day, stem, page in pre_archived:
        raw = (FIXTURES / f"{stem}-p{page}.json").read_bytes()
        target = seed / "data" / "state" / "catalogue" / "supplement" / "jc" / day[:7]
        target.mkdir(parents=True, exist_ok=True)
        page_path = target / f"{day}-p{page}.json"
        page_path.write_bytes(raw)
        page_path.with_suffix(".json.meta.json").write_bytes(sidecar_bytes(day, page, raw))
    archive = tmp_path / "archive"
    export_feeds(source, archive, date(2026, 11, 14), "seed")
    return archive


def register_pages(seen=None, fail_pages=()):
    def callback(request):
        query = parse_qs(urlparse(request.url).query)
        day, page = query["matchBeginDate"][0], int(query["pageNo"][0])
        assert query["matchEndDate"] == [day]
        assert query["pageSize"] == ["30"]
        if seen is not None:
            seen.append((day, page))
        if (day, page) in fail_pages:
            return 404, {}, b"offline failure"
        path = FIXTURES / f"{PAGES[day]}-p{page}.json"
        if not path.is_file():
            return 404, {}, b"missing fixture"
        return 200, {"Content-Type": "application/json"}, path.read_bytes()

    responses.add_callback(responses.GET, API_URL, callback=callback)


def run_entry(tmp_path, monkeypatch, archive, *, max_requests=5, fetch=TEST_FETCH):
    work = tmp_path / "work"
    (work / "data").mkdir(parents=True)
    actions(monkeypatch)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    monkeypatch.setattr("zucai.cloud_jc_catalogue.datetime", fake_datetime(TEST_NOW))
    monkeypatch.setattr("zucai.sources.base.datetime", fake_datetime(fetch))
    monkeypatch.chdir(work)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zucai.cloud_feeds",
            "probe",
            "--probe-kind",
            "jc-supplement",
            "--archive",
            str(archive),
            "--run-id",
            "jc-supp-offline",
            "--max-requests",
            str(max_requests),
        ],
    )
    main()
    return work, summary


def matches_count(work):
    connection = sqlite3.connect(work / "data" / "zucai.db")
    try:
        return connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    finally:
        connection.close()


@responses.activate
def test_supplement_archives_all_days_after_census(tmp_path, monkeypatch):
    archive = seed_archive(tmp_path)
    seen = []
    register_pages(seen)
    work, summary = run_entry(tmp_path, monkeypatch, archive)
    meta = archive_info(archive)
    for day in ("2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18"):
        key = f"state/catalogue/supplement/jc/{day[:7]}/{day}-p1.json"
        assert key in meta["files"] and f"{key}.meta.json" in meta["files"]
    assert "state/catalogue/supplement/jc/2026-11/2026-11-15-p2.json" in meta["files"]
    assert "state/catalogue/supplement/summary.json" in meta["files"]
    record = meta["collection"]["jc_supplement"]
    assert record["status"] == "complete"
    assert record["requests"] == 5
    assert record["window"] == {
        "start_beijing": "2026-11-15",
        "end_beijing": "2026-11-18",
        "basis": "days_after_previous_jc_catalogue_until_yesterday_beijing",
    }
    assert record["days"]["archived"] == [
        "2026-11-15",
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
    ]
    assert record["days"]["skipped"] == []
    assert record["days"]["remaining"] == []
    assert record["days"]["failed"] == {}
    assert seen == [
        ("2026-11-15", 1),
        ("2026-11-15", 2),
        ("2026-11-16", 1),
        ("2026-11-17", 1),
        ("2026-11-18", 1),
    ]
    text = summary.read_text(encoding="utf-8")
    assert "### JC supplement: complete" in text
    assert "新归档 4 天" in text
    assert "还差：无" in text
    assert matches_count(work) == 0


@responses.activate
def test_supplement_skips_days_already_archived(tmp_path, monkeypatch):
    archive = seed_archive(
        tmp_path, pre_archived=[("2026-11-15", "day-a", 1), ("2026-11-15", "day-a", 2)]
    )
    seen = []
    register_pages(seen)
    work, summary = run_entry(tmp_path, monkeypatch, archive)
    record = archive_info(archive)["collection"]["jc_supplement"]
    assert record["status"] == "complete"
    assert record["requests"] == 3
    assert record["days"]["skipped"] == ["2026-11-15"]
    assert record["days"]["archived"] == ["2026-11-16", "2026-11-17", "2026-11-18"]
    assert seen == [("2026-11-16", 1), ("2026-11-17", 1), ("2026-11-18", 1)]
    assert "跳过已归档 1 天" in summary.read_text(encoding="utf-8")
    assert matches_count(work) == 0


@responses.activate
def test_supplement_never_writes_a_partially_fetched_day(tmp_path, monkeypatch):
    archive = seed_archive(tmp_path)
    seen = []
    register_pages(seen, fail_pages={("2026-11-15", 2)})
    work, _summary = run_entry(tmp_path, monkeypatch, archive)
    meta = archive_info(archive)
    record = meta["collection"]["jc_supplement"]
    assert record["status"] == "partial"
    assert record["requests"] == 5
    assert "2026-11-15" in record["days"]["failed"]
    assert record["days"]["archived"] == ["2026-11-16", "2026-11-17", "2026-11-18"]
    assert seen == [
        ("2026-11-15", 1),
        ("2026-11-15", 2),
        ("2026-11-16", 1),
        ("2026-11-17", 1),
        ("2026-11-18", 1),
    ]
    assert not any(
        key.startswith("state/catalogue/supplement/jc/2026-11/2026-11-15-") for key in meta["files"]
    )
    assert matches_count(work) == 0


@responses.activate
def test_supplement_stops_at_request_cap_and_lists_remaining_days(tmp_path, monkeypatch):
    archive = seed_archive(tmp_path)
    seen = []
    register_pages(seen)
    work, summary = run_entry(tmp_path, monkeypatch, archive, max_requests=1)
    meta = archive_info(archive)
    record = meta["collection"]["jc_supplement"]
    assert record["status"] == "partial"
    assert record["requests"] == 1
    assert record["days"]["remaining"] == [
        "2026-11-15",
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
    ]
    assert record["days"]["archived"] == [] and record["days"]["skipped"] == []
    assert record["errors"][0]["error"] == "request_cap"
    assert seen == [("2026-11-15", 1)]
    assert not any(key.startswith("state/catalogue/supplement/jc/") for key in meta["files"])
    assert "state/catalogue/supplement/summary.json" in meta["files"]
    text = summary.read_text(encoding="utf-8")
    assert "### JC supplement: partial" in text
    assert "还差：2026-11-15、2026-11-16、2026-11-17、2026-11-18" in text
    assert matches_count(work) == 0


@responses.activate
def test_supplement_rejects_capture_earlier_than_day_end(tmp_path, monkeypatch):
    archive = seed_archive(tmp_path)
    seen = []
    register_pages(seen)
    work, _summary = run_entry(
        tmp_path, monkeypatch, archive, fetch=datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    )
    meta = archive_info(archive)
    record = meta["collection"]["jc_supplement"]
    assert record["status"] == "partial"
    assert record["requests"] == 4
    assert set(record["days"]["failed"]) == {
        "2026-11-15",
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
    }
    assert all("抓取时间早于当天结束" in reason for reason in record["days"]["failed"].values())
    assert not any(key.startswith("state/catalogue/supplement/jc/") for key in meta["files"])
    assert matches_count(work) == 0


def test_supplement_requires_a_previous_census(tmp_path, monkeypatch):
    archive = seed_archive(tmp_path, census=False)
    work = tmp_path / "work"
    (work / "data").mkdir(parents=True)
    actions(monkeypatch)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zucai.cloud_feeds",
            "probe",
            "--probe-kind",
            "jc-supplement",
            "--archive",
            str(archive),
            "--run-id",
            "jc-supp-offline",
        ],
    )
    with pytest.raises(ValueError, match="缺少竞彩普查"):
        main()


def test_supplement_rejects_invalid_run_id(tmp_path, monkeypatch):
    archive = seed_archive(tmp_path)
    work = tmp_path / "work"
    (work / "data").mkdir(parents=True)
    actions(monkeypatch)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zucai.cloud_feeds",
            "probe",
            "--probe-kind",
            "jc-supplement",
            "--archive",
            str(archive),
            "--run-id",
            "bad run!",
        ],
    )
    with pytest.raises(ValueError, match="run_id 无效"):
        main()
