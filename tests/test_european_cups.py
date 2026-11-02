"""Verified cup scope and regulation-time handling use only offline captures."""

import json
from pathlib import Path

import pytest

from zucai.sources.api_football import ApiFootball
from zucai.sources.football_data_org import FootballDataOrg

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t20"


def test_cl_configuration_numeric_filter_and_extra_time_regulation_scores():
    source = FootballDataOrg()
    doc = json.loads((FIXTURES / "fdorg_cl_2023.json").read_bytes())
    games = source.parse(json.dumps(doc).encode(), competition="CL", season=2023)
    assert games and all(g.league == "欧冠" for g in games)
    assert source.competitions["CL"]["earliest_season"] == 2023
    overtime = [r for r in doc["matches"] if r["score"]["duration"] != "REGULAR"]
    assert overtime
    for row in overtime:
        game = next(g for g in games if g.provider_match_id == row["id"])
        regular = row["score"]["regularTime"]
        assert game.full == (regular["home"], regular["away"])
    doc["filters"]["competitions"] = "2001"
    assert source.parse(json.dumps(doc).encode(), competition="CL", season=2023) == games
    source.session.close()


@pytest.mark.parametrize(("code", "name"), [("3", "欧罗巴"), ("848", "欧协联")])
def test_apif_cup_season_and_unfiltered_date_response(code, name):
    source = ApiFootball()
    doc = json.loads((FIXTURES / f"apif_{code}_2024.json").read_bytes())
    games = source.parse(json.dumps(doc).encode(), league_id=code, season=2024)
    assert games and all(g.league == name for g in games)
    assert source.leagues[code]["seasons_available"] == [2022, 2023, 2024]
    for row in doc["response"]:
        if row["fixture"]["status"]["short"] in {"FT", "AET", "PEN"}:
            game = next(g for g in games if g.provider_match_id == row["fixture"]["id"])
            full = row["score"]["fulltime"]
            assert game.full == (full["home"], full["away"])
    day = games[0].kickoff_utc.date().isoformat()
    doc["parameters"] = {"date": day}
    doc["response"] = [doc["response"][0]]
    doc["results"] = 1
    assert source.parse(json.dumps(doc).encode(), date=day)[0].league == name
    source.session.close()


def test_apif_denied_season_is_an_error_and_cl_is_not_configured():
    source = ApiFootball()
    with pytest.raises(ValueError, match="API 错误"):
        source.parse((FIXTURES / "apif_plan_2026.json").read_bytes(), league_id="3", season=2026)
    assert "2" not in source.leagues
    source.session.close()


def test_cl_joins_same_daily_window_and_uses_july_season():
    from datetime import UTC, datetime

    from zucai.cloud_apis import COMPETITION_CODES
    from zucai.predict.seasons import season_for
    from zucai.sources.fdorg_command import _requests

    source = FootballDataOrg()
    assert "CL" in COMPETITION_CODES.split(",")
    scopes = _requests(
        COMPETITION_CODES,
        None,
        3,
        None,
        False,
        datetime(2026, 11, 3, tzinfo=UTC).date(),
        source.competitions,
    )
    assert len(scopes) == 1
    assert scopes[0]["competition"].split(",").count("CL") == 1
    assert season_for(datetime(2026, 6, 30, tzinfo=UTC), "欧冠") == "2526"
    assert season_for(datetime(2026, 7, 1, tzinfo=UTC), "欧冠") == "2627"
    source.session.close()
