"""Offline checks for the single approved Den Haag historical identity merge."""

import copy
import csv
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db, session_scope
from zucai.models import (
    ApifFixtureIndex,
    Match,
    MatchEvent,
    MatchLineup,
    MatchStat,
    Odds,
    Result,
    Team,
)
from zucai.sources.apif_reconcile import FIELDS
from zucai.stats_identity import AUDIT_NAME, reconcile_den_haag

ROOT = Path(__file__).parents[1]
FIX = ROOT / "data/fixtures/t32/identity_den_haag.json"


@pytest.fixture
def database(tmp_path, monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError("identity reconciliation must not make requests")

    monkeypatch.setattr("requests.sessions.Session.request", forbid_network)
    path = tmp_path / "feeds.db"
    engine = get_engine(path)
    init_db(engine)
    fixture = json.loads(FIX.read_text())
    assert fixture["synthetic"]
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in fixture["teams"])
        session.flush()
        session.add_all(Match(**row) for row in fixture["matches"])
        session.flush()
        for idx, match_id in enumerate(("fd:den-home", "fd:den-away"), 1):
            session.add(Result(match_id=match_id, home_goals=2, away_goals=1))
            session.add(
                Odds(
                    match_id=match_id,
                    pool="had",
                    selection="h",
                    price=2.5,
                    snapshot_utc="2024-07-01T10:00:00Z",
                )
            )
            match = session.get(Match, match_id)
            session.add(
                ApifFixtureIndex(
                    provider_fixture_id=str(100 + idx),
                    match_id=match_id,
                    league="荷甲",
                    provider_league_id=88,
                    season=2024,
                    kickoff_utc=match.kickoff_utc,
                    home_provider_id=198 if idx == 1 else 999,
                    away_provider_id=999 if idx == 1 else 198,
                    home_team_id=match.home_team_id,
                    away_team_id=match.away_team_id,
                    captured_utc="2026-11-04T12:00:00Z",
                    raw_sha256="a" * 64,
                    raw_location="fixture://den-haag",
                    parser_version="synthetic",
                    evidence={"synthetic": True},
                )
            )
        stat = json.loads((ROOT / "data/fixtures/t31/store.json").read_text())["stat"]
        stat.update(match_id="fd:den-home", provider_fixture_id="fd:den-home", team_id=10)
        session.add(MatchStat(**stat))
        common = {
            key: value
            for key, value in stat.items()
            if key in MatchEvent.__table__.columns and key not in {"raw_value", "value"}
        }
        common.update(
            provider="apif", provider_fixture_id="101", endpoint="events", record_key="event:1"
        )
        session.add(MatchEvent(**common, raw_value={"detail": "synthetic"}, value={"elapsed": 10}))
        common.update(
            match_id="fd:den-away",
            provider_fixture_id="102",
            endpoint="lineups",
            record_key="away:coach",
            side="away",
        )
        session.add(
            MatchLineup(
                **common,
                role="coach",
                captured_before_kickoff=False,
                raw_value={"name": "synthetic"},
                value={"name": "synthetic"},
            )
        )
    engine.dispose()
    return path


def snapshot(database):
    with sqlite3.connect(database) as connection:
        result = {}
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            frozen = [
                col for col in columns if col not in {"team_id", "home_team_id", "away_team_id"}
            ]
            if table == "teams":
                continue
            names = ",".join(f'"{col}"' for col in frozen)
            result[table] = connection.execute(
                f'SELECT {names} FROM "{table}" ORDER BY 1'
            ).fetchall()
        return result


def test_narrow_merge_preserves_evidence_counts_and_updates_all_team_refs(database):
    before = snapshot(database)
    report = reconcile_den_haag(database, "first")
    assert report["status"] == "complete" and report["requests"] == 0
    assert report["merged_teams"] == report["deleted_teams"] == 1
    assert report["rekeyed_matches"] == 2
    assert report["rekeyed_stats"] == 3 and report["rekeyed_indices"] == 2
    assert snapshot(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM teams ORDER BY id").fetchall() == [
            (1,),
            (2,),
            (3,),
        ]
        for table, columns in (
            ("matches", ("home_team_id", "away_team_id")),
            ("apif_fixture_index", ("home_team_id", "away_team_id")),
            ("match_stats", ("team_id",)),
            ("match_events", ("team_id",)),
            ("match_lineups", ("team_id",)),
        ):
            for column in columns:
                assert not connection.execute(
                    f'SELECT 1 FROM "{table}" WHERE "{column}"=10'
                ).fetchall()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        aliases = json.loads(
            connection.execute("SELECT aliases FROM teams WHERE id=1").fetchone()[0]
        )
        assert aliases == ["apif:荷甲:team:198", "Den Haag"]
    audit = database.parent / "state/audit" / AUDIT_NAME
    rows = list(csv.DictReader(audit.open()))
    assert len(rows) == 1 and rows[0]["reason"] == "merged"
    assert rows[0]["matches_rekeyed"] == "2"
    prior = audit.read_bytes(), database.read_bytes()
    again = reconcile_den_haag(database, "second")
    assert again["merged_teams"] == again["deleted_teams"] == again["rekeyed_matches"] == 0
    assert again["rekeyed_stats"] == again["rekeyed_indices"] == 0
    assert (audit.read_bytes(), database.read_bytes()) == prior


@pytest.mark.parametrize(
    "case",
    [
        "missing_target",
        "old_alias",
        "global_source_alias",
        "global_target_alias",
        "foreign_source",
        "foreign_league",
        "self_match",
        "window",
        "invalid_stat",
        "invalid_index",
        "orphan_stat",
    ],
)
def test_prechecks_reject_conflicts_before_any_identity_mutation(database, case):
    with sqlite3.connect(database) as connection:
        if case == "missing_target":
            connection.execute("UPDATE teams SET name='Other existing team' WHERE id=1")
        elif case == "old_alias":
            connection.execute("UPDATE teams SET aliases='[\"unexpected\"]' WHERE id=10")
        elif case.startswith("global_"):
            alias = "Den Haag" if case == "global_source_alias" else "海牙"
            connection.execute("UPDATE teams SET aliases=? WHERE id=2", (json.dumps([alias]),))
        elif case == "foreign_source":
            connection.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) "
                "VALUES('jc:bad','jc','荷甲',10,2,'2024-09-01T12:00:00Z','scheduled')"
            )
        elif case == "foreign_league":
            connection.execute("UPDATE matches SET league='英超' WHERE match_id='fd:den-home'")
        elif case == "self_match":
            connection.execute("UPDATE matches SET away_team_id=1 WHERE match_id='fd:den-home'")
        elif case == "window":
            connection.execute(
                "UPDATE matches SET kickoff_utc='2024-08-01T20:59:00Z' "
                "WHERE match_id='jc:den-existing'"
            )
        elif case == "invalid_stat":
            connection.execute("UPDATE match_stats SET team_id=3")
        elif case == "invalid_index":
            connection.execute(
                "UPDATE apif_fixture_index SET home_team_id=1 WHERE provider_fixture_id='101'"
            )
        else:
            connection.execute("UPDATE match_stats SET match_id='jc:den-existing'")
    before = database.read_bytes()
    report = reconcile_den_haag(database, case)
    assert report["status"] == "partial" and report["skipped_conflicts"] == 1
    assert report["merged_teams"] == report["rekeyed_matches"] == 0
    assert database.read_bytes() == before
    audit = database.parent / "state/audit" / AUDIT_NAME
    prior = audit.read_bytes()
    assert list(csv.DictReader(audit.open()))[0]["reason"] != "merged"
    reconcile_den_haag(database, "retry")
    assert audit.read_bytes() == prior


@pytest.mark.parametrize(
    "field", ["canonical", "status", "approved_by", "approved_utc", "approval_pr"]
)
def test_exact_formal_approval_is_required(database, tmp_path, monkeypatch, field):
    from zucai import stats_identity

    entry = copy.deepcopy(stats_identity.APPROVAL)
    entry[field] = "unapproved"
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"荷甲": {"Den Haag": entry}}))
    monkeypatch.setattr(stats_identity, "ALIASES_PATH", path)
    before = database.read_bytes()
    with pytest.raises(ValueError, match="正式批准记录"):
        reconcile_den_haag(database, "unauthorized")
    assert database.read_bytes() == before
    assert not (database.parent / "state/audit" / AUDIT_NAME).exists()


@pytest.mark.parametrize("existing_audit", [False, True])
@pytest.mark.parametrize("failure", ["audit_write", "commit"])
def test_failure_rolls_back_database_and_restores_audit(
    database, monkeypatch, existing_audit, failure
):
    from zucai import stats_identity

    audit = database.parent / "state/audit" / AUDIT_NAME
    previous = (",".join(FIELDS) + "\r\n").encode() if existing_audit else None
    if previous is not None:
        audit.parent.mkdir(parents=True)
        audit.write_bytes(previous)
    before = database.read_bytes()
    original = stats_identity.write_audit

    def fail_write(*args):
        original(*args)
        raise OSError("synthetic audit write failure")

    def fail_commit(session):
        raise OSError("synthetic commit failure")

    if failure == "audit_write":
        monkeypatch.setattr(stats_identity, "write_audit", fail_write)
    else:
        event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(OSError, match="synthetic"):
            reconcile_den_haag(database, "failure")
    finally:
        if failure == "commit":
            event.remove(Session, "before_commit", fail_commit)
    assert database.read_bytes() == before
    assert (audit.read_bytes() if audit.exists() else None) == previous
