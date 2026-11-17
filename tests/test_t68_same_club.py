"""T68 第八节第 13 项第二阶段（提案部分）：官方比赛见证判定同一家俱乐部（合成样本，只测规则）。"""

from datetime import UTC, datetime, timedelta

import pytest

from zucai.identity_canonical import _existing_team_verdict
from zucai.identity_same_club import BASIS, MIN_OFFICIAL_MATCHES, link_decision, official_witness

START = datetime(2026, 8, 7, 16, 30, tzinfo=UTC)
CODE = "79"


def _facts(games=6, *, provider="159", official="144", formal=None, teams=None):
    official_rows, provider_rows = [], []
    for index in range(games):
        kickoff = (START + timedelta(days=7 * index)).isoformat()
        home = index % 2 == 0
        other = str(900 + index)
        official_rows.append(
            dict(
                match_id=f"jc:{index}",
                competition_id=CODE,
                kickoff_utc=kickoff,
                clock_conflict=False,
                home_source_id=official if home else other,
                away_source_id=other if home else official,
            )
        )
        provider_rows.append(
            dict(
                match_id=f"apif:{index}",
                competition_id=CODE,
                kickoff_utc=kickoff,
                complete=True,
                home=provider if home else str(7000 + index),
                away=str(7000 + index) if home else provider,
            )
        )
    stored = {
        "project-team:535": {
            "name": "English club",
            "project_team_id": "535",
            "identity_key": "project-team:535",
        }
    }
    return {
        "official": official_rows,
        "apif": provider_rows,
        "teams": stored if teams is None else teams,
        "formal": (
            {"旧联赛": {"159": {"name": "X", "canonical": "English club", "status": "approved"}}}
            if formal is None
            else formal
        ),
    }


def _team(official="144", project=None):
    return {
        "official_team_id": official,
        "name": "中文队",
        "official_names": ["中文队"],
        "official_name_evidence": [],
        "project_team_id": project,
        "category": "club/men/senior/first",
        "category_status": "approved",
    }


def _verdict(facts, team=None):
    return _existing_team_verdict(
        facts,
        {"matches": [], "results": []},
        team or _team(),
        "jc-team:144",
        configs={},
        countries={},
        index={},
        code=CODE,
        witness=official_witness(facts),
    )


def test_every_official_match_agreeing_on_one_provider_id_is_a_witness():
    entry = official_witness(_facts())["144"]
    assert entry["provider_team_id"] == "159" and entry["reasons"] == []
    assert (entry["matches"], entry["covered"]) == (6, 6)
    assert (entry["home_witnesses"], entry["away_witnesses"]) == (3, 3)
    assert [w["provider_match_id"] for w in entry["witnesses"]][:2] == ["apif:0", "apif:1"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("few", "fewer_than_min_official_matches"),
        ("exception", "no_consistent_provider_id"),
        ("uncovered", "official_match_without_provider_record"),
        ("home_only", "missing_home_or_away_witness"),
        ("twins", "several_consistent_provider_ids"),
    ],
)
def test_failed_condition_gives_no_provider_id(change, reason):
    facts = _facts(games=MIN_OFFICIAL_MATCHES - 1 if change == "few" else 6)
    if change == "exception":
        facts["apif"][2]["home"] = "160"
    if change == "uncovered":
        facts["apif"].pop()
    if change == "home_only":
        for official, provider in zip(facts["official"], facts["apif"], strict=True):
            if official["away_source_id"] == "144":
                official["home_source_id"], official["away_source_id"] = (
                    "144",
                    official["home_source_id"],
                )
                provider["home"], provider["away"] = "159", provider["home"]
    if change == "twins":
        facts["apif"] += [
            dict(row, match_id=row["match_id"] + "b", **{side: "160"})
            for row in facts["apif"]
            for side in ("home", "away")
            if row[side] == "159"
        ]
    entry = official_witness(facts)["144"]
    assert entry["provider_team_id"] is None and reason in entry["reasons"]


def test_one_provider_id_claimed_by_two_official_teams_is_rejected_for_both():
    facts = _facts()
    facts["official"] += [
        dict(
            row,
            match_id=row["match_id"] + "x",
            home_source_id="145" if row["home_source_id"] == "144" else row["home_source_id"],
            away_source_id="145" if row["away_source_id"] == "144" else row["away_source_id"],
        )
        for row in facts["official"]
    ]
    witness = official_witness(facts)
    for official in ("144", "145"):
        assert witness[official]["provider_team_id"] is None
        assert "provider_id_claimed_by_several_official_teams" in witness[official]["reasons"]


def test_rows_with_a_clock_conflict_or_incomplete_scope_are_ignored():
    facts = _facts()
    facts["official"].append(
        dict(facts["official"][0], match_id="jc:bad", clock_conflict=True, home_source_id="144")
    )
    facts["apif"].append(dict(facts["apif"][0], match_id="apif:bad", complete=False, home="999"))
    assert official_witness(facts)["144"]["provider_team_id"] == "159"


def test_witnessed_provider_id_of_a_stored_team_means_the_same_club():
    verdict = _verdict(_facts())
    assert verdict["status"] == "existing" and verdict["basis"] == BASIS
    assert verdict["project_team_id"] == "535" and verdict["provider_team_id"] == "159"
    assert verdict["witness"]["matches"] == 6


def test_witnessed_provider_id_without_an_approved_entry_never_becomes_new():
    """No approved entry: the weaker official-side witness must not open a path to 新建."""
    for formal in ({}, {"旧联赛": {"159": {"canonical": "English club", "status": "proposed"}}}):
        verdict = _verdict(_facts(formal=formal))
        assert verdict["status"] == "pending_review"
        assert verdict["basis"] == "no_unique_witnessed_provider_team"


def test_club_holding_another_provider_id_in_a_different_league_stays_pending():
    """Reviewer note on PR 185: one league approved, the witnessed id of the other is not."""
    formal = {"旧联赛": {"777": {"name": "X", "canonical": "English club", "status": "approved"}}}
    verdict = _verdict(_facts(formal=formal))
    assert verdict["status"] == "pending_review"


def test_canonical_name_carried_by_two_stored_teams_is_not_linked():
    """Reviewer note on PR 185: a normalized-name collision must not pick a team."""
    teams = {
        key: {"name": "English club", "project_team_id": key[-3:], "identity_key": key}
        for key in ("project-team:535", "project-team:536")
    }
    assert _verdict(_facts(teams=teams))["status"] == "pending_review"
    formal = {
        "旧联赛": {"159": {"name": "X", "canonical": "English club", "status": "approved"}},
        "另一联赛": {"159": {"name": "X", "canonical": "Other club", "status": "approved"}},
    }
    teams = {
        "project-team:535": {"name": "English club", "project_team_id": "535"},
        "project-team:536": {"name": "Other club", "project_team_id": "536"},
    }
    assert _verdict(_facts(formal=formal, teams=teams))["status"] == "pending_review"


def _decision(tables, team=None):
    facts = _facts()
    verdict = _verdict(facts, team)
    evidence = [dict(label="中文队", match_id="jc:0", side="home", originals=[{"path": "p"}])]
    return link_decision(
        team or _team(), verdict, tables, evidence=evidence, canonical="中文队", json_text=repr
    )


def test_link_decision_keeps_the_stored_row_and_lists_the_official_name_row_to_absorb():
    tables = {
        "teams": [
            {"id": 535, "name": "English club", "aliases": []},
            {"id": 450, "name": "中文队", "aliases": []},
        ],
        "matches": [
            {"match_id": "jc:0", "source": "jc", "home_team_id": 450, "away_team_id": 9},
            {"match_id": "fd:1", "source": "fd", "home_team_id": 535, "away_team_id": 8},
        ],
    }
    decision = _decision(tables, _team(project="450"))
    assert decision["status"] == "proposed" and decision["action"] == "link_existing_identity"
    assert (decision["project_team_id"], decision["previous_name"]) == ("535", "English club")
    assert decision["absorb_project_team_id"] == "450" and decision["blockers"] == []
    assert decision["provider_team_id"] == "159" and len(decision["witness_sha256"]) == 64
    without_row = _decision(dict(tables, teams=tables["teams"][:1]), _team())
    assert without_row["absorb_project_team_id"] is None and without_row["blockers"] == []


@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        ("aliases", "official_name_row_has_aliases_or_is_missing"),
        ("non_jc", "official_name_row_has_non_jc_matches"),
        ("meet", "both_rows_meet_in_one_match"),
    ],
)
def test_link_decision_reports_what_blocks_absorbing_the_official_name_row(change, blocker):
    tables = {
        "teams": [
            {"id": 535, "name": "English club", "aliases": []},
            {"id": 450, "name": "中文队", "aliases": ["别名"] if change == "aliases" else []},
        ],
        "matches": [
            {
                "match_id": "m:0",
                "source": "apif" if change == "non_jc" else "jc",
                "home_team_id": 450,
                "away_team_id": 535 if change == "meet" else 9,
            }
        ],
    }
    assert blocker in _decision(tables, _team(project="450"))["blockers"]
