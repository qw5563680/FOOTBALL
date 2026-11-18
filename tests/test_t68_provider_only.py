"""T68: identities for provider teams JC never sells are approved team by team and
re-verified against the archive when applied."""

import copy
import csv
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from zucai.db import get_engine, init_db

ROUTE = {"league": "测试乙", "current_season": 2026}


def _apif(mid, home, away, when, names):
    return dict(
        match_id=f"apif:{mid}",
        competition_id="89",
        season=2026,
        complete=True,
        home=home,
        away=away,
        home_name=names[home],
        away_name=names[away],
        kickoff_utc=when,
    )


def _official(mid, home, away, when):
    return dict(
        match_id=f"jc:{mid}",
        competition_id="89",
        kickoff_utc=when,
        status="finished",
        home_source_id=home,
        away_source_id=away,
    )


@pytest.fixture
def facts(monkeypatch):
    import zucai.identity_provider_only as module

    monkeypatch.setattr(module, "_route", lambda code: dict(ROUTE))
    names = {"1": "Alpha", "2": "Beta", "9": "Jong Alpha"}
    return dict(
        apif=[
            _apif(1, "1", "2", "2026-08-08T14:00:00+00:00", names),
            _apif(2, "2", "1", "2026-09-08T14:00:00+00:00", names),
            _apif(3, "9", "1", "2026-08-15T14:00:00+00:00", names),
            _apif(4, "2", "9", "2026-08-22T14:00:00+00:00", names),
        ],
        official=[
            _official(101, "10", "20", "2026-08-08T14:00:00+00:00"),
            _official(102, "20", "10", "2026-09-08T14:00:00+00:00"),
        ],
        formal={},
    )


def _decision(**changes):
    row = dict(
        action="create_provider_identity",
        status="approved",
        provider="apif",
        competition_id="89",
        league="测试乙",
        provider_team_id="9",
        provider_labels=["Jong Alpha"],
        canonical="青年阿尔法",
        category="club/men/senior/reserve",
        approved_by="qw5563680",
        approved_utc=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        review_url="https://github.com/qw5563680/FOOTBALL/pull/192",
    )
    row.update(changes)
    return row


def test_reserve_side_never_sold_is_the_only_candidate(facts):
    from zucai.identity_provider_only import candidates

    rows = candidates(facts, "89")
    assert [(r["provider_team_id"], r["provider_labels"], r["fixtures"]) for r in rows] == [
        ("9", ["Jong Alpha"], 2)
    ]
    assert rows[0]["blockers"] == []
    assert rows[0]["official_teams"] == 2


def test_first_team_label_is_never_a_reserve_side(facts):
    from zucai.identity_provider_only import candidates

    for row in facts["apif"]:
        for side in ("home", "away"):
            if row[side] == "9":
                row[f"{side}_name"] = "Willem II"
    assert candidates(facts, "89")[0]["blockers"] == ["label_without_reserve_marker"]


def test_one_unmarked_label_among_several_blocks(facts):
    from zucai.identity_provider_only import candidates

    facts["apif"][2]["home_name"] = "Alpha Town"
    assert "label_without_reserve_marker" in candidates(facts, "89")[0]["blockers"]


def test_side_that_jc_sold_is_refused(facts):
    from zucai.identity_provider_only import candidates

    # JC sold a fixture of official team 10 (provider 1) at the time provider 9 played it.
    facts["official"].append(_official(103, "30", "10", "2026-08-15T15:00:00+00:00"))
    row = next(r for r in candidates(facts, "89") if r["provider_team_id"] == "9")
    assert "fixture_sold_by_jc" in row["blockers"]
    assert row["details"]["fixtures_sold_by_jc"] == ["apif:3"]


def test_unsettled_official_team_blocks_every_candidate(facts):
    from zucai.identity_provider_only import candidates

    facts["official"].append(_official(104, "40", "50", "2026-10-01T14:00:00+00:00"))
    row = candidates(facts, "89")[0]
    assert "official_teams_without_settled_provider_id" in row["blockers"]
    assert row["details"]["unsettled_official_teams"] == ["40", "50"]


def test_bound_or_witnessed_provider_id_is_not_a_candidate(facts):
    from zucai.identity_provider_only import candidates

    facts["formal"] = {"测试乙": {"9": {"status": "approved", "canonical": "x"}}}
    assert candidates(facts, "89") == []


def test_cancelled_official_fixture_does_not_count_as_sold(facts):
    from zucai.identity_provider_only import candidates

    facts["official"].append(
        dict(_official(105, "30", "10", "2026-08-15T15:00:00+00:00"), status="cancelled")
    )
    assert candidates(facts, "89")[0]["blockers"] == []


@pytest.fixture
def database(tmp_path, facts, monkeypatch):
    import zucai.identity_binding_archive as archive_module

    monkeypatch.setattr(archive_module, "load_archive", lambda root, leagues=None: facts)
    db = tmp_path / "data/feeds.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    return db


def _approvals(tmp_path, decisions):
    path = tmp_path / "approvals.json"
    path.write_text(
        json.dumps(dict(version=1, decisions=[], provider_only_decisions=decisions)),
        encoding="utf-8",
    )
    return path


def _teams(db):
    with sqlite3.connect(db) as con:
        return con.execute("SELECT name, aliases FROM teams ORDER BY id").fetchall()


def test_apply_creates_row_and_receipt_once(database, tmp_path):
    from zucai.identity_provider_only import RECEIPTS, apply_provider_only

    decision = _decision()
    path = _approvals(tmp_path, [decision])
    first = apply_provider_only(database, tmp_path, path)
    assert (first["created"], first["already_applied"], first["matches_changed"]) == (1, 0, 0)
    assert _teams(database) == [("青年阿尔法", "[]")]
    with (database.parent / RECEIPTS).open(encoding="utf-8", newline="") as stream:
        receipts = list(csv.DictReader(stream))
    assert [(r["competition_id"], r["provider_team_id"], r["canonical"]) for r in receipts] == [
        ("89", "9", "青年阿尔法")
    ]
    assert json.loads(receipts[0]["decision_json"]) == decision
    again = apply_provider_only(database, tmp_path, path)
    assert (again["created"], again["already_applied"]) == (0, 1)
    assert len(_teams(database)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        dict(status="proposed"),
        dict(approved_by="someone-else"),
        dict(review_url="https://example.com/pull/1"),
        dict(category="club/men/senior/first"),
        dict(canonical="apif:team:9"),
        dict(canonical="青年阿尔法队"),
        dict(provider_labels=[]),
        dict(provider_labels=["Jong Other"]),
        dict(provider_team_id="1"),
        dict(provider_team_id="77"),
        dict(approved_utc=(datetime.now(UTC) + timedelta(hours=1)).isoformat()),
    ],
)
def test_apply_rejects_invalid_approval_without_writes(database, tmp_path, changes):
    from zucai.identity_provider_only import RECEIPTS, apply_provider_only

    path = _approvals(tmp_path, [_decision(**changes)])
    with pytest.raises(ValueError):
        apply_provider_only(database, tmp_path, path)
    assert _teams(database) == []
    assert not (database.parent / RECEIPTS).exists()


def test_apply_rechecks_facts_at_run_time(database, tmp_path, facts):
    from zucai.identity_provider_only import apply_provider_only

    facts["official"].append(_official(103, "30", "10", "2026-08-15T15:00:00+00:00"))
    with pytest.raises(ValueError, match="fixture_sold_by_jc"):
        apply_provider_only(database, tmp_path, _approvals(tmp_path, [_decision()]))
    assert _teams(database) == []


def test_apply_refuses_taken_name_and_changed_receipt(database, tmp_path):
    from zucai.identity_provider_only import RECEIPTS, apply_provider_only

    with sqlite3.connect(database) as con:
        con.execute("INSERT INTO teams (name, aliases) VALUES ('别的球队', '[\"青年阿尔法\"]')")
    with pytest.raises(ValueError, match="规范队名已被占用"):
        apply_provider_only(database, tmp_path, _approvals(tmp_path, [_decision()]))
    assert not (database.parent / RECEIPTS).exists()
    decision = _decision(canonical="青年阿尔法二")
    apply_provider_only(database, tmp_path, _approvals(tmp_path, [decision]))
    before = (database.parent / RECEIPTS).read_bytes()
    changed = copy.deepcopy(decision)
    changed["canonical"] = "青年阿尔法三"
    with pytest.raises(ValueError, match="批准内容不同"):
        apply_provider_only(database, tmp_path, _approvals(tmp_path, [changed]))
    assert (database.parent / RECEIPTS).read_bytes() == before
    assert len(_teams(database)) == 2


def test_duplicate_decisions_are_refused(database, tmp_path):
    from zucai.identity_provider_only import apply_provider_only

    with pytest.raises(ValueError, match="重复"):
        apply_provider_only(database, tmp_path, _approvals(tmp_path, [_decision(), _decision()]))
    assert _teams(database) == []


def test_receipt_file_matches_feeds_export_policy():
    import re

    from zucai.cloud_feeds import STATE_FILE
    from zucai.identity_provider_only import RECEIPTS

    assert re.fullmatch(STATE_FILE, RECEIPTS)
