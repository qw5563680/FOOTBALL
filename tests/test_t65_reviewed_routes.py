"""Reviewed identities use distinct project league keys before any import or schedule."""

import copy
import json
from pathlib import Path

import pytest

from zucai.league_registry import daily_codes, load_registry, provider_entries
from zucai.sources.api_football import ApiFootball

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t61/registry_policy.json"
APPROVED_IDS = frozenset(
    "79 89 99 307 41 235 80 95 114 104 293 45 81 48 101 137 66 143 102 90 46 "
    "73 294 96 547 556 529 528 105 526 115 17 13 15 18 531 1168 16 32 30 34 37 "
    "10 1 6 4 7 9 22 960 1222 23 480 532 524 490 38 897 803 965 1245 1188".split()
)
UNREVIEWED_IDS = frozenset("40 62 71 850 916".split())


def save(tmp_path, document):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.parametrize("provider", ["apif", "fdorg", "fd"])
@pytest.mark.parametrize("status", ["candidate", "verified"])
def test_duplicate_import_league_key_is_rejected_before_activation(tmp_path, provider, status):
    document = load_registry(FIXTURE)
    template = document["providers"]["apif"]["1001"]
    first, second = ("2001", "2002") if provider == "apif" else ("AA", "BB")
    for code in (first, second):
        entry = copy.deepcopy(template)
        entry.update(status=status, daily=status == "verified", backfill=status == "verified")
        config = {"league": "同名杯赛"}
        if provider == "apif":
            config["seasons_available"] = []
        elif provider == "fdorg":
            config["earliest_season"] = 2024
        else:
            config.update(div_code=code, status=status)
        entry["config"] = config
        document["providers"][provider][code] = entry
    with pytest.raises(ValueError, match="duplicate.*league"):
        load_registry(save(tmp_path, document))


def test_same_league_key_is_allowed_across_providers_and_for_identity_only(tmp_path):
    document = load_registry(FIXTURE)
    entry = copy.deepcopy(document["providers"]["apif"]["1001"])
    entry["config"] = {"league": "Synthetic one", "earliest_season": 2024}
    document["providers"]["fdorg"]["AA"] = entry
    identity = copy.deepcopy(document["providers"]["apif"]["1001"])
    identity.update(mode="identity_only", daily=False, backfill=False)
    identity["config"]["identity_only"] = True
    document["providers"]["apif"]["2001"] = identity
    assert load_registry(save(tmp_path, document)) == document


def test_reviewed_ids_enter_daily_and_backfill_without_inventing_access():
    registry = load_registry()
    legacy = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    assert len(APPROVED_IDS) == 62
    for purpose in ("daily", "backfill"):
        routes = provider_entries("apif", purpose=purpose)
        assert routes.keys() - legacy.keys() == APPROVED_IDS
        assert not UNREVIEWED_IDS & routes.keys()
    assert APPROVED_IDS <= set(daily_codes("apif"))
    for code in APPROVED_IDS:
        route = registry["providers"]["apif"][code]
        assert route["access_status"] == "unverified"
        assert route["config"]["seasons_available"] == []
        assert route["config"]["catalogue_seasons"]
    for code in UNREVIEWED_IDS:
        route = registry["providers"]["apif"][code]
        assert route["status"] == "candidate"
        assert route["daily"] is False and route["backfill"] is False


@pytest.mark.parametrize(
    ("league_id", "project_name"),
    [(45, "英足总杯"), (294, "韩国杯"), (547, "意超杯"), (556, "西超杯"), (529, "德超杯")],
)
def test_same_english_api_names_parse_to_distinct_project_leagues(
    tmp_path, league_id, project_name
):
    payload = json.loads((ROOT / "data/fixtures/t10/synthetic_season.json").read_bytes())
    payload["parameters"]["league"] = str(league_id)
    for row in payload["response"]:
        row["league"]["id"] = league_id
    source = ApiFootball(quota_path=tmp_path / "quota.json")
    try:
        games = source.parse(json.dumps(payload).encode(), league_id=league_id, season=2024)
        assert games and {game.league for game in games} == {project_name}
    finally:
        source.session.close()
