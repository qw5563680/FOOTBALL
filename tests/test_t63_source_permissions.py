"""Changing a route cannot turn identity/catalogue evidence into result permission."""

import json
from pathlib import Path

import pytest

from zucai.cloud_api_backfill import api_backfill_scopes
from zucai.cloud_identity_probe import identity_codes
from zucai.league_registry import load_registry, provider_config, provider_entries
from zucai.sources.api_football import APIF_INDEX_LEAGUE_IDS, ApiFootball

BRIDGES = {"39", "140", "135", "78", "61", "88", "94"}


def test_seven_result_routes_keep_permissions_closed_pending_reviewer_probe(tmp_path):
    registry = load_registry()
    for code in BRIDGES:
        entry = registry["providers"]["apif"][code]
        assert entry["mode"] == "import"
        assert entry["config"]["identity_bridge"] is True
        assert entry["config"]["require_result_permission"] is True
        assert entry["config"]["result_seasons_available"] == []
        assert entry["daily"] is False and entry["backfill"] is False
        assert code not in provider_entries("apif", purpose="daily")
        assert code not in provider_entries("apif", purpose="backfill")
    assert {int(code) for code in BRIDGES} <= APIF_INDEX_LEAGUE_IDS
    assert set(identity_codes()) == BRIDGES
    client = ApiFootball(quota_path=tmp_path / "unused-quota.json")
    with pytest.raises(ValueError, match="权限|核验"):
        client.request_url(league_id=39, season=2022)
    with pytest.raises(ValueError, match="权限|核验|仅回填"):
        api_backfill_scopes("apif", "39", 2022, 2026)


def test_identity_scope_remains_a_read_only_catalogue_operation(tmp_path):
    client = ApiFootball(identity_only=True, quota_path=tmp_path / "unused-quota.json")
    assert set(client.leagues) == BRIDGES
    assert "league=39" in client.request_url(league_id=39, season=2025)


def test_only_an_explicit_verified_result_year_unlocks_regular_replay(tmp_path):
    configs = provider_config("apif")
    configs["39"]["result_seasons_available"] = [2025]
    client = ApiFootball(leagues=configs, quota_path=tmp_path / "unused-quota.json")
    assert "season=2025" in client.request_url(league_id=39, season=2025)
    with pytest.raises(ValueError, match="权限|核验"):
        client.request_url(league_id=39, season=2024)


@pytest.mark.parametrize(
    "daily,years,expected", [(False, [2026], False), (True, [2022], False), (True, [2026], True)]
)
@pytest.mark.parametrize("explicit_date", [False, True])
def test_date_response_requires_daily_permission_and_returned_season(
    tmp_path, daily, years, expected, explicit_date
):
    doc = json.loads((Path(__file__).parents[1] / "data/fixtures/t10/live_date.json").read_bytes())
    row = doc["response"][0]
    row["league"].update(
        id=39, season=2026
    )  # Synthetic permission boundary, not provider evidence.
    doc["response"], doc["results"] = [row], 1
    configs = provider_config("apif")
    configs["39"].update(result_seasons_available=years, result_daily_verified=daily)
    client = ApiFootball(leagues=configs, quota_path=tmp_path / "unused-quota.json")
    args = {"date": "2026-11-01"} if explicit_date else {}
    result = client.parse(json.dumps(doc).encode(), **args)
    assert bool(result) is expected
    assert bool(client.rejected) is not expected
    if not expected:
        assert "权限核验" in client.rejected[0]["reason"]


def test_result_daily_permission_is_a_boolean_registry_field(tmp_path):
    doc = load_registry()
    doc["providers"]["apif"]["39"]["config"]["result_daily_verified"] = "yes"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="result_daily_verified"):
        load_registry(path)
