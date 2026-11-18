"""T68 第八节第 13 项第二阶段（落库部分）：经批准把官方球队关联到库内既有球队（合成样本）。"""

import json
import shutil
import sqlite3
from datetime import UTC, datetime

import pytest

from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import pin_categories, seal, table
from zucai import identity_canonical
from zucai import jc_catalogue_kickoffs as kickoffs
from zucai.db import get_engine, init_db
from zucai.identity_same_club import BASIS

AUDIT = "state/audit/jc_canonical_identities.csv"
OFFICIAL = "849"


def _rows(root, name):
    columns = json.loads((root / "feed_manifest.json").read_bytes())["columns"][name]
    lines = (root / f"state/{name}.jsonl").read_text().splitlines()
    return [dict(zip(columns, json.loads(line), strict=True)) for line in lines]


@pytest.fixture
def linking(frozen_archive, tmp_path, monkeypatch):  # noqa: F811
    """Official team 849 owns stored row 1 through one JC match; row 535 is the same club."""
    pin_categories(tmp_path, monkeypatch, approved={"48"})
    # Keep the result page as the only official original of the match: the head file
    # carries no row pointer, so it could not prove the numeric team by itself.
    for path in (frozen_archive / "state/catalogue/heads/jc-heads").iterdir():
        path.unlink()
    seal(frozen_archive)
    teams = [
        dict(id=1, name="加的夫城", aliases=[]),
        dict(id=2, name="切尔西", aliases=[]),
        dict(id=535, name="English club", aliases=[]),
    ]
    table(frozen_archive, "teams", teams)
    # An older archived database snapshot still names row 1 for this match.
    rows, _, proof = kickoffs._catalogue(frozen_archive)
    clocks, issues = kickoffs._database_evidence(frozen_archive, rows, proof)
    assert not issues
    kickoffs._preserve_evidence(
        clocks[2042404][0], rows[0], frozen_archive, frozen_archive, frozen_archive / "unused"
    )
    seal(frozen_archive)
    db = tmp_path / "copy/data/feeds.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(db) as con:
        for name in ("teams", "matches", "results"):
            for row in _rows(frozen_archive, name):
                row = {k: json.dumps(v) if isinstance(v, list) else v for k, v in row.items()}
                row = {k: v for k, v in row.items() if v is not None}
                con.execute(
                    f"INSERT INTO {name} ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                    list(row.values()),
                )
    shutil.copytree(frozen_archive / "state", db.parent / "state")
    original = identity_canonical._existing_team_verdict
    state = dict(same_club=True)

    def verdict(facts, tables, team, key, **kwargs):
        if state["same_club"] and team.get("official_team_id") == OFFICIAL:
            witnesses = [
                dict(match_id="jc:1", provider_match_id="apif:1", kickoff_utc="t1", side="home"),
                dict(match_id="jc:2", provider_match_id="apif:2", kickoff_utc="t2", side="away"),
            ]
            return dict(
                status="existing",
                basis=BASIS,
                project_team_id="535",
                provider_team_id="159",
                canonical="English club",
                witness=dict(
                    matches=2, covered=2, home_witnesses=1, away_witnesses=1, witnesses=witnesses
                ),
            )
        return original(facts, tables, team, key, **kwargs)

    monkeypatch.setattr(identity_canonical, "_existing_team_verdict", verdict)
    return frozen_archive, db, state


def approved_link(archive):
    proposal = identity_canonical.propose(archive, codes=["48"])
    (item,) = proposal["link_decisions"]
    assert item["blockers"] == [] and item["absorb_project_team_id"] == "1"
    item.update(
        status="approved",
        approved_by="qw5563680",
        approved_utc=datetime.now(UTC).isoformat(),
        approval_pr="https://github.com/qw5563680/FOOTBALL/pull/186",
        review_url="https://github.com/qw5563680/FOOTBALL/pull/186#pullrequestreview-1",
    )
    document = dict(proposal, decisions=[], link_decisions=[item])
    path = archive.parent / "approvals.json"
    path.write_text(json.dumps(document))
    return path, item


def _export(archive, db):
    """What export does for the tables and the receipt this change touches."""
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        teams = [dict(r) for r in con.execute("SELECT * FROM teams ORDER BY id")]
        matches = [dict(r) for r in con.execute("SELECT * FROM matches")]
    table(archive, "teams", teams)
    table(archive, "matches", matches)
    dest = archive / AUDIT
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes((db.parent / AUDIT).read_bytes())
    seal(archive)
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    manifest["created_utc"] = datetime.now(UTC).isoformat()
    (archive / "feed_manifest.json").write_text(json.dumps(manifest))
    return teams, matches


def test_link_moves_matches_renames_the_kept_row_and_is_read_back(linking):
    from zucai.identity_binding_archive import load_archive

    archive, db, _ = linking
    path, item = approved_link(archive)
    result = identity_canonical.apply_approvals(db, archive, path)
    assert (result["created"], result["linked"], result["matches_changed"]) == (0, 1, 1)
    assert result["references_moved"] == {"matches.home_team_id": 1}
    teams, matches = _export(archive, db)
    by_id = {row["id"]: row for row in teams}
    assert 1 not in by_id and len(teams) == 2
    assert by_id[535]["name"] == item["canonical"]
    assert json.loads(by_id[535]["aliases"]) == ["English club", "jc:team:" + OFFICIAL]
    assert [(m["home_team_id"], m["away_team_id"]) for m in matches] == [(535, 2)]
    # The archived snapshot still says row 1; the authenticated receipt explains it.
    loaded = load_archive(archive, leagues=["48"])
    team = loaded["teams"]["jc-team:" + OFFICIAL]
    assert (team["identity_status"], team["project_team_id"]) == ("existing_numbered", "535")
    assert team["project_identity_evidence"][-1]["kind"] == "approved_same_club_link"
    again = identity_canonical.apply_approvals(db, archive, path)
    assert (again["linked"], again["already_applied"]) == (0, 1)


def test_without_the_receipt_an_old_snapshot_naming_the_absorbed_row_still_blocks(linking):
    from zucai.identity_binding_archive import load_archive

    archive, db, _ = linking
    path, _ = approved_link(archive)
    identity_canonical.apply_approvals(db, archive, path)
    _export(archive, db)
    (archive / AUDIT).unlink()
    seal(archive)
    loaded = load_archive(archive, leagues=["48"])
    assert loaded["teams"]["jc-team:" + OFFICIAL]["identity_status"] == "conflict"


def test_receipt_naming_a_row_that_still_exists_is_rejected_on_read(linking):
    from zucai.identity_binding_archive import load_archive

    archive, db, _ = linking
    path, _ = approved_link(archive)
    identity_canonical.apply_approvals(db, archive, path)
    teams, _ = _export(archive, db)
    table(archive, "teams", teams + [dict(id=1, name="Back again", aliases="[]")])
    with pytest.raises(ValueError, match="Same-club receipt"):
        load_archive(archive, leagues=["48"])


REJECTIONS = {
    "proposed": "explicit approved decision",
    "in_creation_list": "explicit approved decision",
    "verdict_changed": "pending_review",
    "other_target": "批准内容与当前事实不一致",
    "witness_tampered": "witness digest invalid",
    "witness_not_current": "not a subset of current witness",
    "absorbed_row_has_alias": "official_name_row_has_aliases_or_is_missing",
    "name_owned_by_third_row": "already has an owner",
}


@pytest.mark.parametrize("attack", sorted(REJECTIONS))
def test_link_rejects_anything_that_differs_from_current_facts_without_writes(linking, attack):
    archive, db, state = linking
    path, item = approved_link(archive)
    document = json.loads(path.read_bytes())
    row = document["link_decisions"][0]
    if attack == "proposed":
        row["status"] = "proposed"
    if attack == "in_creation_list":
        document.update(decisions=[row], link_decisions=[])
    if attack == "verdict_changed":
        state["same_club"] = False
    if attack == "other_target":
        row["project_team_id"] = "2"
    if attack == "witness_tampered":
        row["witness"]["matches"] = 99
    if attack == "witness_not_current":
        row["witness"]["witnesses"][0]["provider_match_id"] = "apif:9"
        row["witness_sha256"] = identity_canonical.sha256(
            identity_canonical._json(row["witness"]).encode()
        ).hexdigest()
    if attack in {"absorbed_row_has_alias", "name_owned_by_third_row"}:
        teams = _rows(archive, "teams")
        for team in teams:
            if attack == "absorbed_row_has_alias" and team["id"] == 1:
                team["aliases"] = ["别名"]
            if attack == "name_owned_by_third_row" and team["id"] == 2:
                team["aliases"] = [item["canonical"]]
        table(archive, "teams", teams)
        with sqlite3.connect(db) as con:
            for team in teams:
                con.execute(
                    "UPDATE teams SET aliases=? WHERE id=?",
                    (json.dumps(team["aliases"]), team["id"]),
                )
    path.write_text(json.dumps(document))
    before = db.read_bytes()
    with pytest.raises(ValueError) as caught:
        identity_canonical.apply_approvals(db, archive, path)
    assert REJECTIONS[attack] in str(caught.value)
    assert db.read_bytes() == before
    assert not (db.parent / AUDIT).exists()
