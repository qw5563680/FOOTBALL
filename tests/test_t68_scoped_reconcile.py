"""Scoped offline identity rekeying never widens legacy/daily authorization."""

import json
import sqlite3
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db

SAMPLE = json.loads(Path("data/fixtures/t41/reconcile_jc.json").read_bytes())


@pytest.fixture
def scoped_db(tmp_path):
    db = tmp_path / "data/feeds.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO teams VALUES(?,?,?)",
            [
                (1, "阿尔法", "[]"),
                (2, "贝塔", "[]"),
                (3, "apif:team:100", "[]"),
                (4, "apif:team:200", "[]"),
            ],
        )
        for mid, league, h, a in [
            ("apif:10", "沙职", 3, 4),
            ("jc:10", "沙职", 1, 2),
            ("apif:11", "另一赛事", 3, 4),
        ]:
            con.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES(?,?,?,?,?,?,?)",
                (mid, mid.split(":")[0], league, h, a, SAMPLE["kickoff_utc"], "finished"),
            )
            con.execute(
                "INSERT INTO results(match_id,home_goals,away_goals,available_utc) VALUES(?,?,?,?)",
                (mid, 2, 1, SAMPLE["available_utc"]),
            )
    aliases = tmp_path / "aliases.json"
    aliases.write_text(
        json.dumps(
            {
                "沙职": {
                    pid: dict(
                        canonical=name,
                        status="approved",
                        approved_by="qw5563680",
                        approved_utc=SAMPLE["approved_utc"],
                        approval_pr=SAMPLE["approval_pr"],
                        binding_scope=dict(leagues=["沙职"], category="club/men/senior/first"),
                    )
                    for pid, name in [("100", "阿尔法"), ("200", "贝塔")]
                }
            }
        )
    )
    return db, aliases


def invoke(scoped_db):
    from zucai.sources.apif_scoped_reconcile import reconcile_scoped

    db, aliases = scoped_db
    return reconcile_scoped(db, ["307"], "offline-test", aliases_path=aliases)


def test_two_provisional_opponents_rekey_together_and_repeat(scoped_db):
    db, _ = scoped_db
    with sqlite3.connect(db) as con:
        results = con.execute("SELECT * FROM results ORDER BY match_id").fetchall()
    report = invoke(scoped_db)
    assert report["rekeyed_matches"] == 1 and report["linked"] == 1
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT home_team_id,away_team_id FROM matches WHERE match_id='apif:10'"
        ).fetchone() == (1, 2)
        assert con.execute(
            "SELECT home_team_id,away_team_id FROM matches WHERE match_id='apif:11'"
        ).fetchone() == (3, 4)
        assert con.execute(
            "SELECT counterpart_id FROM matches WHERE match_id='jc:10'"
        ).fetchone() == ("apif:10",)
        assert con.execute("SELECT * FROM results ORDER BY match_id").fetchall() == results
    again = invoke(scoped_db)
    assert again["rekeyed_matches"] == again["linked"] == 0


@pytest.mark.parametrize("attack", ["reverse", "claimed", "multiple", "unapproved", "time_scope"])
def test_scoped_conflicts_not_linked(scoped_db, attack):
    db, aliases = scoped_db
    with sqlite3.connect(db) as con:
        if attack == "reverse":
            con.execute("UPDATE matches SET home_team_id=2,away_team_id=1 WHERE match_id='jc:10'")
        if attack == "claimed":
            con.execute("UPDATE matches SET counterpart_id='fd:existing' WHERE match_id='jc:10'")
        if attack == "multiple":
            con.execute(
                "INSERT INTO matches SELECT 'jc:20',source,league,home_team_id,away_team_id,"
                "kickoff_utc,jc_num,status,result_checked_utc,result_provider,counterpart_id "
                "FROM matches WHERE match_id='jc:10'"
            )
    if attack in {"unapproved", "time_scope"}:
        d = json.loads(aliases.read_bytes())
        for entry in d["沙职"].values():
            if attack == "unapproved":
                entry["status"] = "proposed"
            else:
                entry["binding_scope"].update(
                    start_utc="2030-01-01T00:00:00Z", end_utc="2030-12-31T00:00:00Z"
                )
        aliases.write_text(json.dumps(d))
    report = invoke(scoped_db)
    assert report["linked"] == 0
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT counterpart_id FROM matches WHERE match_id='jc:10'").fetchone()[
            0
        ] == ("fd:existing" if attack == "claimed" else None)
    if attack in {"unapproved", "time_scope"}:
        assert report["rekeyed_matches"] == 0
    else:
        assert report["pending_links"]


def test_legacy_routes_are_not_authorized_by_new_scoped_command(scoped_db):
    from zucai.sources.apif_scoped_reconcile import reconcile_scoped

    db, aliases = scoped_db
    before = db.read_bytes()
    with pytest.raises(ValueError):
        reconcile_scoped(db, ["98"], "wrong-route", aliases_path=aliases)
    assert db.read_bytes() == before


def test_audit_failure_rolls_back_projected_identities(scoped_db, monkeypatch):
    from zucai.sources import apif_scoped_reconcile as scoped

    db, _ = scoped_db
    before = db.read_bytes()

    def fail(*args):
        raise OSError("offline synthetic disk failure")

    monkeypatch.setattr(scoped, "write_audit", fail)
    with pytest.raises(OSError):
        invoke(scoped_db)
    assert db.read_bytes() == before
    assert not (db.parent / "state/audit/jc_relink.csv").exists()


def test_attached_identity_snapshots_require_separate_review(scoped_db):
    from zucai.db import session_scope
    from zucai.models import ApifFixtureIndex

    db, _ = scoped_db
    engine = get_engine(db)
    with session_scope(engine) as session:
        session.add(
            ApifFixtureIndex(
                provider_fixture_id="10",
                match_id="apif:10",
                league="沙职",
                provider_league_id=307,
                season=2026,
                kickoff_utc=SAMPLE["kickoff_utc"],
                home_provider_id=100,
                away_provider_id=200,
                home_team_id=3,
                away_team_id=4,
                captured_utc=SAMPLE["available_utc"],
                raw_sha256="a" * 64,
                raw_location="offline-fixture",
                parser_version="fixture",
                evidence={},
            )
        )
    engine.dispose()
    before = db.read_bytes()
    report = invoke(scoped_db)
    assert report["rekeyed_matches"] == 0
    assert report["status"] == "partial"
    assert db.read_bytes() == before


def test_jc_arriving_after_identity_merge_can_link_on_repeat(scoped_db):
    db, _ = scoped_db
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM results WHERE match_id='jc:10'")
        con.execute("DELETE FROM matches WHERE match_id='jc:10'")
    assert invoke(scoped_db)["rekeyed_matches"] == 1
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('jc:10','jc','沙职',1,2,?,'finished')",
            (SAMPLE["kickoff_utc"],),
        )
    second = invoke(scoped_db)
    assert second["rekeyed_matches"] == 0 and second["linked"] == 1


def test_pending_jc_conflict_is_still_reported_after_rekey(scoped_db):
    db, _ = scoped_db
    with sqlite3.connect(db) as con:
        con.execute("UPDATE matches SET home_team_id=2,away_team_id=1 WHERE match_id='jc:10'")
    assert invoke(scoped_db)["pending_links"]
    assert invoke(scoped_db)["pending_links"]


def test_partial_time_scope_moves_only_authorized_history(scoped_db):
    from datetime import datetime, timedelta

    db, path = scoped_db
    when = datetime.fromisoformat(SAMPLE["kickoff_utc"])
    aliases = json.loads(path.read_bytes())
    for entry in aliases["沙职"].values():
        entry["binding_scope"].update(
            start_utc=(when - timedelta(hours=1)).isoformat(),
            end_utc=(when + timedelta(hours=1)).isoformat(),
        )
    path.write_text(json.dumps(aliases))
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('apif:12','apif','沙职',3,4,?,'finished')",
            ((when + timedelta(days=1)).isoformat(),),
        )
    result = invoke(scoped_db)
    assert result["rekeyed_matches"] == result["linked"] == 1
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT home_team_id,away_team_id FROM matches WHERE match_id='apif:12'"
        ).fetchone() == (3, 4)
        assert con.execute("SELECT COUNT(*) FROM teams WHERE id IN (3,4)").fetchone() == (2,)


def test_cloud_scoped_reconcile_export_then_restore_stays_consistent(
    scoped_db, tmp_path, monkeypatch
):
    """Online entry: restore, scoped merge, export, publish-verification; the
    exported archive restores byte-consistent databases every time."""
    import sys
    from datetime import date

    from test_apif_reconcile import frozen
    from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds

    db, aliases = scoped_db
    monkeypatch.setattr("zucai.sources.apif_scoped_reconcile.ALIASES", aliases)
    archive = tmp_path / "archive"
    original = export_feeds(
        db,
        archive,
        date(2026, 11, 3),
        "seed",
        collection={"apif": {"status": "complete", "run_id": "seed"}},
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "scoped-reconcile",
            "--archive",
            str(archive),
            "--run-id",
            "s1",
            "--leagues",
            "307",
        ],
    )
    main()
    merged = work / "data/zucai.db"
    meta = archive_info(archive)
    record = meta["collection"]["apif_scoped"]
    assert record["operation"] == "scoped-reconcile"
    assert record["linked"] == 1
    assert record["rekeyed_matches"] == 1
    # Manifest record stays slim: only status and counts, no match lists or
    # full decisions; bounded size; original per-source records survive.
    assert set(record) == {
        "operation",
        "status",
        "run_id",
        "leagues",
        "rekeyed_matches",
        "linked",
        "pending_links",
        "decisions",
    }
    assert isinstance(record["decisions"], int) and "changed_match_ids" not in record
    assert len(json.dumps(record)) < 2000
    assert meta["collection"]["apif"] == {"status": "complete", "run_id": "seed"}
    # The manual operation must never enter the daily status.
    from zucai.cloud_feeds import daily_collection

    daily = daily_collection(meta["collection"])
    assert "apif_scoped" not in daily
    assert daily["apif"] == {"status": "complete", "run_id": "seed"}
    assert "赛事：307 沙职。" in (work / "summary.md").read_text()
    # Export-then-restore consistency: independent restores agree with the
    # merged working database, and repeated restores leave the archive stable.
    for index in range(2):
        restored = tmp_path / f"restored-{index}/data/zucai.db"
        restore_feeds(archive, restored)
        assert frozen(restored) == frozen(merged)
    before = {
        p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    }
    restore_feeds(archive, tmp_path / "restored-again/data/zucai.db")
    after = {
        p.relative_to(archive).as_posix(): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    }
    assert after == before
    assert original["counts"]["matches"] == meta["counts"]["matches"]
    assert "APIF 范围身份合并" in (work / "summary.md").read_text()
