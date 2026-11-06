import csv
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import responses

from zucai.sources.sporttery import API, SportteryCalculator, fetch_schedule

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t39/jc_catalog_12.json"
CAPTURED = datetime(2026, 11, 4, 3, 30, tzinfo=UTC)
FIELDS = [
    "business_date",
    "jc_num",
    "match_id",
    "matchId",
    "home",
    "away",
    "league",
    "matchStatus",
    "sellStatus",
    "has_fixed_odds",
    "retained",
    "captured_utc",
    "kickoff_utc",
    "poolList",
    "lastUpdateTime",
    "sale_end_fields",
]


def read_audit(path):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def capture_at(monkeypatch, instant):
    from zucai.sources.base import RawStore

    save = RawStore.save_response
    monkeypatch.setattr(
        RawStore,
        "save_response",
        lambda self, *args: save(self, *args, captured_at=instant),
    )


def test_catalog_includes_all_numbers_and_skipped_rows_without_changing_selection():
    feed = SportteryCalculator()
    games = feed.parse(FIXTURE.read_bytes(), captured=CAPTURED)
    assert [game.jc_num for game in games] == [
        f"周三{number:03}" for number in range(1, 13) if number not in (3, 12)
    ]
    assert [row["jc_num"] for row in feed.catalog] == [
        f"周三{number:03}" for number in range(1, 13)
    ]
    assert len(feed.skipped) == 2
    assert [row["jc_num"] for row in feed.skipped] == ["周三003", "周三012"]
    assert {row["reason"] for row in feed.skipped} == {"match_status_not_selling"}
    for row in feed.catalog:
        assert list(row) == FIELDS
        assert row["match_id"] == "jc:" + row["matchId"]
        assert row["business_date"] == "2026-11-04"
        assert row["captured_utc"] == "2026-11-04T03:30:00.000000Z"
        assert row["kickoff_utc"] == "2026-11-04T19:00:00.000000Z"
        assert row["has_fixed_odds"] == "1"
        assert row["retained"] == ("0" if row["jc_num"] in ("周三003", "周三012") else "1")


@pytest.mark.parametrize(
    ("change", "reason", "fixed"),
    [
        ({"sellStatus": 0}, "sell_status_not_1", "1"),
        ({"poolList": []}, "no_fixed_odds", "0"),
        ({"poolOddsType": "P"}, "no_fixed_odds", "0"),
        ({"poolStatus": "Closed"}, "no_selling_fixed_pool", "1"),
        ({"single": 0, "allUp": 0}, "no_selling_fixed_pool", "1"),
        ({"had": {"h": "--", "d": "3.0", "a": "4.0"}}, "no_valid_fixed_quotes", "1"),
    ],
)
def test_skipped_reasons_follow_the_existing_fixed_pool_and_quote_rules(change, reason, fixed):
    body = json.loads(FIXTURE.read_bytes())
    row = body["value"]["matchInfoList"][0]["subMatchList"][0]
    if set(change) <= {"poolOddsType", "poolStatus", "single", "allUp"}:
        row["poolList"][0].update(change)
    else:
        row.update(change)
    feed = SportteryCalculator()
    games = feed.parse(json.dumps(body).encode())
    assert "周三001" not in {game.jc_num for game in games}
    skipped = next(row for row in feed.skipped if row["jc_num"] == "周三001")
    assert skipped["reason"] == reason
    assert skipped["has_fixed_odds"] == fixed


def test_parse_clears_previous_catalog_and_keeps_missing_skipped_kickoff_unknown():
    body = json.loads(FIXTURE.read_bytes())
    body["value"]["matchInfoList"][0]["subMatchList"][2]["matchTime"] = "unknown"
    body["value"]["matchInfoList"][0]["subMatchList"][2]["poolList"] = None
    feed = SportteryCalculator()
    assert len(feed.parse(json.dumps(body).encode())) == 10
    assert feed.catalog[2]["kickoff_utc"] == ""
    assert feed.catalog[2]["has_fixed_odds"] == "0"
    assert feed.parse(b'{"success":true,"errorCode":"0","value":{}}') == []
    assert feed.catalog == feed.skipped == []


def test_schedule_persists_complete_catalog_idempotently_without_creating_skipped_teams(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZUCAI_DB", str(tmp_path / "data/zucai.db"))
    capture_at(monkeypatch, CAPTURED)
    metadata = {}
    with responses.RequestsMock() as api:
        api.add(responses.GET, API + "getMatchCalculatorV1.qry", body=FIXTURE.read_bytes())
        assert fetch_schedule(metadata=metadata) == (10, 30)
        assert fetch_schedule() == (10, 0)
    assert metadata["captured_utc"] == "2026-11-04T03:30:00.000000Z"
    audit = tmp_path / "data/state/audit"
    fields, catalog = read_audit(audit / "jc_catalog/2026-11.csv")
    assert fields == FIELDS and len(catalog) == 12
    fields, skipped = read_audit(audit / "jc_skipped/2026-11.csv")
    assert fields == FIELDS + ["reason"] and len(skipped) == 2
    with sqlite3.connect(tmp_path / "data/zucai.db") as db:
        assert db.execute("SELECT count(*) FROM matches").fetchone() == (10,)
        assert db.execute("SELECT count(*) FROM teams").fetchone() == (20,)
        assert not db.execute(
            "SELECT 1 FROM teams WHERE name IN ('样本主队003','样本客队003',"
            "'样本主队012','样本客队012')"
        ).fetchall()
        assert not db.execute("SELECT 1 FROM matches WHERE counterpart_id IS NOT NULL").fetchall()


def test_date_filter_does_not_hide_other_business_days_from_catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZUCAI_DB", str(tmp_path / "data/zucai.db"))
    capture_at(monkeypatch, CAPTURED)
    body = json.loads(FIXTURE.read_bytes())
    following = dict(body["value"]["matchInfoList"][0]["subMatchList"][0])
    following.update(matchId=3900013, matchNumStr="周四001", matchDate="2026-11-06")
    body["value"]["matchInfoList"].append(
        {"businessDate": "2026-11-05", "subMatchList": [following]}
    )
    with responses.RequestsMock() as api:
        api.add(responses.GET, API + "getMatchCalculatorV1.qry", json=body)
        assert fetch_schedule(date(2026, 11, 5)) == (1, 3)
    _, rows = read_audit(tmp_path / "data/state/audit/jc_catalog/2026-11.csv")
    assert len(rows) == 13
    assert {row["business_date"] for row in rows} == {"2026-11-04", "2026-11-05"}


def test_a_later_capture_appends_changed_status_and_audit_survives_archive_restore(
    tmp_path, monkeypatch
):
    from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds

    monkeypatch.chdir(tmp_path)
    database = tmp_path / "data/zucai.db"
    monkeypatch.setenv("ZUCAI_DB", str(database))
    body = json.loads(FIXTURE.read_bytes())
    for instant in (CAPTURED, CAPTURED + timedelta(hours=1)):
        with monkeypatch.context() as patch:
            capture_at(patch, instant)
            with responses.RequestsMock() as api:
                api.add(responses.GET, API + "getMatchCalculatorV1.qry", json=body)
                fetch_schedule()
        body["value"]["matchInfoList"][0]["subMatchList"][-1].update(
            matchStatus="Selling", sellStatus=1
        )
    _, catalog = read_audit(tmp_path / "data/state/audit/jc_catalog/2026-11.csv")
    _, skipped = read_audit(tmp_path / "data/state/audit/jc_skipped/2026-11.csv")
    assert len(catalog) == 24 and len(skipped) == 3
    assert [row["retained"] for row in catalog if row["jc_num"] == "周三012"] == ["0", "1"]
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 4), "test-catalog")
    assert {"state/audit/jc_catalog/2026-11.csv", "state/audit/jc_skipped/2026-11.csv"} <= set(
        archive_info(archive)["files"]
    )
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, restored)
    for name in ("jc_catalog/2026-11.csv", "jc_skipped/2026-11.csv"):
        assert (restored.parent / "state/audit" / name).read_bytes() == (
            database.parent / "state/audit" / name
        ).read_bytes()


def test_conflicting_replay_at_same_capture_does_not_overwrite_audit_or_insert_new_match(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "data/zucai.db"
    monkeypatch.setenv("ZUCAI_DB", str(database))
    capture_at(monkeypatch, CAPTURED)
    audit = tmp_path / "data/state/audit"
    with responses.RequestsMock() as api:
        api.add(responses.GET, API + "getMatchCalculatorV1.qry", body=FIXTURE.read_bytes())
        fetch_schedule()
        before = {path: path.read_bytes() for path in audit.rglob("*.csv")}
        conflicting = json.loads(FIXTURE.read_bytes())
        conflicting["value"]["matchInfoList"][0]["subMatchList"][2].update(
            matchStatus="Selling", sellStatus=1
        )
        api.replace(responses.GET, API + "getMatchCalculatorV1.qry", json=conflicting)
        with pytest.raises(ValueError, match="审计冲突"):
            fetch_schedule()
    assert {path: path.read_bytes() for path in audit.rglob("*.csv")} == before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM matches").fetchone() == (10,)


def test_restoring_legacy_archive_clears_newer_local_catalog_evidence(tmp_path):
    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.db import get_engine, init_db

    source = tmp_path / "source/zucai.db"
    engine = get_engine(source)
    init_db(engine)
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(source, archive, date(2026, 11, 1), "legacy")
    target = tmp_path / "restore/data/zucai.db"
    audit = target.parent / "state/audit"
    audit.mkdir(parents=True)
    for name in ("jc_catalog.csv", "jc_skipped.csv"):
        (audit / name).write_text("newer evidence\n", encoding="utf-8")
        monthly = audit / Path(name).stem / "2026-11.csv"
        monthly.parent.mkdir()
        monthly.write_text("newer monthly evidence\n", encoding="utf-8")
    restore_feeds(archive, target)
    assert not (audit / "jc_catalog.csv").exists()
    assert not (audit / "jc_skipped.csv").exists()
    assert not list(audit.rglob("*.csv"))
