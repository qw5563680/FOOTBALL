"""Real FD row, synthetic official seed: an existing proof survives later result import."""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from test_fd_stats_archive import LocalRelease
from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.fd_stats_links import FD_LINK_COLUMNS, validate_fd_links
from zucai.models import Match, Result, Team
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.sources.fd_ingest import fetch_fd
from zucai.stats_archive import check_stats
from zucai.stats_rows import publish_rows, restore_rows
from zucai.stats_storage import ingest_fd

ROOT = Path(__file__).parents[1]
RAW = ROOT / "data/fixtures/t66/fd-n1-zwolle-den-haag.csv"
FD_ID = "fd:N1:20261101:zwolle:den-haag"
JC_ID = "jc:2042418"


def rows(database, table):
    with sqlite3.connect(database) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def mutate(database, statement, parameters=()):
    with sqlite3.connect(database) as db:
        db.execute(statement, parameters)


def seed(tmp_path, pointer=None):
    database = tmp_path / "source/feeds.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(
            [
                Team(id=1, name="兹沃勒", aliases=[]),
                Team(id=2, name="海牙", aliases=[]),
                Team(id=3, name="第三队", aliases=[]),
                Team(id=4, name="第四队", aliases=[]),
            ]
        )
        session.flush()
        session.add(
            Match(
                match_id=JC_ID,
                source="jc",
                league="荷甲",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc="2026-11-01T11:15:00Z",
                counterpart_id=pointer,
                status="finished",
            )
        )
        session.flush()
        session.add(
            Result(
                match_id=JC_ID,
                home_goals=1,
                away_goals=0,
                ht_home_goals=1,
                ht_away_goals=0,
                available_utc="2026-11-01T15:00:00Z",
                available_source="captured",
            )
        )
    engine.dispose()
    result = ingest_fd(
        database,
        RAW,
        div="N1",
        season="2627",
        leagues_path=ROOT / "data/football_data_co_uk/leagues.json",
        aliases_path=ROOT / "data/team_aliases/football_data_co_uk.json",
    )
    assert result["inserted"] > 0 and result["rejected"] == 0
    with sqlite3.connect(database) as db:
        assert list(validate_fd_links(db)) == [FD_ID]
    return database


def import_result(database):
    return fetch_fd(
        div="N1",
        seasons=["2627"],
        assume=timedelta(hours=3),
        db=database,
        raw_file=RAW,
        raw_root=database.parent / "raw",
        audit_dir=database.parent / "state/audit",
        leagues_path=ROOT / "data/football_data_co_uk/leagues.json",
        aliases_path=ROOT / "data/team_aliases/football_data_co_uk.json",
    )


@pytest.mark.parametrize("original", [None, "fdorg:77"])
def test_existing_statistics_then_fd_result_roundtrips_without_rewriting_proof(tmp_path, original):
    database = seed(tmp_path, original)
    before = {name: rows(database, name) for name in ("fd_stats_links", "match_stats", "results")}
    for _ in range(2):
        imported = import_result(database)
        assert imported["linked_existing"] == 1
        assert imported["inserted"] == imported["rejected_rows"] == 0
        assert imported["suspected_duplicates"] == 0
        with sqlite3.connect(database) as db:
            assert list(validate_fd_links(db)) == [FD_ID]
            assert db.execute("SELECT counterpart_id FROM matches").fetchone() == (
                original or FD_ID,
            )
        assert {name: rows(database, name) for name in before} == before

    release = LocalRelease(tmp_path / "release")
    publish_rows(
        database,
        tmp_path / "assets",
        release=release,
        tag="stats-raw-202611",
        run_id="t66",
    )
    archive = tmp_path / "archive"
    exported = export_feeds(database, archive, date(2026, 11, 14), "t66")
    assert exported["fd_stats_links"]["count"] == 1
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    restore_rows(restored, archive, release=release)
    assert {name: rows(restored, name) for name in before} == before
    assert rows(restored, "matches") == rows(database, "matches")
    assert rows(restored, "closing_odds") == rows(database, "closing_odds")


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        (None, "fd:N1:different"),
        (None, "apif:77"),
        (None, "fdorg:77"),
        ("fdorg:77", FD_ID),
        ("fdorg:77", "fdorg:88"),
        ("fdorg:77", None),
    ],
)
def test_only_an_empty_original_pointer_may_gain_its_exact_fd_id(tmp_path, original, changed):
    database = seed(tmp_path, original)
    mutate(database, "UPDATE matches SET counterpart_id=?", (changed,))
    with sqlite3.connect(database) as db, pytest.raises(ValueError):
        validate_fd_links(db)


@pytest.mark.parametrize(
    ("kind", "home", "away"), [("reverse", 2, 1), ("shared", 1, 3), ("ambiguous", 1, 2)]
)
def test_exact_fd_pointer_does_not_bypass_unique_team_and_direction_checks(
    tmp_path, kind, home, away
):
    database = seed(tmp_path)
    mutate(database, "UPDATE matches SET counterpart_id=?", (FD_ID,))
    mutate(
        database,
        "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,kickoff_utc,status) "
        "VALUES('jc:2','jc','荷甲',?,?,'2026-11-01T11:15:00Z','scheduled')",
        (home, away),
    )
    with (
        sqlite3.connect(database) as db,
        pytest.raises(ValueError, match="反向、共享球队或候选不唯一"),
    ):
        validate_fd_links(db)


@pytest.mark.parametrize("claim", ["counterpart", "direct"])
def test_exact_fd_pointer_rejects_a_second_claim_even_outside_the_target_window(tmp_path, claim):
    database = seed(tmp_path)
    mutate(database, "UPDATE matches SET counterpart_id=?", (FD_ID,))
    mutate(
        database,
        "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
        "kickoff_utc,status,counterpart_id) VALUES(?,?,'荷甲',3,4,"
        "'2026-10-01T11:15:00Z','scheduled',?)",
        (
            FD_ID if claim == "direct" else "jc:2",
            "fd" if claim == "direct" else "jc",
            None if claim == "direct" else FD_ID,
        ),
    )
    with sqlite3.connect(database) as db, pytest.raises(ValueError):
        validate_fd_links(db)


def test_real_crop_retains_the_captured_clock_and_exact_row_provenance():
    import hashlib

    provenance = json.loads((RAW.parent / "provenance.json").read_text())
    metadata = json.loads(RAW.with_suffix(".csv.meta.json").read_text())
    assert provenance["raw_line"] == 97
    assert metadata["fetched_at_utc"] == "2026-11-04T14:07:19.980721Z"
    assert hashlib.sha256(RAW.read_bytes()).hexdigest() == provenance["fixture_sha256"]
    assert metadata["sha256"] == provenance["fixture_sha256"]


def test_later_stats_capture_still_appends_observation_after_fd_result_import(tmp_path):
    database = seed(tmp_path)
    before = rows(database, "fd_stats_links")[0]
    import_result(database)
    # A separately simulated capture; original real fixture remains byte-identical.
    raw = RawStore(tmp_path / "later").save(
        "football-data-co-uk",
        "N1-2627",
        RAW.read_bytes() + b"\n",
        "csv",
        captured_at=instant("2026-11-05T14:07:19Z"),
        url="https://football-data.co.uk/mmz4281/2627/N1.csv",
        status_code=200,
    )
    for _ in range(2):
        assert ingest_fd(
            database,
            raw,
            div="N1",
            season="2627",
            leagues_path=ROOT / "data/football_data_co_uk/leagues.json",
            aliases_path=ROOT / "data/team_aliases/football_data_co_uk.json",
        ) == {"inserted": 0, "rejected": 0}
    with sqlite3.connect(database) as db:
        checked = validate_fd_links(db)[FD_ID]
        observations = checked["evidence"]["observations"]
        assert len(observations) == 2
        assert (
            observations[0]
            == json.loads(before[FD_LINK_COLUMNS.index("evidence")])["observations"][0]
        )
        assert instant(observations[0]["captured_utc"]) == instant("2026-11-04T14:07:19.980721Z")
        assert instant(observations[1]["captured_utc"]) == instant("2026-11-05T14:07:19Z")
        assert checked["evidence"]["counterpart_id"] is None
        check_stats(db)
    after = rows(database, "fd_stats_links")[0]
    # Only the append-only observation list changes, never original identity/time/hash.
    evidence_column = FD_LINK_COLUMNS.index("evidence")
    assert before[:evidence_column] == after[:evidence_column]
    assert before[evidence_column + 1 :] == after[evidence_column + 1 :]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("raw_sha256", "b" * 64),
        ("raw_location", "forged://raw.csv"),
        ("captured_utc", "2026-11-05T12:00:00Z"),
    ],
)
def test_fd_result_pointer_does_not_bypass_existing_link_raw_provenance(tmp_path, column, value):
    database = seed(tmp_path)
    import_result(database)
    mutate(database, f"UPDATE match_stats SET {column}=?", (value,))
    with sqlite3.connect(database) as db, pytest.raises(ValueError, match="原件证据"):
        check_stats(db)
    with pytest.raises(ValueError, match="原件证据"):
        publish_rows(
            database,
            tmp_path / "assets",
            release=LocalRelease(tmp_path / "release"),
            tag="stats-raw-202611",
            run_id="t66-tampered",
        )
