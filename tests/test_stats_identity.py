"""Offline checks for the owner-approved fd historical identity merge."""

import csv
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import Mock

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
from zucai.stats_identity import AUDIT_NAME, reconcile_fd_aliases

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
    report = reconcile_fd_aliases(database, "first")
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
    again = reconcile_fd_aliases(database, "second")
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
    report = reconcile_fd_aliases(database, case)
    assert report["status"] == "partial" and report["skipped_conflicts"] == 1
    assert report["merged_teams"] == report["rekeyed_matches"] == 0
    assert database.read_bytes() == before
    audit = database.parent / "state/audit" / AUDIT_NAME
    prior = audit.read_bytes()
    assert list(csv.DictReader(audit.open()))[0]["reason"] != "merged"
    reconcile_fd_aliases(database, "retry")
    assert audit.read_bytes() == prior


@pytest.mark.parametrize("field", ["canonical", "approved_by", "approved_utc", "approval_pr"])
def test_exact_formal_approval_is_required(database, tmp_path, monkeypatch, field):
    from zucai import stats_identity

    entry = json.loads((ROOT / "data/team_aliases/football_data_co_uk.json").read_text())["荷甲"][
        "Den Haag"
    ]
    entry[field] = "" if field == "canonical" else "unapproved"
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"荷甲": {"Den Haag": entry}}))
    monkeypatch.setattr(stats_identity, "ALIASES_PATH", path)
    before = database.read_bytes()
    with pytest.raises(ValueError, match="批准|审批"):
        reconcile_fd_aliases(database, "unauthorized")
    assert database.read_bytes() == before
    assert not (database.parent / "state/audit" / AUDIT_NAME).exists()


@pytest.mark.parametrize("status", ["proposed", "auto_verified"])
def test_inferred_or_proposed_fd_alias_does_not_authorize_merge(
    database, tmp_path, monkeypatch, status
):
    from zucai import stats_identity

    aliases = json.loads((ROOT / "data/team_aliases/football_data_co_uk.json").read_text())
    entry = aliases["荷甲"]["Den Haag"]
    entry["status"] = status
    path = tmp_path / "unapproved.json"
    path.write_text(json.dumps({"荷甲": {"Den Haag": entry}}))
    monkeypatch.setattr(stats_identity, "ALIASES_PATH", path)
    before = database.read_bytes()
    assert reconcile_fd_aliases(database, "unapproved")["merged_teams"] == 0
    assert database.read_bytes() == before
    assert not (database.parent / "state/audit" / AUDIT_NAME).exists()


def test_all_ten_approved_pairs_merge_and_premier_league_index_has_no_identity_rejections(tmp_path):
    from zucai.stats_link import link_fixtures

    fixture = json.loads((ROOT / "data/fixtures/t32/followup_identity.json").read_text())
    approval = json.loads((ROOT / "data/fixtures/t32/followup_approvals.json").read_text())
    aliases = json.loads((ROOT / "data/team_aliases/api_football.json").read_text())
    aliases["英超"].update(approval["synthetic_opponents"])
    path = tmp_path / "feeds.db"
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in fixture["teams"])
        session.flush()
        session.add_all(Match(**row) for row in fixture["matches"])
    before = snapshot(path)
    # Reproduce the old third-batch canonical-name collision from the review.
    old_aliases = json.loads(json.dumps(aliases))
    for pid in ("48", "39"):
        old_aliases["英超"][pid]["canonical"] = old_aliases["英超"][pid]["name"]
    raw = json.dumps(fixture["pages"][0]).encode()
    metadata = dict(
        captured_utc="2026-11-04T15:00:00Z", raw_sha256="a" * 64, raw_location="fixture://followup"
    )
    with session_scope(engine) as session:
        old = link_fixtures(
            session, raw, league_id=39, season=2025, aliases=old_aliases, metadata=metadata
        )
        assert sum("多个既有身份" in row["detail"] for row in old["audit"]) == 2
    report = reconcile_fd_aliases(path, "ten-approved")
    assert report["merged_teams"] == report["deleted_teams"] == report["rekeyed_matches"] == 10
    assert report["skipped_conflicts"] == 0 and report["status"] == "complete"
    assert len(report["identity_redirects"]) == 10
    assert snapshot(path) == before
    with session_scope(engine) as session:
        result = link_fixtures(
            session, raw, league_id=39, season=2025, aliases=aliases, metadata=metadata
        )
        assert result["linked"] == 3 and result["rejected"] == 0
    prior = path.read_bytes(), (path.parent / "state/audit" / AUDIT_NAME).read_bytes()
    assert reconcile_fd_aliases(path, "repeat")["merged_teams"] == 0
    assert (path.read_bytes(), (path.parent / "state/audit" / AUDIT_NAME).read_bytes()) == prior
    engine.dispose()


def test_approved_fd_merge_republishes_old_technical_versions_without_overwriting_assets(
    database, tmp_path
):
    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.stats_rows import publish_rows, restore_rows

    # Reuse the attached-stat/event/lineup fixture for a newly approved pair.
    with sqlite3.connect(database) as db:
        db.execute("UPDATE teams SET name='West Ham' WHERE id=10")
        db.execute("UPDATE teams SET name='西汉姆联',aliases='[\"West Ham\"]' WHERE id=1")
        db.execute("UPDATE matches SET league='英超'")
        db.execute("UPDATE apif_fixture_index SET league='英超',provider_league_id=39")
        db.execute("UPDATE apif_fixture_index SET home_provider_id=48 WHERE home_provider_id=198")
        db.execute("UPDATE apif_fixture_index SET away_provider_id=48 WHERE away_provider_id=198")
        db.execute("UPDATE match_events SET provider_team_id=48")
        db.execute("UPDATE match_lineups SET provider_team_id=48")
        db.execute(
            "INSERT INTO results(match_id,home_goals,away_goals) "
            "SELECT 'jc:den-existing',home_goals,away_goals "
            "FROM results WHERE match_id='fd:den-home'"
        )
    stored = {}
    release = Mock()

    def upload(tag, path):
        data = path.read_bytes()
        key = tag, path.name
        assert key not in stored or stored[key] == data
        stored[key] = data
        return dict(
            tag=tag, name=path.name, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )

    def download(tag, name, dest):
        dest.write_bytes(stored[tag, name])
        return dest

    release.upload.side_effect = upload
    release.download.side_effect = download
    args = dict(release=release, tag="stats-raw-202611")
    before = publish_rows(database, tmp_path / "old", run_id="before", **args)
    frozen_assets = stored.copy()
    frozen_rows = snapshot(database)
    merged = reconcile_fd_aliases(database, "approved")
    assert merged["merged_teams"] == 1 and merged["rekeyed_stats"] == 3
    assert merged["rekeyed_indices"] == 2 and snapshot(database) == frozen_rows
    before["identity_redirects"] = merged["identity_redirects"]
    current = publish_rows(database, tmp_path / "new", previous=before, run_id="after", **args)
    assert all(stored[key] == data for key, data in frozen_assets.items())
    assert current["snapshot"]["counts"] == before["snapshot"]["counts"]
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 4), "after")
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    assert sum(restore_rows(restored, archive, release=release).values()) == 3
    assert snapshot(restored) == frozen_rows
    with sqlite3.connect(restored) as db:
        for table in ("match_stats", "match_events", "match_lineups"):
            assert db.execute(f"SELECT DISTINCT team_id FROM {table}").fetchall() == [(1,)]


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
            reconcile_fd_aliases(database, "failure")
    finally:
        if failure == "commit":
            event.remove(Session, "before_commit", fail_commit)
    assert database.read_bytes() == before
    assert (audit.read_bytes() if audit.exists() else None) == previous
