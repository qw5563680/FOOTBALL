"""A review plan pins only the exact candidate, identity and match evidence it read."""

import json
import sqlite3
import sys
from datetime import date

import pytest

from test_t58_jc_candidate_review import case as case
from test_t58_jc_candidate_review import write_json
from zucai.cloud_candidate_review import apply_to_archive
from zucai.cloud_feeds import CANDIDATE_REVIEW_DIR, archive_info, export_feeds, restore_feeds
from zucai.jc_candidate_review import main, plan_candidates, review_candidates


def seal(case, tmp_path, monkeypatch, *, decision=0):
    db, _, _, old = case
    draft, plan = tmp_path / "draft.json", tmp_path / "scope-plan.json"
    write_json(draft, {"reviewer": old["reviewer"], "decisions": [old["decisions"][decision]]})
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "jc_candidate_review",
            "plan",
            "--db",
            str(db),
            "--decisions",
            str(draft),
            "--out",
            str(plan),
        ],
    )
    main()
    return plan


def test_unrelated_collection_does_not_stale_scoped_plan(case, tmp_path, monkeypatch):
    db, queue, _, _ = case
    plan_path = seal(case, tmp_path, monkeypatch)
    plan = json.loads(plan_path.read_bytes())
    assert plan["version"] == 2
    assert "inventory_sha256" not in plan and "queue_sha256" not in plan
    assert len(plan["decisions"][0]["scope_sha256"]) == 64

    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO teams VALUES(5,'后来新增球队','[]')")
        con.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('jc:8','jc','测试联赛',5,2,"
            "'2026-11-13 10:00:00.000000','scheduled')"
        )
        con.execute("UPDATE teams SET aliases='[\"无关改动\"]' WHERE id=3")
        con.execute("INSERT INTO results(match_id,home_goals,away_goals) VALUES('jc:8',1,1)")
    candidates = json.loads(queue.read_bytes())
    candidates["测试联赛"]["可疑简称"]["other_note"] = "与计划无关的队列更新"
    write_json(queue, candidates)
    unchanged_db = db.read_bytes()

    report = review_candidates(db, plan_path, tmp_path / "audit")
    assert db.read_bytes() == unchanged_db
    assert report["pending_before"] == 2 and report["pending_after"] == 1
    decision = report["decisions"][0]
    assert [team["id"] for team in decision["scoped_evidence"]["teams"]] == [1, 2]
    assert [match["match_id"] for match in decision["scoped_evidence"]["matches"]] == ["jc:1"]
    assert json.loads(queue.read_bytes())["测试联赛"]["新身份"]["status"] == "cleared"


@pytest.mark.parametrize(
    "change",
    [
        "candidate",
        "team",
        "opponent",
        "match",
        "match_status",
        "new_conflict",
        "new_result",
        "verified_score",
    ],
)
def test_related_evidence_changes_require_review(case, tmp_path, monkeypatch, change):
    db, queue, _, _ = case
    if change == "verified_score":
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO results(match_id,home_goals,away_goals,"
                "available_utc,available_source) "
                "VALUES('jc:3',0,0,'2026-11-12T14:00:00Z','captured')"
            )
    plan = seal(case, tmp_path, monkeypatch, decision=1)
    if change == "candidate":
        candidates = json.loads(queue.read_bytes())
        candidates["测试联赛"]["可疑简称"]["other_note"] = "该候选的新事实"
        write_json(queue, candidates)
    else:
        with sqlite3.connect(db) as con:
            if change == "team":
                con.execute('UPDATE teams SET aliases=\'["Possible Duplicate","后来"]\' WHERE id=4')
            elif change == "opponent":
                con.execute("UPDATE teams SET name='变更后的对手' WHERE id=2")
            elif change == "match":
                con.execute("UPDATE matches SET counterpart_id='apif:99' WHERE match_id='jc:3'")
            elif change == "match_status":
                con.execute("UPDATE matches SET status='finished' WHERE match_id='jc:3'")
            elif change == "new_result":
                con.execute(
                    "INSERT INTO results(match_id,home_goals,away_goals) VALUES('jc:3',0,0)"
                )
            elif change == "verified_score":
                con.execute("UPDATE results SET home_goals=1 WHERE match_id='jc:3'")
            else:
                con.execute("INSERT INTO teams VALUES(5,'另一个相关队','[]')")
                con.execute(
                    "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                    "kickoff_utc,status) VALUES('apif:8','apif','测试联赛',5,2,"
                    "'2026-11-12 12:00:00.000000','scheduled')"
                )

    original_db, original_queue = db.read_bytes(), queue.read_bytes()
    with pytest.raises(ValueError, match="事实已变化"):
        review_candidates(db, plan, tmp_path / "audit")
    assert queue.read_bytes() == original_queue
    assert db.read_bytes() == original_db
    assert not (tmp_path / "audit").exists()


def test_scoped_plan_still_rechecks_duplicate_rules(case, tmp_path, monkeypatch):
    db, queue, _, _ = case
    plan_path = seal(case, tmp_path, monkeypatch, decision=1)
    plan = json.loads(plan_path.read_bytes())
    plan["decisions"][0]["conclusion"] = "distinct"
    write_json(plan_path, plan)
    original = queue.read_bytes()
    with pytest.raises(ValueError, match="重复|可疑"):
        review_candidates(db, plan_path, tmp_path / "audit")
    assert queue.read_bytes() == original
    assert not (tmp_path / "audit").exists()


def test_archive_review_applies_v2_without_rewriting_v1_evidence(case, tmp_path):
    db, _, legacy_plan, original = case
    archive = tmp_path / "archive"
    through = date(2026, 11, 13)
    export_feeds(db, archive, through, "before-review")
    apply_to_archive(archive, legacy_plan, "legacy-plan")
    legacy_prefix = archive / CANDIDATE_REVIEW_DIR / "legacy-plan"
    legacy_files = {path.name: path.read_bytes() for path in legacy_prefix.iterdir()}

    copy = tmp_path / "restore" / "feeds.db"
    restore_feeds(archive, copy)
    plan = tmp_path / "scoped-plan.json"
    write_json(
        plan,
        plan_candidates(
            copy, {"reviewer": original["reviewer"], "decisions": [original["decisions"][1]]}
        ),
    )
    with sqlite3.connect(copy) as con:
        con.execute("INSERT INTO teams VALUES(5,'后来新增球队','[]')")
    export_feeds(copy, archive, through, "unrelated-collection")

    op = apply_to_archive(archive, plan, "scoped-plan")
    assert (op["pending_before"], op["pending_after"], op["requests"]) == (1, 1, 0)
    assert {path.name: path.read_bytes() for path in legacy_prefix.iterdir()} == legacy_files
    assert op["audit"] in archive_info(archive)["files"]
    audit = json.loads((archive / op["audit"]).read_bytes())
    assert audit["plan_version"] == 2
    assert (
        audit["decisions"][0]["scoped_evidence"]["candidate"]["review"]["conclusion"]
        == "unresolved"
    )
    assert [t["id"] for t in audit["decisions"][0]["scoped_evidence"]["teams"]] == [2, 3, 4]
