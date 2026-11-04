"""Offline ingestion is append-only, repeatable and atomic with its audits."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.cloud_feeds import export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, MatchStat, Team
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_storage import ingest_apif, ingest_fd, link_archive

FIX = Path(__file__).parents[1] / "data/fixtures"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "feeds.db"
    engine = get_engine(path)
    init_db(engine)
    data = json.loads((FIX / "t31/store.json").read_text())
    with session_scope(engine) as session:
        session.add_all(Team(**t) for t in data["teams"])
        session.flush()
        session.add_all(Match(**m) for m in data["matches"])
    engine.dispose()
    return path


def save(tmp_path, endpoint, data=None):
    store = RawStore(tmp_path / "raw")
    if endpoint == "fixtures":
        data = data or dict(
            get="fixtures",
            parameters={"league": "2", "season": "2024"},
            errors=[],
            results=1,
            paging={"current": 1, "total": 1},
            response=[json.loads((FIX / "t31/fixture.json").read_text())],
        )
        url = "https://v3.football.api-sports.io/fixtures?league=2&season=2024"
    else:
        data = data or json.loads((FIX / f"t30/{endpoint}.json").read_text())
        url = f"https://v3.football.api-sports.io/fixtures/{endpoint}?fixture=102"
    return store.save(
        "api-football",
        endpoint,
        json.dumps(data).encode(),
        "json",
        captured_at=instant("2026-11-04T12:00:00Z"),
        url=url,
        status_code=200,
    )


def setup_link(database, tmp_path):
    fixture = save(tmp_path, "fixtures")
    result = link_archive(
        database, fixture, league_id=2, season=2024, aliases_path=FIX / "t31/aliases.json"
    )
    assert result["linked"] == 1
    return fixture


def stats(database):
    engine = get_engine(database)
    with session_scope(engine) as session:
        rows = [
            (r.record_key, r.value, r.raw_value, r.version, r.available_utc)
            for r in session.scalars(select(MatchStat).order_by(MatchStat.id))
        ]
    engine.dispose()
    return rows


def test_duplicate_null_correction_and_old_replay(database, tmp_path):
    fixture = setup_link(database, tmp_path)
    raw = save(tmp_path, "statistics")
    assert ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")["inserted"] == 36
    before = database.read_bytes()
    audit = database.parent / "state/audit/stats_ingest_audit.csv"
    audit_before = audit.read_bytes()
    assert ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")["inserted"] == 0
    assert database.read_bytes() == before and audit.read_bytes() == audit_before
    original = json.loads((FIX / "t30/statistics.json").read_text())
    original["response"][0]["statistics"][0]["value"] = None
    null = save(tmp_path, "statistics", original)
    assert ingest_apif(database, null, fixture_file=fixture, endpoint="statistics")["inserted"] == 0
    original["response"][0]["statistics"][0]["value"] = 5
    correction = save(tmp_path, "statistics", original)
    assert (
        ingest_apif(database, correction, fixture_file=fixture, endpoint="statistics")["inserted"]
        == 1
    )
    assert [(r[1], r[3]) for r in stats(database) if r[0] == "home:shots_on_target"] == [
        (4, 1),
        (5, 2),
    ]
    before = database.read_bytes()
    ingest_apif(database, null, fixture_file=fixture, endpoint="statistics")
    ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")
    assert database.read_bytes() == before
    assert all(row[4] is None for row in stats(database))


@pytest.mark.parametrize("endpoint", ["events", "lineups", "players"])
def test_all_endpoint_tables_export_restore(database, tmp_path, endpoint):
    fixture = setup_link(database, tmp_path)
    raw = save(tmp_path, endpoint)
    assert ingest_apif(database, raw, fixture_file=fixture, endpoint=endpoint)["inserted"] > 0
    archive = tmp_path / "archive"
    meta = export_feeds(database, archive, instant("2026-11-04T12:00:00Z").date(), "offline")
    restored = tmp_path / "restored.db"
    restore_feeds(archive, restored)
    second = export_feeds(
        restored, tmp_path / "again", instant("2026-11-04T12:00:00Z").date(), "offline"
    )
    assert meta["technical_data"] == second["technical_data"]
    for key in meta["files"]:
        if key.startswith("state/"):
            assert meta["files"][key] == second["files"][key]


def test_failure_rolls_back_rows_and_audit(database, tmp_path, monkeypatch):
    fixture = setup_link(database, tmp_path)
    raw = save(tmp_path, "statistics")
    from zucai import stats_storage

    real = stats_storage.write_audit

    def fail_after_write(*args, **kwargs):
        real(*args, **kwargs)
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(stats_storage, "write_audit", fail_after_write)
    with pytest.raises(OSError, match="synthetic"):
        ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")
    assert stats(database) == []
    assert not (database.parent / "state/audit/stats_ingest_audit.csv").exists()


def test_error_response_audited_but_no_observations(database, tmp_path):
    fixture = setup_link(database, tmp_path)
    data = json.loads((FIX / "t30/statistics.json").read_text())
    data["errors"] = {"access": "denied"}
    raw = save(tmp_path, "statistics", data)
    result = ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")
    assert result["inserted"] == 0 and result["rejected"] == 1
    assert stats(database) == []
    assert "errors" in (database.parent / "state/audit/stats_ingest_audit.csv").read_text()


def test_original_required_and_sidecar_hash_verified(database, tmp_path):
    fixture = setup_link(database, tmp_path)
    raw = save(tmp_path, "statistics")
    raw.write_bytes(b"{}")
    with pytest.raises(ValueError, match="哈希"):
        ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")
    raw.unlink()
    with pytest.raises(FileNotFoundError):
        ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")
    assert stats(database) == []


def test_fd_existing_matches_only(database, tmp_path):
    raw = RawStore(tmp_path / "raw").save(
        "football-data-co-uk",
        "E0-2425",
        (FIX / "t31/fd.csv").read_bytes(),
        "csv",
        captured_at=instant("2026-11-04T12:00:00Z"),
        url="https://www.football-data.co.uk/mmz4281/2425/E0.csv",
        status_code=200,
    )
    result = ingest_fd(
        database, raw, div="E0", season="2425", leagues_path=FIX / "t31/fd_leagues.json"
    )
    assert result["inserted"] == 16
    assert all(r[4] is None for r in stats(database))
    assert (
        ingest_fd(database, raw, div="E0", season="2425", leagues_path=FIX / "t31/fd_leagues.json")[
            "inserted"
        ]
        == 0
    )
    engine = get_engine(database)
    with session_scope(engine) as session:
        assert len(list(session.scalars(select(Match)))) == 2
        assert len(list(session.scalars(select(Team)))) == 2
        assert list(session.scalars(select(ApifFixtureIndex))) == []
    engine.dispose()


def test_empty_response_receipt_and_no_change_snapshot_are_idempotent(database, tmp_path):
    fixture = setup_link(database, tmp_path)
    data = json.loads((FIX / "t30/events.json").read_text())
    data.update(results=0, response=[])
    empty = save(tmp_path, "events", data)
    assert ingest_apif(database, empty, fixture_file=fixture, endpoint="events")["empty"]
    audit = database.parent / "state/audit/stats_ingest_audit.csv"
    before = audit.read_bytes()
    ingest_apif(database, empty, fixture_file=fixture, endpoint="events")
    assert audit.read_bytes() == before
    # An unchanged response with a different raw hash still needs a persistent receipt.
    original = save(tmp_path, "statistics")
    ingest_apif(database, original, fixture_file=fixture, endpoint="statistics")
    data = json.loads((FIX / "t30/statistics.json").read_text())
    data["response"][0]["team"]["name"] += " spelling"
    unchanged = save(tmp_path, "statistics", data)
    assert (
        ingest_apif(database, unchanged, fixture_file=fixture, endpoint="statistics")["inserted"]
        == 0
    )
    data["response"][0]["statistics"][0]["value"] = 6
    correction = save(tmp_path, "statistics", data)
    ingest_apif(database, correction, fixture_file=fixture, endpoint="statistics")
    before = stats(database)
    ingest_apif(database, unchanged, fixture_file=fixture, endpoint="statistics")
    assert stats(database) == before


def test_commit_failure_restores_existing_audit(database, tmp_path, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    fixture = setup_link(database, tmp_path)
    raw = save(tmp_path, "statistics")
    ingest_apif(database, raw, fixture_file=fixture, endpoint="statistics")
    audit = database.parent / "state/audit/stats_ingest_audit.csv"
    before = audit.read_bytes()
    values = stats(database)
    data = json.loads((FIX / "t30/statistics.json").read_text())
    data["response"][0]["statistics"][0]["value"] = 9
    corrected = save(tmp_path, "statistics", data)

    def fail_commit(session):
        raise ValueError("synthetic commit failure")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(ValueError, match="commit failure"):
            ingest_apif(database, corrected, fixture_file=fixture, endpoint="statistics")
    finally:
        event.remove(Session, "before_commit", fail_commit)
    assert audit.read_bytes() == before
    assert stats(database) == values
