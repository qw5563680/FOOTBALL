"""Replay cup binding from sealed offline evidence, with synthetic identities."""

import json
from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest

from test_t68_identity_archive_entry import CAPTURED, CLOCK, raw, seal, table
from test_t68_jc_kickoffs import FIXTURES, catalogue, save_json
from zucai import identity_binding_archive, jc_catalogue_kickoffs
from zucai.identity_binding import replay

CUPS = [("3", 70, "欧罗巴"), ("848", 1033103, "欧协联")]
JC_MATCH = 901001001
APIF_MATCH = 901001101
JC_HOME, JC_AWAY = 76511001, 76511002
APIF_HOME, APIF_AWAY = 86511001, 86511002
HEAD = f"state/catalogue/heads/jc-heads/{JC_MATCH}.json"


def _archive(tmp_path, cup, scenario):
    code, official_league_id, league = cup
    root = tmp_path / "archive"
    catalogue(root, [JC_MATCH], [official_league_id])
    path = "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    document = json.loads((root / path).read_bytes())
    url = json.loads((root / (path + ".meta.json")).read_bytes())["url"]
    document["value"]["matchResult"][0].update(
        homeTeamId=JC_HOME,
        awayTeamId=JC_AWAY,
        homeTeam="离线东队",
        allHomeTeam="离线东队",
        awayTeam="离线西队",
        allAwayTeam="离线西队",
    )
    raw(root, path, document, url)

    seal(root)
    present = scenario in {"stored_identity", "reversed_provider"}
    table(
        root,
        "teams",
        [dict(id=1, name="演练主队", aliases=[]), dict(id=2, name="演练客队", aliases=[])]
        if present
        else [],
    )
    table(
        root,
        "matches",
        [
            dict(
                match_id=f"jc:{JC_MATCH}",
                source="jc",
                league=league,
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=CLOCK,
                status="finished",
                result_provider="jc",
                result_checked_utc=CAPTURED,
            )
        ]
        if present
        else [],
    )
    table(
        root,
        "results",
        [dict(match_id=f"jc:{JC_MATCH}", home_goals=1, away_goals=3, available_utc=CAPTURED)]
        if present
        else [],
    )

    if scenario != "missing_clock":
        head = json.loads((FIXTURES / "t68/heads/success.json").read_bytes())
        home, away = (JC_AWAY, JC_HOME) if scenario == "reversed_head" else (JC_HOME, JC_AWAY)
        head["value"].update(
            sportteryMatchId=JC_MATCH,
            sportteryHomeTeamId=home,
            sportteryAwayTeamId=away,
            matchDateTime="2026-10-31 20:15",
            homeTeamShortName="离线东队",
            awayTeamShortName="离线西队",
            fullCourtGoal="1:3",
        )
        raw(
            root,
            HEAD,
            head,
            jc_catalogue_kickoffs.API
            + f"getMatchHeadV1.qry?source=web&sportteryMatchId={JC_MATCH}",
        )

    provider = json.loads((FIXTURES / "t68/identity/apif-page.json").read_bytes())
    provider["parameters"] = {"league": code, "season": "2026"}
    source = provider["response"][0]
    source["league"] = dict(id=int(code), season=2026)
    source["fixture"].update(
        id=APIF_MATCH, date=CLOCK, timestamp=int(datetime.fromisoformat(CLOCK).timestamp())
    )
    source["teams"] = dict(
        home=dict(id=APIF_HOME, name="Synthetic Harbor"),
        away=dict(id=APIF_AWAY, name="Synthetic Valley"),
    )
    source["goals"] = source["score"]["fulltime"] = {"home": 1, "away": 3}
    source["score"]["halftime"] = {"home": 0, "away": 0}
    if scenario == "ambiguous_window":
        other = deepcopy(source)
        newer = datetime.fromisoformat(CLOCK) + timedelta(hours=1)
        other["fixture"].update(
            id=APIF_MATCH + 1, date=newer.isoformat(), timestamp=int(newer.timestamp())
        )
        other["goals"] = other["score"]["fulltime"] = {"home": 3, "away": 1}
        provider["response"].append(other)
        provider["results"] = 2
    if scenario == "reversed_provider":
        source["teams"]["home"], source["teams"]["away"] = (
            source["teams"]["away"],
            source["teams"]["home"],
        )
    raw(
        root,
        "state/catalogue/backfill/backfill/api-football/day/fixtures_cup.json",
        provider,
        f"https://v3.football.api-sports.io/fixtures?league={code}&season=2026",
    )
    seal(root)
    return root


@pytest.mark.parametrize("cup", CUPS)
@pytest.mark.parametrize(
    "scenario, expected",
    [
        ("new_identity", "conditional_verified"),
        ("stored_identity", "strict_verified"),
        ("missing_clock", "missing_kickoff_evidence"),
        ("ambiguous_window", "not_bidirectionally_unique"),
        ("reversed_head", "conflicting_jc_kickoff_evidence"),
        ("reversed_provider", "global_provider_id_conflict"),
    ],
)
def test_cup_binding_replays_without_weakening_evidence(
    tmp_path, monkeypatch, cup, scenario, expected
):
    code, _, league = cup
    monkeypatch.setattr(jc_catalogue_kickoffs, "CATALOGUE_SOURCE_RUN", "catalogue")
    monkeypatch.setattr(
        jc_catalogue_kickoffs, "CATALOGUE_WINDOW", (date(2026, 10, 31), date(2026, 10, 31))
    )
    root = _archive(tmp_path, cup, scenario)
    aliases = tmp_path / "aliases.json"
    known = {str(APIF_HOME): {"canonical": "演练主队", "status": "approved"}}
    save_json(aliases, {league: known} if scenario == "reversed_provider" else {})
    monkeypatch.setattr(identity_binding_archive, "FORMAL", aliases)
    result = replay(root, tmp_path / "output", leagues=[code])
    candidates = result["apif"].get(league, {})
    register = result["official_register"]["rows"]
    assert {row["official_team_id"] for row in register} == {str(JC_HOME), str(JC_AWAY)}
    if expected in {"strict_verified", "conditional_verified"}:
        assert set(candidates) == {str(APIF_HOME), str(APIF_AWAY)}
        assert result["summary"]["inputs"]["jc_verified_clocks"] == 1
        for project_id, (provider_id, official_id) in enumerate(
            ((APIF_HOME, JC_HOME), (APIF_AWAY, JC_AWAY)), 1
        ):
            candidate = candidates[str(provider_id)]
            assert candidate["evidence_status"] == expected
            assert candidate["status"] == "proposed"
            assert candidate["official_team_id"] == str(official_id)
            if scenario == "stored_identity":
                assert candidate["project_team_id"] == str(project_id)
            else:
                assert candidate["project_team_id"] is None
                assert candidate["approval_gaps"] == ["canonical_identity_creation_pending"]
            witness = candidate["evidence"][0]
            assert witness["kind"] == "strict_oriented_fixture"
            assert witness["apif_candidates"] == [f"apif:{APIF_MATCH}"]
            assert witness["official_candidates"] == [f"jc:{JC_MATCH}"]
            assert witness["counterpart"]["kickoff_utc"] == CLOCK
            assert any(p.get("path") == HEAD for p in witness["counterpart"]["provenance"])
    else:
        assert not [
            candidate
            for candidate in candidates.values()
            if candidate.get("evidence_status") in {"strict_verified", "conditional_verified"}
        ]
        assert expected in {reason for item in result["unverified"] for reason in item["reasons"]}
        assert all(not row["candidate_ids"] and row["reasons"] for row in register)
    assert result["summary"]["requests"] == result["summary"]["production_writes"] == 0
