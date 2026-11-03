import csv
import json
import sqlite3
from pathlib import Path

import pytest
import responses

from zucai.db import get_engine, init_db

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t23/reconcile.json").read_bytes())


@pytest.fixture
def seed(tmp_path, monkeypatch):
    db = tmp_path / "seed/data/zucai.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO teams VALUES(?,?,?)",
            [(i, n, json.dumps(a)) for i, n, a in FIXTURE["teams"]],
        )
        con.executemany(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) "
            "VALUES(?,?,?,?,?,?,'scheduled')",
            FIXTURE["matches"],
        )
        con.execute("UPDATE matches SET status='finished' WHERE match_id='apif:90001'")
        con.execute(
            "INSERT INTO results VALUES('apif:90001',2,1,1,0,'2024-08-21T00:00:00Z','assumed')"
        )
        con.execute(
            "INSERT INTO odds VALUES(1,'apif:90001','had',0,'h',2.5,'2024-08-19T00:00:00Z')"
        )
        con.execute(
            "INSERT INTO closing_odds "
            "VALUES('apif:90001','PS','had',2.0,3.0,4.0,'publisher_closing')"
        )
        con.execute("INSERT INTO sfc_issues(issue_no) VALUES('fixture')")
        con.execute("INSERT INTO sfc_issue_matches VALUES('fixture',1,'apif:90001')")
        con.execute(
            "INSERT INTO predictions(id,match_id,model_name,model_version,p_home,p_draw,p_away,"
            "score_matrix,created_utc) "
            "VALUES(1,'apif:90001','fixture','1',0.5,0.2,0.3,'[[1.0]]','2024-08-19T00:00:00Z')"
        )
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps(FIXTURE["aliases"]))
    monkeypatch.setattr("zucai.sources.apif_reconcile.ALIASES", path)
    return db, path


def frozen(db):
    with sqlite3.connect(db) as con:
        tables = [
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r[0] not in {"teams", "matches"}
        ]
        result = {t: con.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
        result["matches"] = con.execute(
            "SELECT match_id,source,league,kickoff_utc,status,jc_num,result_provider,"
            "result_checked_utc,counterpart_id FROM matches ORDER BY match_id"
        ).fetchall()
    return result


def test_reconcile_across_leagues_keeps_match_evidence_and_is_idempotent(seed):
    from zucai.sources.apif_reconcile import reconcile

    db, aliases = seed
    before, formal = frozen(db), aliases.read_bytes()
    with responses.RequestsMock() as http:
        report = reconcile(db, "first")
        assert not http.calls
    assert report["merged_teams"] == report["deleted_teams"] == 2
    assert report["rekeyed_matches"] == 3 and report["skipped_conflicts"] == 0
    assert frozen(db) == before and aliases.read_bytes() == formal
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT id FROM teams ORDER BY id").fetchall() == [
            (1,),
            (2,),
            (12,),
            (13,),
        ]
        assert con.execute(
            "SELECT home_team_id,away_team_id FROM matches WHERE match_id='apif:90001'"
        ).fetchone() == (1, 2)
        assert json.loads(con.execute("SELECT aliases FROM teams WHERE id=1").fetchone()[0]) == [
            "apif:欧协联:team:9101",
            "apif:欧罗巴:team:9101",
        ]
    audit = db.parent / "state/audit/apif_identity_merges.csv"
    raw = audit.read_bytes()
    assert len(list(csv.DictReader(audit.open()))) == 5
    again = reconcile(db, "second")
    assert again["merged_teams"] == again["rekeyed_matches"] == again["deleted_teams"] == 0
    assert audit.read_bytes() == raw and frozen(db) == before


@pytest.mark.parametrize(
    "problem", ["missing", "ambiguous", "binding", "provisional_alias", "window", "foreign_source"]
)
def test_preflight_conflicts_are_skipped_per_league_and_audited(seed, problem):
    from zucai.sources.apif_reconcile import reconcile

    db, _ = seed
    with sqlite3.connect(db) as con:
        if problem == "missing":
            con.execute("UPDATE teams SET name='其他' WHERE id=1")
        elif problem == "ambiguous":
            con.execute("UPDATE teams SET aliases='[\"合成甲\"]' WHERE id=12")
        elif problem == "binding":
            con.execute("UPDATE teams SET aliases='[\"apif:欧罗巴:team:9999\"]' WHERE id=1")
        elif problem == "provisional_alias":
            con.execute("UPDATE teams SET aliases='[\"unexpected\"]' WHERE id=10")
        elif problem == "foreign_source":
            con.execute(
                "UPDATE matches SET match_id='fd:90001',source='fd' WHERE match_id='apif:90001'"
            )
        else:
            con.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) "
                "VALUES('jc:conflict','jc','欧罗巴',1,2,'2024-08-20T21:00:00Z','scheduled')"
            )
    report = reconcile(db, problem)
    assert report["skipped_conflicts"] > 0
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id IN ('apif:90001','fd:90001')"
        ).fetchone() == (10,)
    audit = db.parent / "state/audit/apif_identity_merges.csv"
    raw = audit.read_bytes()
    assert any(
        row["team_id"] == "9101" and row["reason"] != "merged"
        for row in csv.DictReader(audit.open())
    )
    reconcile(db, "repeat")
    assert audit.read_bytes() == raw


def test_failure_to_write_audit_rolls_back_database(seed, monkeypatch):
    from zucai.sources.apif_reconcile import reconcile

    db, _ = seed
    before = db.read_bytes()

    def fail(*args):
        raise OSError("fixture failure")

    monkeypatch.setattr("zucai.sources.apif_reconcile.write_audit", fail)
    with pytest.raises(OSError):
        reconcile(db, "failed")
    assert db.read_bytes() == before


@pytest.mark.parametrize("existing_audit", [False, True])
def test_failed_commit_restores_audit_with_database(seed, monkeypatch, existing_audit):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from zucai.sources.apif_reconcile import FIELDS, reconcile

    db, _ = seed
    before = db.read_bytes()
    audit = db.parent / "state/audit/apif_identity_merges.csv"
    previous = (",".join(FIELDS) + "\r\n").encode() if existing_audit else None
    if previous is not None:
        audit.parent.mkdir(parents=True)
        audit.write_bytes(previous)

    def short_timeout(path):
        engine = get_engine(path)

        @event.listens_for(engine, "connect")
        def timeout(connection, _record):
            connection.execute("PRAGMA busy_timeout=1")

        return engine

    monkeypatch.setattr("zucai.sources.apif_reconcile.get_engine", short_timeout)
    with sqlite3.connect(db) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM teams").fetchall()
        with pytest.raises(OperationalError, match="database is locked"):
            reconcile(db, "commit-failed")
        reader.rollback()
    assert db.read_bytes() == before
    assert (audit.read_bytes() if audit.exists() else None) == previous


def test_resolved_conflict_updates_same_audit_key_instead_of_duplicating(seed):
    from zucai.sources.apif_reconcile import reconcile

    db, _ = seed
    with sqlite3.connect(db) as con:
        con.execute("UPDATE teams SET aliases='[\"apif:欧罗巴:team:9999\"]' WHERE id=1")
    reconcile(db, "blocked")
    audit = db.parent / "state/audit/apif_identity_merges.csv"
    count = len(list(csv.DictReader(audit.open())))
    with sqlite3.connect(db) as con:
        aliases = json.loads(con.execute("SELECT aliases FROM teams WHERE id=1").fetchone()[0])
        con.execute(
            "UPDATE teams SET aliases=? WHERE id=1",
            (json.dumps([a for a in aliases if a != "apif:欧罗巴:team:9999"]),),
        )
    report = reconcile(db, "resolved")
    assert report["merged_teams"] == 1 and report["deleted_teams"] == 1
    rows = list(csv.DictReader(audit.open()))
    assert len(rows) == count and all(r["reason"] == "merged" for r in rows)


def test_cloud_reconcile_preserves_archive_watermark_counts_and_frozen_files(
    seed, tmp_path, monkeypatch
):
    import sys
    from datetime import date

    from test_forecast_archive import staged_run
    from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
    from zucai.forecast_archive import publish_forecasts, verify_forecasts

    db, _ = seed
    archive = tmp_path / "archive"
    original = export_feeds(db, archive, date(2026, 11, 3), "seed")
    baseline = tmp_path / "baseline/data/zucai.db"
    restore_feeds(archive, baseline)
    archived_evidence = frozen(baseline)
    stage, run = staged_run(tmp_path / "stage", "unchanged")
    forecasts = publish_forecasts(archive, stage, run)
    frozen_files = {
        name: (archive / name).read_bytes()
        for name in [*forecasts["files"], "forecast_manifest.json"]
    }
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    for index in range(2):
        work = tmp_path / f"run-{index}"
        work.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
        monkeypatch.setattr(
            sys, "argv", ["feeds", "reconcile", "--archive", str(archive), "--run-id", str(index)]
        )
        with responses.RequestsMock() as http:
            main()
            assert not http.calls
        meta = archive_info(archive)
        assert meta["through_beijing"] == original["through_beijing"]
        assert meta["counts"]["matches"] == original["counts"]["matches"]
        assert meta["counts"]["teams"] == original["counts"]["teams"] - 2
        assert meta["collection"]["apif"]["merged_teams"] == (2 if index == 0 else 0)
        assert meta["collection"]["apif"]["rekeyed_matches"] == (3 if index == 0 else 0)
        assert meta["collection"]["apif"]["skipped_conflicts"] == 0
        restored = tmp_path / f"restored-{index}/data/zucai.db"
        restore_feeds(archive, restored)
        assert frozen(restored) == archived_evidence
        assert verify_forecasts(archive) == forecasts
        assert all((archive / name).read_bytes() == raw for name, raw in frozen_files.items())
        assert "合并临时球队" in (work / "summary.md").read_text()


def test_reconcile_cli_requires_manual_actions_before_restore(tmp_path, monkeypatch):
    import sys

    from zucai.cloud_feeds import main

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["feeds", "reconcile", "--archive", str(tmp_path), "--run-id", "x"]
    )
    with responses.RequestsMock() as http, pytest.raises(ValueError, match="workflow_dispatch"):
        main()
    assert not http.calls
