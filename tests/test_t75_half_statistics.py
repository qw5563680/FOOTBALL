"""T75 step A: half-time statistics from real probe originals, offline only."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, MatchHalfStat, MatchStat, Team
from zucai.sources.apif_stats import instant, parse_endpoint, parse_half_statistics
from zucai.sources.base import RawStore
from zucai.stats_schema import migrate_stats
from zucai.stats_storage import ingest_apif, read_audit

FIX = Path(__file__).parents[1] / "data/fixtures/t75"
INDEX = json.loads((FIX / "README.json").read_bytes())
CAPTURED = "2026-11-18T18:13:39.714784Z"


def fixture(case):
    return json.loads((FIX / case / "fixture.json").read_bytes())


def raw(case, name="half"):
    return (FIX / case / f"{name}.json").read_bytes()


def by_key(rows):
    return {row["record_key"]: row for row in rows}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "feeds.db"
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        team_id = 0
        for case in INDEX["cases"]:
            game = fixture(case)
            ids = []
            for side in ("home", "away"):
                team_id += 1
                session.add(Team(id=team_id, name=f"{case}-{game['teams'][side]['name']}"))
                ids.append(team_id)
            session.flush()
            fixture_id = str(game["fixture"]["id"])
            session.add(
                Match(
                    match_id=f"apif:{fixture_id}",
                    source="apif",
                    league=game["league"]["name"],
                    home_team_id=ids[0],
                    away_team_id=ids[1],
                    kickoff_utc=game["fixture"]["date"],
                    status="finished",
                )
            )
            session.flush()
            session.add(
                ApifFixtureIndex(
                    provider_fixture_id=fixture_id,
                    match_id=f"apif:{fixture_id}",
                    league=game["league"]["name"],
                    provider_league_id=game["league"]["id"],
                    season=game["league"]["season"],
                    kickoff_utc=game["fixture"]["date"],
                    home_provider_id=game["teams"]["home"]["id"],
                    away_provider_id=game["teams"]["away"]["id"],
                    home_team_id=ids[0],
                    away_team_id=ids[1],
                    captured_utc=CAPTURED,
                    raw_sha256=INDEX["cases"][case]["fixtures_page_sha256"],
                    raw_location="test",
                    parser_version="t32",
                    evidence={"fixture": game},
                )
            )
    engine.dispose()
    return path


def counts(database):
    with sqlite3.connect(database) as db:
        return tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("match_half_stats", "match_stats")
        )


def tampered(tmp_path, case, change):
    document = json.loads(raw(case))
    change(document)
    fixture_id = INDEX["cases"][case]["fixture_id"]
    return RawStore(tmp_path / "raw").save(
        "api-football",
        "statistics",
        json.dumps(document).encode(),
        "json",
        captured_at=instant("2026-11-18T19:00:00Z"),
        url=f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}&half=true",
        status_code=200,
    )


def set_stat(document, group, label, value, team=0):
    for stat in document["response"][team][group]:
        if stat["type"] == label:
            stat["value"] = value
            return
    raise AssertionError(label)


def test_samples_are_the_pinned_real_originals():
    assert INDEX["synthetic"] is False
    for case, entry in INDEX["cases"].items():
        for name in ("half", "full"):
            digest = hashlib.sha256(raw(case, name)).hexdigest()
            assert digest == entry[f"{name}_sha256"]
            sidecar = json.loads((FIX / case / f"{name}.json.meta.json").read_bytes())
            assert sidecar["sha256"] == digest and sidecar["status_code"] == 200
            assert ("half=true" in sidecar["url"]) is (name == "half")


@pytest.mark.parametrize("case", ["epl2026", "fin2026", "uel2026"])
def test_real_halves_parse_and_counts_add_up_to_full_time(case):
    rows = parse_half_statistics(raw(case), fixture=fixture(case), captured_utc=CAPTURED)
    # 18 statistics per side, plus Free Kicks where the provider sends it.
    assert len(rows) == len(by_key(rows)) == {"epl2026": 72, "fin2026": 76, "uel2026": 76}[case]
    assert {row["endpoint"] for row in rows} == {"statistics_half"}
    assert {row["period_basis"] for row in rows} == {"1h", "2h"}
    assert not any(row["eligible_90"] or row["extra_time_included"] for row in rows)
    assert {row["parser_version"] for row in rows} == {"t75.1"}
    half = by_key(rows)
    full = by_key(
        parse_endpoint(
            raw(case, "full"), endpoint="statistics", fixture=fixture(case), captured_utc=CAPTURED
        )
    )
    checked = 0
    for key, whole in full.items():
        first, second = half[f"1h:{key}"], half[f"2h:{key}"]
        assert (first["stat_key"], first["unit"], first["side"]) == (
            whole["stat_key"],
            whole["unit"],
            whole["side"],
        )
        values = (first["value"], second["value"], whole["value"])
        if whole["unit"] == "count" and None not in values:
            assert values[0] + values[1] == values[2]
            checked += 1
    assert len(rows) == 2 * len(full)
    assert checked >= 24


def test_full_group_of_the_half_response_equals_the_plain_response():
    for case in ("epl2026", "fin2026", "uel2026"):
        plain = json.loads(raw(case, "full"))["response"]
        with_half = json.loads(raw(case))["response"]
        assert [row["statistics"] for row in with_half] == [row["statistics"] for row in plain]


def test_real_premier_league_nulls_stay_null_and_xg_is_split():
    rows = by_key(
        parse_half_statistics(raw("epl2026"), fixture=fixture("epl2026"), captured_utc=CAPTURED)
    )
    for half in ("1h", "2h"):
        for side in ("home", "away"):
            fouls = rows[f"{half}:{side}:fouls"]
            assert fouls["value"] is None and fouls["raw_value"] is None
            assert fouls["missing_reason"] == "null"
            assert rows[f"{half}:{side}:goals_prevented"]["value"] is None
    assert (rows["1h:home:expected_goals"]["value"], rows["2h:home:expected_goals"]["value"]) == (
        1.32,
        0.56,
    )
    assert (rows["1h:home:corners"]["raw_value"], rows["1h:home:possession"]["value"]) == (
        json.loads(raw("epl2026"))["response"][0]["statistics_1h"][7]["value"],
        66.0,
    )


def test_real_2023_response_without_half_groups_yields_no_rows(database):
    assert (
        parse_half_statistics(raw("epl2023"), fixture=fixture("epl2023"), captured_utc=CAPTURED)
        == []
    )
    result = ingest_apif(database, FIX / "epl2023/half.json", endpoint="statistics_half")
    assert result == dict(inserted=0, rejected=0, empty=True)
    assert counts(database) == (0, 0)
    receipts = read_audit(database.parent / "state/audit/stats_ingest_audit.csv")
    assert [(r["endpoint"], r["outcome"], r["reason"]) for r in receipts] == [
        ("statistics_half", "ingested", "无半场数据")
    ]
    before = database.read_bytes()
    assert ingest_apif(database, FIX / "epl2023/half.json", endpoint="statistics_half") == dict(
        inserted=0, rejected=0, duplicate=True
    )
    assert database.read_bytes() == before


def test_ingest_is_separate_from_full_time_rows_and_repeatable(database):
    half = FIX / "epl2026/half.json"
    assert ingest_apif(database, half, endpoint="statistics_half")["inserted"] == 72
    assert counts(database) == (72, 0)
    before = database.read_bytes()
    audit = database.parent / "state/audit/stats_ingest_audit.csv"
    audit_before = audit.read_bytes()
    assert ingest_apif(database, half, endpoint="statistics_half")["inserted"] == 0
    assert database.read_bytes() == before and audit.read_bytes() == audit_before
    # The existing full-time path is untouched and never reads the half groups.
    assert ingest_apif(database, FIX / "epl2026/full.json", endpoint="statistics")["inserted"] == 36
    assert counts(database) == (72, 36)
    engine = get_engine(database)
    with session_scope(engine) as session:
        rows = list(session.scalars(select(MatchHalfStat)))
        assert {(r.availability_basis, r.available_utc, r.version) for r in rows} == {
            ("backfilled", None, 1)
        }
        assert {r.raw_sha256 for r in rows} == {INDEX["cases"]["epl2026"]["half_sha256"]}
        assert (
            session.scalar(
                select(func.count()).select_from(MatchStat).where(MatchStat.period_basis != "90")
            )
            == 0
        )
    engine.dispose()


def test_export_refuses_a_database_holding_half_rows_until_step_b(database, tmp_path):
    from zucai.stats_archive import export_stats

    with sqlite3.connect(database) as db:
        export_stats(db, tmp_path / "empty")
    ingest_apif(database, FIX / "epl2026/half.json", endpoint="statistics_half")
    with sqlite3.connect(database) as db, pytest.raises(ValueError, match="半场统计行尚未纳入归档"):
        export_stats(db, tmp_path / "archive")


def test_count_that_does_not_add_up_refuses_both_halves(database, tmp_path):
    path = tampered(
        tmp_path, "fin2026", lambda doc: set_stat(doc, "statistics_1h", "Corner Kicks", 99)
    )
    with pytest.raises(ValueError, match="corners 上下半场相加不等于全场"):
        parse_half_statistics(path.read_bytes(), fixture=fixture("fin2026"), captured_utc=CAPTURED)
    assert ingest_apif(database, path, endpoint="statistics_half") == dict(inserted=0, rejected=1)
    assert counts(database) == (0, 0)
    receipt = read_audit(database.parent / "state/audit/stats_ingest_audit.csv")[-1]
    assert (receipt["outcome"], receipt["reason"]) == ("rejected", "响应无效")
    assert "上下半场相加不等于全场" in receipt["detail"]
    # The full-time original of the same match still ingests normally.
    assert ingest_apif(database, FIX / "fin2026/full.json", endpoint="statistics")["inserted"] == 38
    assert counts(database) == (0, 38)


@pytest.mark.parametrize(("value", "accepted"), [("0.87", True), ("1.40", False)])
def test_expected_goals_tolerance(tmp_path, value, accepted):
    path = tampered(
        tmp_path, "fin2026", lambda doc: set_stat(doc, "statistics_1h", "expected_goals", value)
    )
    if accepted:
        rows = parse_half_statistics(
            path.read_bytes(), fixture=fixture("fin2026"), captured_utc=CAPTURED
        )
        assert by_key(rows)["1h:home:expected_goals"]["value"] == 0.87
    else:
        with pytest.raises(ValueError, match="expected_goals 上下半场相加不等于全场"):
            parse_half_statistics(
                path.read_bytes(), fixture=fixture("fin2026"), captured_utc=CAPTURED
            )


def test_partial_halves_and_non_ft_matches_are_refused(tmp_path):
    path = tampered(tmp_path, "fin2026", lambda doc: doc["response"][1].pop("statistics_2h"))
    with pytest.raises(ValueError, match="部分球队或部分时段"):
        parse_half_statistics(path.read_bytes(), fixture=fixture("fin2026"), captured_utc=CAPTURED)
    extra_time = fixture("fin2026")
    extra_time["fixture"]["status"]["short"] = "AET"
    with pytest.raises(ValueError, match="非 90 分钟完赛"):
        parse_half_statistics(raw("fin2026"), fixture=extra_time, captured_utc=CAPTURED)
    with pytest.raises(ValueError, match="fixture / league / season"):
        parse_half_statistics(
            raw("fin2026", "full"), fixture=fixture("fin2026"), captured_utc=CAPTURED
        )


def test_table_refuses_full_time_periods_and_migrates_into_old_databases(database):
    engine = get_engine(database)
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            MatchHalfStat(
                provider="apif",
                provider_fixture_id="1557367",
                endpoint="statistics_half",
                record_key="1h:home:shots",
                match_id="apif:1557367",
                team_id=1,
                provider_team_id=42,
                side="home",
                period_basis="90",
                extra_time_included=False,
                eligible_90=False,
                captured_utc=CAPTURED,
                available_utc=None,
                availability_basis="backfilled",
                raw_sha256="0" * 64,
                raw_location="test",
                parser_version="t75.1",
                missing_reason=None,
                version=1,
                stat_key="shots",
                raw_value=1,
                value=1.0,
                unit="count",
            )
        )
    engine.dispose()
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE match_half_stats")
    engine = get_engine(database)
    migrate_stats(engine)
    engine.dispose()
    assert counts(database) == (0, 0)
