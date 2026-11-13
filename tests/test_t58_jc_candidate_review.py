"""Offline review changes only an evidence-backed pending-name disposition."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from zucai.db import get_engine, init_db
from zucai.digest_aliases import pending_name_lines
from zucai.jc_candidate_review import inspect_candidates, review_candidates


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def case(tmp_path):
    db = tmp_path / "copy/feeds.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(db) as con:
        con.executemany(
            "INSERT INTO teams(id,name,aliases) VALUES(?,?,?)",
            [
                (1, "新身份", "[]"),
                (2, "对手", "[]"),
                (3, "可疑简称", "[]"),
                (4, "既有球队", '["Possible Duplicate"]'),
            ],
        )
        for mid, tid in [("jc:1", 1), ("jc:3", 3)]:
            con.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES(?,?,?,?,?,?,?)",
                (
                    mid,
                    "jc",
                    "测试联赛",
                    tid,
                    2,
                    f"2026-11-12 {9 + tid:02}:00:00.000000",
                    "scheduled",
                ),
            )
    queue = db.parent / "state/alias_candidates/jc.json"
    entries = {}
    for name, tid in [("新身份", 1), ("可疑简称", 3)]:
        entries[name] = dict(
            source="jc",
            canonical=None,
            status="pending",
            team_id=tid,
            evidence=[
                dict(
                    match_id=f"jc:{tid}",
                    side="home",
                    kickoff_utc=f"2026-11-12T{9 + tid:02}:00:00Z",
                    captured_utc="2026-11-11T10:00:00Z",
                )
            ],
        )
    write_json(queue, {"测试联赛": entries})
    info = inspect_candidates(db)
    plan = dict(
        version=1,
        reviewer="offline test reviewer",
        inventory_sha256=info["inventory_sha256"],
        queue_sha256=info["queue_sha256"],
        decisions=[
            dict(
                league="测试联赛",
                raw_name="新身份",
                team_id=1,
                conclusion="distinct",
                checked_names=["新身份", "New Team"],
                reason="逐项核对固定身份清单，所列精确名称只有当前身份",
            ),
            dict(
                league="测试联赛",
                raw_name="可疑简称",
                team_id=3,
                conclusion="unresolved",
                checked_names=["可疑简称", "Possible Duplicate"],
                reason="疑似既有身份，尚无同场配对证据，保持待核",
            ),
        ],
    )
    path = tmp_path / "review.json"
    write_json(path, plan)
    return db, queue, path, plan


def test_review_clears_distinct_but_keeps_suspect_and_database_bytes(case, tmp_path):
    db, queue, plan_path, _ = case
    original_db, original_queue = db.read_bytes(), json.loads(queue.read_bytes())
    report = review_candidates(db, plan_path, tmp_path / "audit")
    after = json.loads(queue.read_bytes())
    assert report["pending_before"] == 2
    assert report["pending_after"] == 1
    assert after["测试联赛"]["新身份"]["status"] == "cleared"
    assert after["测试联赛"]["可疑简称"]["status"] == "pending"
    assert report["decisions"][1]["other_team_ids"] == [4]
    assert db.read_bytes() == original_db
    for name, entry in original_queue["测试联赛"].items():
        assert after["测试联赛"][name]["evidence"] == entry["evidence"]
        assert after["测试联赛"][name]["canonical"] is None
    review = after["测试联赛"]["新身份"]["review"]
    assert datetime.fromisoformat(review["recorded_utc"]).tzinfo == UTC
    assert (
        review["audit_sha256"]
        == hashlib.sha256((tmp_path / "audit/review.json").read_bytes()).hexdigest()
    )
    feed = {"files": {"state/alias_candidates/jc.json": "unused"}}
    before = pending_name_lines(db.parent, feed, "2026-11-12T00:00:00Z")
    now = pending_name_lines(db.parent, feed, datetime.now(UTC).isoformat())
    assert "2 个" in before[0], "A later review must not retroactively erase pending history"
    assert "1 个" in now[0] and "可疑简称" in now[0] and "新身份" not in now[0]


@pytest.mark.parametrize("mutation", ["duplicate", "observation", "inventory", "queue", "reason"])
def test_bad_or_stale_review_is_all_or_nothing(case, tmp_path, mutation):
    db, queue, plan_path, plan = case
    if mutation == "duplicate":
        plan["decisions"][1]["conclusion"] = "distinct"
    elif mutation == "observation":
        candidates = json.loads(queue.read_bytes())
        candidates["测试联赛"]["新身份"]["evidence"][0]["side"] = "away"
        write_json(queue, candidates)
        plan["queue_sha256"] = inspect_candidates(db)["queue_sha256"]
    elif mutation == "inventory":
        with sqlite3.connect(db) as con:
            con.execute("INSERT INTO teams(id,name,aliases) VALUES(5,'后来新增球队','[]')")
    elif mutation == "queue":
        queue.write_text(queue.read_text() + "\n", encoding="utf-8")
    else:
        plan["decisions"][0]["reason"] = ""
    write_json(plan_path, plan)
    original = queue.read_bytes()
    with pytest.raises(ValueError):
        review_candidates(db, plan_path, tmp_path / "audit")
    assert queue.read_bytes() == original
    assert not (tmp_path / "audit").exists()


def test_confirmed_duplicate_stays_pending_without_reconciling(case, tmp_path):
    db, queue, plan_path, plan = case
    plan["decisions"][1]["conclusion"] = "duplicate"
    write_json(plan_path, plan)
    before = db.read_bytes()
    report = review_candidates(db, plan_path, tmp_path / "audit")
    assert report["decisions"][1]["next_action"] == "T54_reconcile_required"
    assert json.loads(queue.read_bytes())["测试联赛"]["可疑简称"]["status"] == "pending"
    assert db.read_bytes() == before


def test_same_match_identity_conflict_blocks_clear_even_when_names_not_listed(case, tmp_path):
    db, queue, plan_path, plan = case
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('apif:42','apif','测试联赛',4,2,"
            "'2026-11-12 10:00:00.000000','scheduled')"
        )
    plan["inventory_sha256"] = inspect_candidates(db)["inventory_sha256"]
    write_json(plan_path, plan)
    before = queue.read_bytes()
    with pytest.raises(ValueError, match="重复|可疑"):
        review_candidates(db, plan_path, tmp_path / "audit")
    assert queue.read_bytes() == before


def test_no_plan_never_clears_a_candidate_and_audit_is_not_overwritten(case, tmp_path):
    db, queue, plan_path, plan = case
    plan["decisions"] = plan["decisions"][:1]
    write_json(plan_path, plan)
    report = review_candidates(db, plan_path, tmp_path / "audit")
    assert report["pending_after"] == 1
    unchanged = json.loads(queue.read_bytes())["测试联赛"]["可疑简称"]
    assert unchanged["status"] == "pending" and "review" not in unchanged
    before = (tmp_path / "audit/review.json").read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        review_candidates(db, plan_path, tmp_path / "audit")
    assert (tmp_path / "audit/review.json").read_bytes() == before


def test_review_survives_feed_roundtrip_and_next_observation(case, tmp_path):
    from datetime import date

    from sqlalchemy.orm import Session

    from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
    from zucai.sources.sporttery import JcMatch
    from zucai.sources.sporttery_aliases import resolve_teams, save_candidates

    db, queue, plan_path, _ = case
    review_candidates(db, plan_path, tmp_path / "audit")
    reviewed = queue.read_bytes()
    engine = get_engine(db)
    with Session(engine) as session:
        pending = []
        fixture = JcMatch(
            "jc:next",
            date(2026, 11, 14),
            "周六001",
            "测试联赛",
            "新身份",
            "对手",
            datetime(2026, 11, 14, 10, tzinfo=UTC),
            [],
        )
        home, _ = resolve_teams(
            session, fixture, datetime(2026, 11, 13, tzinfo=UTC), aliases={}, candidates=pending
        )
        assert home.id == 1 and pending == []
    engine.dispose()
    save_candidates(db, pending)
    assert queue.read_bytes() == reviewed
    archive = tmp_path / "export"
    export_feeds(db, archive, date(2026, 11, 13), "t58-test")
    manifest = archive_info(archive)
    assert (
        manifest["files"]["state/alias_candidates/jc.json"] == hashlib.sha256(reviewed).hexdigest()
    )
    restored = tmp_path / "restored/feeds.db"
    restore_feeds(archive, restored)
    assert (restored.parent / "state/alias_candidates/jc.json").read_bytes() == reviewed
    assert (
        inspect_candidates(restored)["inventory_sha256"]
        == inspect_candidates(db)["inventory_sha256"]
    )
