"""Default providers use reviewed registry configs; explicit offline injection remains intact."""

import json
from pathlib import Path

import pytest

from zucai.league_registry import load_registry, provider_config, provider_entries
from zucai.sources import api_football, football_data, football_data_org

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data/fixtures"


def test_default_apif_parsing_matches_explicit_legacy_config(tmp_path):
    legacy = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    raw = (FIXTURES / "t10/synthetic_season.json").read_bytes()
    default = api_football.ApiFootball(quota_path=tmp_path / "quota.json")
    explicit = api_football.ApiFootball(leagues=legacy, quota_path=tmp_path / "quota.json")
    assert {code: default.leagues[code] for code in explicit.leagues} == explicit.leagues
    assert default.parse(raw, league_id=98, season=2024) == explicit.parse(
        raw, league_id=98, season=2024
    )
    assert default.rejected == explicit.rejected
    assert "5" in default.leagues
    assert "2" not in default.leagues and "39" not in default.leagues
    default.session.close()
    explicit.session.close()


def test_default_fdorg_parsing_matches_explicit_legacy_config():
    legacy = json.loads((ROOT / "data/football_data_org/competitions.json").read_bytes())
    raw = (FIXTURES / "t09/synthetic_season.json").read_bytes()
    default = football_data_org.FootballDataOrg()
    explicit = football_data_org.FootballDataOrg(competitions=legacy)
    assert default.competitions == explicit.competitions
    assert default.parse(raw) == explicit.parse(raw)
    assert default.rejected == explicit.rejected
    default.session.close()
    explicit.session.close()


def test_default_fd_config_excludes_candidate_but_explicit_config_is_unchanged():
    legacy = json.loads((ROOT / "data/football_data_co_uk/leagues.json").read_bytes())
    default = football_data.FootballDataCoUk()
    explicit = football_data.FootballDataCoUk(leagues=legacy)
    assert default.leagues == provider_config("fd")
    assert "D2" not in default.leagues
    assert explicit.leagues is legacy and "D2" in explicit.leagues
    with pytest.raises(ValueError):
        football_data.verified_league(default.leagues, "D2")
    with pytest.raises(ValueError):
        football_data.verified_league(explicit.leagues, "D2")
    default.session.close()
    explicit.session.close()


def test_new_apif_registry_entry_reaches_parser_without_source_code_change(monkeypatch, tmp_path):
    path = FIXTURES / "t61/registry_policy.json"
    approved = provider_config("apif", path=path)
    monkeypatch.setattr(api_football, "provider_config", lambda provider: approved, raising=False)
    source = api_football.ApiFootball(quota_path=tmp_path / "quota.json")
    assert set(source.leagues) == {"1001"}
    assert "league=1001" in source.request_url(league_id=1001, season=2025)
    with pytest.raises(ValueError):
        source.request_url(league_id=1002, season=2025)
    source.session.close()


def test_new_fdorg_config_code_and_numeric_filter_are_not_hardcoded(monkeypatch):
    config = {"TEST": {"league": "Synthetic new", "earliest_season": 2024, "api_id": 990001}}
    monkeypatch.setattr(
        football_data_org, "provider_config", lambda provider: config, raising=False
    )
    source = football_data_org.FootballDataOrg()
    assert "competitions/TEST/" in source.request_url(competition="TEST", season=2024)
    payload = json.loads((FIXTURES / "t09/synthetic_season.json").read_bytes())
    payload["competition"]["code"] = "TEST"
    payload["filters"] = {
        "dateFrom": "2024-08-31",
        "dateTo": "2024-09-01",
        "competitions": "990001",
    }
    payload["matches"] = [payload["matches"][0]]
    payload["matches"][0]["competition"]["code"] = "TEST"
    payload["resultSet"]["count"] = 1
    games = source.parse(
        json.dumps(payload).encode(),
        competition="TEST",
        date_from="2024-08-31",
        date_to="2024-09-01",
    )
    assert len(games) == 1 and games[0].league == "Synthetic new"
    source.session.close()


def test_explicit_apif_fixture_dictionary_bypasses_default_registry_loading(monkeypatch, tmp_path):
    def unavailable(provider):
        raise AssertionError("Explicit fixture must not load the default registry")

    monkeypatch.setattr(api_football, "provider_config", unavailable, raising=False)
    explicit = {"99": {"league": "Synthetic explicit"}}
    source = api_football.ApiFootball(leagues=explicit, quota_path=tmp_path / "quota.json")
    assert source.leagues == explicit
    source.session.close()


def test_apif_daily_flag_filters_defaults_and_preserves_explicit_fixture_path(
    monkeypatch, tmp_path
):
    from zucai.sources import apif_command

    doc = load_registry(FIXTURES / "t61/registry_policy.json")
    doc["download_policy"]["scope"] = "all"
    doc["providers"]["apif"]["1001"]["daily"] = False
    doc["providers"]["apif"]["5"] = load_registry()["providers"]["apif"]["5"]
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc))
    approved = provider_config("apif", path=path)
    monkeypatch.setattr(apif_command, "provider_config", lambda provider: approved)
    monkeypatch.setattr(
        apif_command,
        "provider_entries",
        lambda provider, **kwargs: provider_entries(provider, path=path, **kwargs),
    )
    captured = []

    class CaptureOnly:
        def __init__(self, **kwargs):
            captured.append(kwargs["leagues"])
            raise RuntimeError("stop before network or database")

    monkeypatch.setattr(apif_command, "ApiFootball", CaptureOnly)
    common = {"date": "2024-09-16", "aliases_path": FIXTURES / "t10/synthetic_aliases.json"}
    with pytest.raises(RuntimeError, match="stop before"):
        apif_command.fetch_apif(**common)
    assert set(captured[-1]) == {"1003", "5"}
    explicit_path = tmp_path / "explicit.json"
    explicit_path.write_text(json.dumps(approved))
    with pytest.raises(RuntimeError, match="stop before"):
        apif_command.fetch_apif(leagues_path=explicit_path, **common)
    assert captured[-1] == approved


def test_fdorg_conflicting_numeric_id_fails_before_request():
    with pytest.raises(ValueError, match="数字 ID"):
        football_data_org.FootballDataOrg(
            competitions={"TEST": {"league": "Synthetic conflict", "api_id": 2021}}
        )
