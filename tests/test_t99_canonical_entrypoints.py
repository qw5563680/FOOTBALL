"""Exercise T99 through archived country evidence, plan and approved database apply."""

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime

import pytest

import test_t95_cup_team_flow as cup
from test_t68_identity_archive_entry import pin_categories, seal
from test_t68_jc_kickoffs import save_json
from test_t99_country_evidence import PROVENANCE, seal_country_crops
from zucai import cloud_feeds, identity_canonical
from zucai import jc_catalogue_kickoffs as kickoffs
from zucai.db import get_engine, init_db

COUNTRIES = PROVENANCE["probe_index_path"].rsplit("/", 1)[0]
CANDIDATE = str(cup.JC_TEAM_IDS[0])
PROVIDER_ID = cup.PROVIDER_IDS[0]


@pytest.fixture
def cup_archive(tmp_path, monkeypatch):
    # Surrounding fixture identity evidence is a controlled hypothetical; /teams crops above
    # are authentic and checked against the original hashes before editing any negative case.
    pin_categories(tmp_path, monkeypatch, approved={"3"})
    monkeypatch.setattr(kickoffs, "CATALOGUE_SOURCE_RUN", "catalogue")
    monkeypatch.setattr(kickoffs, "CATALOGUE_WINDOW", (cup.DAY, cup.DAY))
    db = tmp_path / "seed" / "feeds.db"
    engine = get_engine(db)
    try:
        init_db(engine)
    finally:
        engine.dispose()
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO teams VALUES(?,?,'[]')",
            [(1, "合成异国库内甲"), (2, "合成异国库内乙")],
        )
        connection.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('fd:unrelated','fd','西甲',1,2,?,'scheduled')",
            ("2026-10-31T02:15:00+00:00",),
        )
    fixtures = [(994001, "2026-10-31T02:15:00+00:00", 0, 1)]
    cup._season_original(db.parent, "3", fixtures)
    official_ids = cup._official_results(db.parent, 70, fixtures)
    cup._official_heads(db.parent, official_ids, fixtures)
    archive = tmp_path / "archive"
    cloud_feeds.export_feeds(db, archive, cup.DAY, "t99-entrypoint")
    seal_country_crops(archive)
    restored = tmp_path / "restored" / "feeds.db"
    cloud_feeds.restore_feeds(archive, restored)
    return archive, restored


def _approved(proposal, path):
    decision = next(row for row in proposal["decisions"] if row["official_team_id"] == CANDIDATE)
    assert not decision["blockers"]
    approval_pr = "https://github.com/qw5563680/FOOTBALL/pull/999999999"
    document = {
        **proposal,
        "decisions": [
            dict(
                decision,
                status="approved",
                approved_by="qw5563680",
                approved_utc=datetime.now(UTC).isoformat(),
                approval_pr=approval_pr,
                review_url=approval_pr + "#synthetic-review",
            )
        ],
    }
    save_json(path, document)
    return path


def test_plan_and_apply_use_the_same_verified_team_country(cup_archive, tmp_path):
    archive, database = cup_archive
    path = tmp_path / "plan.json"
    identity_canonical.main(["plan", "--archive", str(archive), "--codes", "3", "--out", str(path)])
    proposal = json.loads(path.read_bytes())
    verdict = proposal["existing_team_verdicts"][CANDIDATE]
    assert verdict["status"] == "new"
    assert verdict["provider_team_id"] == str(PROVIDER_ID)
    assert verdict["blocking_teams"] == []
    proof = verdict["provider_team_country"]
    assert proof["country"] == "Scotland"
    for evidence in proof["evidence"]:
        assert (
            evidence["sha256"]
            == hashlib.sha256((archive / evidence["path"]).read_bytes()).hexdigest()
        )
    assert {row["path"] for row in proof["evidence"]} == {
        f"{COUNTRIES}/3.json",
        f"{COUNTRIES}/848.json",
    }
    approvals = _approved(proposal, tmp_path / "approvals.json")
    assert identity_canonical.apply_approvals(database, archive, approvals)["created"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM teams ORDER BY id").fetchall() == [
            ("合成异国库内甲",),
            ("合成异国库内乙",),
            (cup.TEAM_LABELS[0],),
        ]
        assert connection.execute("SELECT match_id FROM matches").fetchall() == [("fd:unrelated",)]


@pytest.mark.parametrize("change", ["missing", "empty", "conflict"])
def test_apply_revalidates_current_country_before_writing_any_identity(
    cup_archive, tmp_path, change
):
    archive, database = cup_archive
    proposal = identity_canonical.propose(archive, codes=["3"])
    assert proposal["existing_team_verdicts"][CANDIDATE]["status"] == "new"
    approvals = _approved(proposal, tmp_path / "approvals.json")
    if change == "missing":
        shutil.rmtree(archive / COUNTRIES)
        seal(archive)
    else:

        def edit(code, body, _meta, _item):
            if change == "empty" or code == 848:
                team = next(r["team"] for r in body["response"] if r["team"]["id"] == PROVIDER_ID)
                team["country"] = "" if change == "empty" else "Denmark"

        seal_country_crops(archive, edit=edit)
    before = database.read_bytes()
    current = identity_canonical.propose(archive, codes=["3"])
    verdict = current["existing_team_verdicts"][CANDIDATE]
    assert verdict["status"] == "pending_review"
    assert verdict["basis"] == "provider_team_country_unverified"
    assert verdict["provider_team_country"]["country"] is None
    with pytest.raises(ValueError, match="创建被拒绝.*provider_team_country_unverified"):
        identity_canonical.apply_approvals(database, archive, approvals)
    assert database.read_bytes() == before
    assert not (database.parent / identity_canonical.RECEIPTS).exists()
