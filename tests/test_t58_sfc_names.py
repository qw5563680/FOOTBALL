"""Exact SFC identities replayed from the immutable 26164 feed snapshot."""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from zucai.cloud_feeds import TABLES
from zucai.db import get_engine, init_db
from zucai.models import Match, SfcIssue, Team
from zucai.sources.sfc import refresh_snapshot

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t58"
EXPECTED = {2: "apif:1528973", 6: "apif:1528977", 10: "apif:1528981"}


@pytest.fixture
def real_26164(tmp_path):
    engine = get_engine(tmp_path / "t58.db")
    init_db(engine)
    with Session(engine) as session:
        for table, cls in (("teams", Team), ("matches", Match)):
            for line in (FIXTURE / f"{table}.jsonl").read_text().splitlines():
                row = dict(zip(TABLES[table], json.loads(line), strict=True))
                if table == "teams":
                    row["aliases"] = json.loads(row["aliases"])
                session.add(cls(**row))
            session.flush()
        issue = SfcIssue(**json.loads((FIXTURE / "26164-state.json").read_text())["issue"])
        session.add(issue)
        session.flush()
        yield session, issue
    engine.dispose()


def test_exact_three_gaps_close_and_other_eleven_rows_remain_identical(real_26164):
    session, issue = real_26164
    original = copy.deepcopy(issue.snapshot)
    actual = refresh_snapshot(session, issue)
    for before, after in zip(original, actual, strict=True):
        if before["seq"] in EXPECTED:
            assert before["mapping"] == "unmatched"
            assert after["mapping"] == "matched"
            assert after["match_id"] == EXPECTED[before["seq"]]
        else:
            assert after == before
    assert issue.snapshot == original
    assert refresh_snapshot(session, issue) == actual


@pytest.mark.parametrize("wrong", ["orientation", "clock", "unknown_id", "unknown_full_name_id"])
def test_wrong_identity_still_rejected(real_26164, wrong):
    session, issue = real_26164
    rows = copy.deepcopy(issue.snapshot)
    row = rows[1]
    if wrong == "orientation":
        for home, away in (("home", "away"), ("provider_home_id", "provider_away_id")):
            row[home], row[away] = row[away], row[home]
    elif wrong == "clock":
        row["source_kickoff_utc"] = (
            datetime.fromisoformat(row["source_kickoff_utc"]) + timedelta(minutes=1)
        ).isoformat()
    else:
        row["provider_home_id"] = "99999999"
        if wrong == "unknown_full_name_id":
            row["home"] = "格鲁吉亚"
    issue.snapshot = rows
    actual = refresh_snapshot(session, issue)[1]
    assert actual["match_id"] is None
    assert actual["mapping"] != "matched"


def test_fixture_bytes_and_each_approved_entry_have_source_evidence():
    from zucai.nations import identities

    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name, meta in provenance["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == meta["sha256"]
    aliases = json.loads((FIXTURE.parents[1] / "team_aliases/sfc.json").read_text())["欧国联"]
    snapshot = json.loads((FIXTURE / "26164-state.json").read_text())["issue"]["snapshot"]
    games = {
        row[0]: row
        for line in (FIXTURE / "matches.jsonl").read_text().splitlines()
        if (row := json.loads(line))
    }
    teams = {
        row[0]: row
        for line in (FIXTURE / "teams.jsonl").read_text().splitlines()
        if (row := json.loads(line))
    }
    for key, entry in aliases.items():
        assert entry["status"] == "approved"
        assert identities()["teams"][str(entry["apif_id"])]["canonical"] == entry["canonical"]
        assert entry["evidence"]
        for proof in entry["evidence"]:
            assert proof["archive_commit"] == provenance["archive_commit"]
            source = next(r for r in snapshot if r["fixture_id"] == proof["sfc_fixture_id"])
            side = proof["side"]
            assert source["provider_" + side + "_id"] == key
            assert source[side] == entry["label"]
            game = games[proof["provider_match_id"]]
            assert source["league"] == game[2] == "欧国联"
            assert datetime.fromisoformat(source["source_kickoff_utc"]) == datetime.fromisoformat(
                game[5]
            )
            team = teams[game[3 if side == "home" else 4]]
            assert team[1] == entry["canonical"]
            assert f"apif:欧国联:team:{entry['apif_id']}" in json.loads(team[2])


@pytest.mark.parametrize("changed", [{"status": "proposed"}, {"scope": "club"}, {"evidence": []}])
def test_unapproved_or_wrong_scope_alias_cannot_match(real_26164, monkeypatch, changed):
    from zucai import nations

    aliases = copy.deepcopy(nations.sfc_aliases())
    aliases["欧国联"]["77"].update(changed)
    monkeypatch.setattr(nations, "sfc_aliases", lambda: aliases)
    session, issue = real_26164
    row = refresh_snapshot(session, issue)[1]
    assert row["match_id"] is None
    assert row["mapping_evidence"]["basis"] == "national_team_name_missing"


@pytest.mark.parametrize("changed", [{"canonical": "匈牙利"}, {"apif_id": 769}, {"label": "错名"}])
def test_conflicting_approved_alias_does_not_override_identity(real_26164, monkeypatch, changed):
    from zucai import nations

    aliases = copy.deepcopy(nations.sfc_aliases())
    aliases["欧国联"]["77"].update(changed)
    monkeypatch.setattr(nations, "sfc_aliases", lambda: aliases)
    session, issue = real_26164
    row = refresh_snapshot(session, issue)[1]
    assert row["match_id"] is None
    assert row["mapping_evidence"]["basis"] == "conflicting_identity"


def test_national_alias_cannot_expand_in_another_competition(real_26164):
    session, issue = real_26164
    rows = copy.deepcopy(issue.snapshot)
    rows[1]["league"] = "友谊赛"
    issue.snapshot = rows
    assert refresh_snapshot(session, issue)[1]["match_id"] is None
