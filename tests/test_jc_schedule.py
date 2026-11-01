import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import responses

from zucai.cli import main

FIXTURE = Path(__file__).parents[1] / "data/fixtures/sporttery_calculator_20261101.json"
API = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry"


def test_fetch_jc_archives_real_betting_pools_and_uses_number_date(tmp_path, monkeypatch):
    from zucai.sources.base import RawStore

    monkeypatch.chdir(tmp_path)
    # The source snapshot is genuinely pre-match. Use that instant in the offline replay.
    save = RawStore.save_response
    instant = datetime(2026, 11, 1, 12, 42, tzinfo=UTC)
    monkeypatch.setattr(
        RawStore, "save_response", lambda self, *a: save(self, *a, captured_at=instant)
    )
    with responses.RequestsMock() as api:
        api.add(responses.GET, API, body=FIXTURE.read_bytes())
        assert main(["fetch", "jc", "--date", "2026-11-01"]) == 0
    with sqlite3.connect("data/zucai.db") as db:
        fixtures = db.execute(
            "select match_id, kickoff_utc from matches order by match_id"
        ).fetchall()
        assert fixtures == [
            ("jc:2042420", "2026-11-01T13:00:00.000000Z"),
            ("jc:2042429", "2026-11-01T16:30:00.000000Z"),
        ]
        odds = dict(
            ((pool, line, opt), price)
            for pool, line, opt, price in db.execute(
                "select pool,line,selection,price from odds where match_id='jc:2042420'"
            )
        )
        assert odds["had", 0, "h"] == 3.46
        assert odds["hhad", 1, "h"] == 1.61
        assert odds["crs", 0, "1:0"] == 6.75
        assert odds["ttg", 0, "7+"] == 38
        assert odds["hafu", 0, "da"] == 4.50
        assert set(opt for pool, _, opt in odds if pool == "had") == {"h", "d", "a"}
    (raw,) = [
        p
        for p in (tmp_path / "data/raw").rglob("calculator_*.json")
        if not p.name.endswith(".meta.json")
    ]
    assert raw.read_bytes() == FIXTURE.read_bytes()


def test_quotes_are_complete_timestamped_markets_and_replays_are_idempotent(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import SportteryCalculator, save_schedule

    monkeypatch.chdir(tmp_path)
    engine = get_engine()
    init_db(engine)
    blob = json.loads(FIXTURE.read_bytes())
    row = blob["value"]["matchInfoList"][0]["subMatchList"][0]
    row["poolList"] = [
        dict(p, poolStatus="Closed") if p["poolCode"] != "HAD" else p for p in row["poolList"]
    ]
    base = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    def ingest(at):
        records = SportteryCalculator().parse(json.dumps(blob).encode())
        with session_scope(engine) as s:
            save_schedule(s, [records[0]], at)

    ingest(base)
    ingest(base + timedelta(minutes=1))
    row["had"]["h"] = "3.50"
    ingest(base + timedelta(minutes=3))
    ingest(base + timedelta(minutes=3))
    ingest(base + timedelta(minutes=13))
    ingest(
        base + timedelta(hours=1)
    )  # scheduled kickoff has arrived, even if the provider cache says Selling
    with sqlite3.connect("data/zucai.db") as db:
        assert db.execute("select count(*) from odds").fetchone() == (9,)
        assert db.execute(
            "select price from odds where selection='h' order by snapshot_utc"
        ).fetchall() == [(3.46,), (3.50,), (3.50,)]
        assert db.execute("select distinct pool from odds").fetchall() == [("had",)]
    engine.dispose()


def test_api_failure_and_malformed_live_rows_do_not_create_partial_matches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    error = {"success": True, "errorCode": "0", "value": {"wrongList": []}}
    with responses.RequestsMock() as api:
        api.add(responses.GET, API, json=error)
        assert main(["fetch", "jc"]) != 0


def test_handicap_switch_back_is_a_new_market_even_at_its_previous_price(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import SportteryCalculator, save_schedule

    monkeypatch.chdir(tmp_path)
    engine = get_engine()
    init_db(engine)
    blob = json.loads(FIXTURE.read_bytes())
    row = blob["value"]["matchInfoList"][0]["subMatchList"][0]
    row["poolList"] = [p for p in row["poolList"] if p["poolCode"] == "HHAD"]
    base = datetime(2026, 11, 1, 12, tzinfo=UTC)
    for minute, line in [(0, "-1"), (2, "-2"), (4, "-1")]:
        row["hhad"]["goalLine"] = line
        game = SportteryCalculator().parse(json.dumps(blob).encode())[0]
        with session_scope(engine) as s:
            save_schedule(s, [game], base + timedelta(minutes=minute))
    with sqlite3.connect("data/zucai.db") as db:
        assert db.execute(
            "select line from odds where selection='h' order by snapshot_utc"
        ).fetchall() == [(-1,), (-2,), (-1,)]
    engine.dispose()


def test_incomplete_extra_markets_are_omitted_without_losing_valid_had_or_other_games():
    from zucai.sources.sporttery import SportteryCalculator

    data = json.loads(FIXTURE.read_bytes())
    r = data["value"]["matchInfoList"][0]["subMatchList"][0]
    r["crs"]["s01s00"] = "--"
    r["ttg"]["s7"] = "NaN"
    r["hafu"]["da"] = "bad"
    r["hhad"]["goalLine"] = ""
    games = SportteryCalculator().parse(json.dumps(data).encode())
    assert {q.pool for q in games[0].odds} == {"had"}
    assert {q.pool for q in games[1].odds} == {"had", "hhad", "hafu", "crs", "ttg"}
