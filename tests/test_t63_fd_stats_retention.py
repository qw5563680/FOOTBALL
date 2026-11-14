"""Retained API results cannot invalidate an unchanged, proven FD-to-JC attachment."""

import sqlite3
import sys

import pytest

from test_fd_stats_archive import publish
from test_fd_stats_links import CAPTURE, FD_ID, ingest, mutate, sql
from test_fd_stats_links import setup as setup
from zucai.cloud_feeds import check_db, main
from zucai.db import get_engine, session_scope
from zucai.fd_stats_links import validate_fd_links
from zucai.models import Match, Result
from zucai.result_source_links import record_result_link
from zucai.sources.apif_stats import instant
from zucai.stats_rows import restore_rows


def retained(setup, *, pointer=None, proof=True):
    database = setup[0]
    mutate(database, "UPDATE matches SET counterpart_id=?", (pointer,))
    assert ingest(setup) == {"inserted": 16, "rejected": 0}
    original = sql(database, "SELECT * FROM fd_stats_links")
    engine = get_engine(database)
    with session_scope(engine) as session:
        jc = session.get(Match, "jc:1")
        api = Match(
            match_id="apif:1",
            source="apif",
            league=jc.league,
            home_team_id=jc.home_team_id,
            away_team_id=jc.away_team_id,
            kickoff_utc=jc.kickoff_utc,
            status="finished",
        )
        session.add(api)
        session.flush()
        for match in (jc, api):
            session.add(
                Result(
                    match_id=match.match_id,
                    home_goals=1,
                    away_goals=0,
                    available_utc=CAPTURE,
                    available_source="captured",
                )
            )
        if proof:
            record_result_link(session, api, jc, instant(CAPTURE))
    engine.dispose()
    return original


@pytest.mark.parametrize("pointer", [None, "fdorg:old", "same-fd"])
def test_valid_retained_api_proof_preserves_fd_target_replay_and_export(
    setup, tmp_path, monkeypatch, pointer
):
    original = retained(setup, pointer=None if pointer == "same-fd" else pointer)
    database = setup[0]
    if pointer == "same-fd":
        mutate(database, "UPDATE matches SET counterpart_id=? WHERE match_id='jc:1'", (FD_ID,))
    before = {
        table: sql(database, f"SELECT * FROM {table} ORDER BY 1")
        for table in ("matches", "results", "match_stats")
    }
    with sqlite3.connect(database) as db:
        assert validate_fd_links(db)[FD_ID]["match_id"] == "jc:1"
        check_db(db)
    assert ingest(setup) == {"inserted": 0, "rejected": 0}
    archive, _, release = publish(tmp_path, database)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "export",
            "--db",
            str(database),
            "--archive",
            str(archive),
            "--run-id",
            "retained",
        ],
    )
    main()
    work = tmp_path / "restored"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        sys, "argv", ["feeds", "verify", "--archive", str(archive), "--run-id", "verify-retained"]
    )
    main()
    restored = work / "data/zucai.db"
    restore_rows(restored, archive, release=release)
    assert sql(database, "SELECT * FROM fd_stats_links") == original
    assert sql(restored, "SELECT * FROM fd_stats_links") == original
    assert {table: sql(database, f"SELECT * FROM {table} ORDER BY 1") for table in before} == before
    assert {table: sql(restored, f"SELECT * FROM {table} ORDER BY 1") for table in before} == before


@pytest.mark.parametrize(
    "damage", ["missing", "stale", "reverse", "shared", "competitor", "pointer", "invalid-proof"]
)
def test_unproven_or_competing_retained_rows_still_reject(setup, damage):
    retained(setup, proof=damage != "missing")
    database = setup[0]
    if damage == "stale":
        mutate(
            database,
            "UPDATE matches SET kickoff_utc='2024-10-01T19:01:00.000000Z' WHERE match_id='apif:1'",
        )
    elif damage == "reverse":
        mutate(database, "UPDATE matches SET home_team_id=2,away_team_id=1 WHERE match_id='apif:1'")
    elif damage == "shared":
        mutate(database, "UPDATE matches SET away_team_id=3 WHERE match_id='apif:1'")
    elif damage == "competitor":
        mutate(
            database,
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('apif:2','apif','英超',1,2,"
            "'2024-10-01T19:00:00.000000Z','scheduled')",
        )
    elif damage == "pointer":
        mutate(database, "UPDATE matches SET counterpart_id='apif:1' WHERE match_id='jc:1'")
    elif damage == "invalid-proof":
        mutate(database, "UPDATE result_source_links SET verified_utc='not-a-time'")
    before = sql(database, "SELECT * FROM fd_stats_links")
    with sqlite3.connect(database) as db, pytest.raises(ValueError):
        validate_fd_links(db)
    assert sql(database, "SELECT * FROM fd_stats_links") == before
