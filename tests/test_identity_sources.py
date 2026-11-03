"""Identity-only fixtures must never enter the collection/import path."""

import json
from pathlib import Path

import pytest
import responses

from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_command import fetch_apif

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t23/identity.json").read_bytes())
BRIDGES = {row["code"] for row in FIXTURE["leagues"]}


def test_bridge_config_is_pending_and_excluded_from_default_source():
    config = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    assert BRIDGES <= config.keys()
    for code in BRIDGES:
        assert config[code]["identity_only"] is True
        assert config[code]["verification_status"] == "needs_verification"
        assert config[code]["verified_utc"] is None
        assert config[code]["seasons_available"] == []
    source = ApiFootball()
    try:
        assert not BRIDGES & source.leagues.keys()
    finally:
        source.session.close()


def test_identity_source_explicitly_selects_only_the_seven_bridges():
    source = ApiFootball(identity_only=True)
    try:
        assert source.leagues.keys() == BRIDGES
    finally:
        source.session.close()


def test_identity_fixture_is_excluded_from_date_parse():
    doc = json.loads((ROOT / "data/fixtures/t14/apif.json").read_bytes())
    doc["response"][0]["league"]["id"] = 39
    source = ApiFootball()
    try:
        assert source.parse(json.dumps(doc).encode(), date=doc["parameters"]["date"]) == []
    finally:
        source.session.close()


def test_explicit_identity_import_is_rejected_before_network_or_database(tmp_path):
    from datetime import timedelta

    with responses.RequestsMock() as http, pytest.raises(ValueError):
        fetch_apif(league_id="39", seasons=[2025], assume=timedelta(hours=3), db=tmp_path / "x.db")
    assert not http.calls and not (tmp_path / "x.db").exists()


def test_backfill_rejects_identity_even_if_permissions_are_present(tmp_path, monkeypatch):
    from zucai.cloud_api_backfill import api_backfill_scopes

    config = json.loads((ROOT / "data/api_football/leagues.json").read_bytes())
    config["39"] = {"league": "英超", "identity_only": True, "seasons_available": [2025, 2026]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr("zucai.cloud_api_backfill.LEAGUES", path)
    with pytest.raises(ValueError):
        api_backfill_scopes("apif", "39", "2025", "2026")
