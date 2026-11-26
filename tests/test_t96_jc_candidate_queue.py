"""Offline checks for resolving only JC candidates whose identity is now proved."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
LEAGUE = "测试联赛"
KO = "2026-11-25T19:30:00.000000Z"
PR = "https://github.com/qw5563680/FOOTBALL/pull/99"
QUEUE = Path("state/alias_candidates/jc.json")


def write_queue(database, entry, name="样本队"):
    queue = database.parent / QUEUE
    queue.parent.mkdir(parents=True)
    queue.write_text(json.dumps({LEAGUE: {name: entry}}, ensure_ascii=False), encoding="utf-8")
    return queue


@pytest.fixture
def fixture(tmp_path):
    db = tmp_path / "feeds.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO teams(id,name,aliases) VALUES(?,?,?)",
            [(1, "样本队", "[]"), (2, "对手", "[]")],
        )
        connection.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES(?,?,?,?,?,?,?)",
            ("jc:880101", "jc", LEAGUE, 1, 2, KO, "scheduled"),
        )
    entry = {
        "source": "jc",
        "status": "pending",
        "canonical": None,
        "team_id": 1,
        "evidence": [
            {
                "match_id": "jc:880101",
                "side": "home",
                "kickoff_utc": KO,
                "captured_utc": "2026-11-24T10:00:00Z",
            }
        ],
        "review": {"conclusion": "duplicate", "recorded_utc": "2026-11-25T01:00:00Z"},
        "legacy_example": "retained",
    }
    return db, entry


def approve(*, canonical="样本队", **extra):
    return {
        "canonical": canonical,
        "status": "approved",
        "approved_by": "qw5563680",
        "approval_pr": PR,
        "approved_utc": "2026-11-26T00:00:00Z",
        **extra,
    }


@pytest.mark.parametrize(
    "change,expected",
    [
        ("verified", 1),
        ("auto_verified", 0),
        ("missing_pr", 0),
        ("wrong_name", 0),
        ("separate_team", 0),
        ("ambiguous_binding", 0),
        ("expired_scope", 0),
        ("changed_match_owner", 0),
        ("rescheduled", 0),
        ("legacy_no_capture", 0),
    ],
)
def test_numeric_binding_needs_approved_provenance_and_current_owner(fixture, change, expected):
    from zucai.jc_candidate_queue import resolve_pending

    db, original = fixture
    binding = f"apif:{LEAGUE}:team:780101"
    approval = approve()
    if change == "missing_pr":
        del approval["approval_pr"]
    elif change == "auto_verified":
        approval["status"] = "auto_verified"
    elif change == "wrong_name":
        approval["canonical"] = "另一身份"
    elif change == "expired_scope":
        approval["binding_scope"] = {
            "leagues": [LEAGUE],
            "category": "club/men/senior/first",
            "start_utc": "2026-11-26T00:00:00Z",
            "end_utc": "2026-12-01T00:00:00Z",
        }

    with sqlite3.connect(db) as connection:
        if change not in {"separate_team", "changed_match_owner"}:
            connection.execute("UPDATE teams SET aliases=? WHERE id=1", (json.dumps([binding]),))
        if change in {"separate_team", "ambiguous_binding", "changed_match_owner"}:
            connection.execute(
                "INSERT INTO teams(id,name,aliases) VALUES(?,?,?)",
                (3, "另一身份", json.dumps([binding])),
            )
        if change == "changed_match_owner":
            connection.execute("UPDATE matches SET home_team_id=3 WHERE match_id='jc:880101'")
        if change == "rescheduled":
            connection.execute(
                "UPDATE matches SET kickoff_utc='2026-11-25T19:40:00.000000Z' "
                "WHERE match_id='jc:880101'"
            )
    if change == "legacy_no_capture":
        del original["evidence"][0]["captured_utc"]
    queue = write_queue(db, original)
    before = db.read_bytes()
    alias_tables = {"apif": {LEAGUE: {"780101": approval}}}
    assert resolve_pending(db, "local-1", recorded=NOW, aliases=alias_tables) == expected
    resolved = json.loads(queue.read_bytes())[LEAGUE]["样本队"]
    if expected:
        assert resolved.pop("resolution") == {
            "basis": "approved_api_binding",
            "source": "apif",
            "run_id": "local-1",
            "recorded_utc": NOW.isoformat(),
            "canonical": "样本队",
            "canonical_team_id": 1,
            "binding": binding,
            "approval_pr": PR,
            "approved_by": "qw5563680",
            "approved_utc": approval["approved_utc"],
        }
        assert resolved.pop("status") == "resolved"
        assert resolved == {k: v for k, v in original.items() if k != "status"}
    else:
        assert resolved == original
    resolved_bytes = queue.read_bytes()
    assert resolve_pending(db, "local-2", recorded=NOW, aliases=alias_tables) == 0
    assert queue.read_bytes() == resolved_bytes
    assert db.read_bytes() == before


@pytest.mark.parametrize("merged", [True, False])
def test_fd_canonical_side_only_resolves_if_both_names_share_one_team(fixture, merged):
    from zucai.jc_candidate_queue import resolve_pending

    db, entry = fixture
    with sqlite3.connect(db) as connection:
        if merged:
            connection.execute("UPDATE teams SET aliases=? WHERE id=1", (json.dumps(["Test FC"]),))
        else:
            connection.execute(
                "INSERT INTO teams(id,name,aliases) VALUES(?,?,?)",
                (3, "Test FC", "[]"),
            )
    target = write_queue(db, entry)
    aliases = {"fd": {LEAGUE: {"Test FC": approve()}}}
    assert resolve_pending(db, "offline", recorded=NOW, aliases=aliases) == int(merged)
    assert json.loads(target.read_bytes())[LEAGUE]["样本队"]["status"] == (
        "resolved" if merged else "pending"
    )


@pytest.mark.parametrize("already_rekeyed", [True, False])
def test_jc_approved_mapping_needs_rekeyed_match_and_merged_identity(fixture, already_rekeyed):
    from zucai.jc_candidate_queue import resolve_pending

    db, entry = fixture
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,aliases) VALUES(?,?,?)",
            (3, "样本规范名", json.dumps(["样本队"] if already_rekeyed else [])),
        )
        if already_rekeyed:
            connection.execute("UPDATE matches SET home_team_id=3 WHERE match_id='jc:880101'")
            connection.execute("DELETE FROM teams WHERE id=1")
    target = write_queue(db, entry)
    aliases = {
        "jc": {
            LEAGUE: {
                "样本队": {
                    "status": "approved",
                    "canonical": "样本规范名",
                    "approval_source": "docs/tasks/T54-jc-short-name-duplicate-team.md",
                }
            }
        }
    }
    assert resolve_pending(db, "offline", recorded=NOW, aliases=aliases) == int(already_rekeyed)
    row = json.loads(target.read_bytes())[LEAGUE]["样本队"]
    if already_rekeyed:
        assert row["resolution"]["canonical_team_id"] == 3 and row["team_id"] == 1
        assert (
            row["resolution"]["approval_source"]
            == aliases["jc"][LEAGUE]["样本队"]["approval_source"]
        )
    else:
        assert row == entry


@pytest.mark.parametrize("case", ["rekeyed", "old_still_plays", "reversed", "provider_missing"])
def test_legacy_jc_rekey_can_leave_unused_team_row(fixture, case):
    from zucai.jc_candidate_queue import resolve_pending

    db, entry = fixture
    binding = f"apif:{LEAGUE}:team:780101"
    provider_id = "apif:880102"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,aliases) VALUES(?,?,?)",
            (3, "样本规范名", json.dumps([binding])),
        )
        if case != "provider_missing":
            home_id, away_id = (2, 3) if case == "reversed" else (3, 2)
            connection.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES(?,?,?,?,?,?,?)",
                (provider_id, "apif", LEAGUE, home_id, away_id, KO, "scheduled"),
            )
        if case == "old_still_plays":
            connection.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES(?,?,?,?,?,?,?)",
                ("jc:880103", "jc", LEAGUE, 1, 2, KO, "scheduled"),
            )
        connection.execute(
            "UPDATE matches SET home_team_id=3,counterpart_id=? WHERE match_id='jc:880101'",
            (provider_id,),
        )
    queue = write_queue(db, entry)
    approval = approve(
        canonical="样本规范名",
        evidence=[
            {
                "jc_match_id": "jc:880101",
                "provider_match_id": provider_id,
                "league": LEAGUE,
                "kickoff_utc": KO,
                "side": "home",
                "raw_team_id": 1,
                "canonical_team_id": 3,
                "opponent_team_id": 2,
                "canonical_binding": binding,
            }
        ],
    )
    aliases = {"jc": {LEAGUE: {"样本队": approval}}}
    expected = case == "rekeyed"
    assert resolve_pending(db, "offline", recorded=NOW, aliases=aliases) == int(expected)
    candidate = json.loads(queue.read_bytes())[LEAGUE]["样本队"]
    assert candidate["status"] == ("resolved" if expected else "pending")


@pytest.mark.parametrize("command", ["sync", "apis"])
def test_collection_archives_resolutions_in_the_current_run(
    fixture, tmp_path, monkeypatch, command
):
    import sys
    from datetime import date

    from zucai import cloud_feeds, jc_candidate_queue

    db, entry = fixture
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE teams SET aliases=? WHERE id=1",
            (json.dumps([f"apif:{LEAGUE}:team:780101"]),),
        )
    write_queue(db, entry)
    archive = tmp_path / "archive"
    cloud_feeds.export_feeds(db, archive, date(2026, 11, 26), "seed")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        jc_candidate_queue,
        "read_json",
        lambda path: {LEAGUE: {"780101": approve()}} if path.name == "api_football.json" else {},
    )
    monkeypatch.setattr(
        sys, "argv", ["feeds", command, "--archive", str(archive), "--run-id", "t96-test"]
    )
    report = dict(
        status="complete", inserted=0, linked_existing=0, requests=0, rejected_rows=0, errors=[]
    )
    monkeypatch.setattr(cloud_feeds, "collect_jc", lambda *args: (report.copy(), True))
    monkeypatch.setattr("zucai.sources.sfc.collect_current_sfc", lambda *a, **kw: report.copy())
    monkeypatch.setattr("zucai.cloud_apis.collect_apis", lambda *a: {"apif": report.copy()})

    def no_requests(*args, **kwargs):
        raise AssertionError("offline replay attempted a network request")

    monkeypatch.setattr("requests.sessions.Session.request", no_requests)
    cloud_feeds.main()
    meta = cloud_feeds.archive_info(archive)
    row = json.loads((archive / QUEUE).read_bytes())[LEAGUE]["样本队"]
    assert row["status"] == "resolved"
    assert row["resolution"]["run_id"] == meta["run"] == "t96-test"
    assert row["resolution"]["approval_pr"] == PR
