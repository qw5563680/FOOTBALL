"""Registry approval, scope and exact legacy compatibility; no network access."""

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest

from zucai.league_registry import (
    REGISTRY_PATH,
    daily_codes,
    load_registry,
    provider_config,
    provider_entries,
    provider_ids,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t61/registry_policy.json"
MASTER = ROOT / "docs/research/2026-11-14-jc-sfc-competition-master-table.csv"
LEGACY = {
    "apif": ROOT / "data/api_football/leagues.json",
    "fdorg": ROOT / "data/football_data_org/competitions.json",
    "fd": ROOT / "data/football_data_co_uk/leagues.json",
}


def save(tmp_path, doc):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def test_master_rows_preserve_every_csv_field_and_ambiguous_source_name():
    registry = load_registry()
    with MASTER.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(registry["competitions"]) == len(rows) == 90
    assert registry["master_table"]["sha256"] == hashlib.sha256(MASTER.read_bytes()).hexdigest()
    assert [row["master_fields"] for row in registry["competitions"]] == rows
    assert [row["master_row_id"] for row in registry["competitions"]] == [
        f"master-{i:03}" for i in range(1, 91)
    ]
    j1 = [row for row in registry["competitions"] if "日职" in row["source_names"]]
    assert [row["master_name"] for row in j1] == ["日职（J1）", "日本J1百年构想联赛"]
    assert j1[0]["status"] == "verified"
    assert j1[1]["status"] == "candidate"
    assert all(mapping["status"] == "candidate" for mapping in j1[1]["mappings"])
    assert j1[1]["official_catalogue"]["owner_decision_required"]
    assert j1[1]["api_catalogue_proposal"]["status"] == "candidate"
    assert "百年构想" in j1[1]["missing_reason"]


def test_legacy_provider_values_and_modes_remain_exact():
    registry = load_registry()
    for provider, path in LEGACY.items():
        original = json.loads(path.read_bytes())
        routes = registry["providers"][provider]
        assert {k: routes[k]["config"] for k in original} == original
        expected = {k: v for k, v in original.items() if provider != "fd" or k != "D2"}
        actual = provider_config(provider)
        assert {code: actual[code] for code in expected} == expected
        if provider != "apif":
            assert actual == expected
    assert {3, 848, 98, 292, 188, 113, 103, 244, 119, 253, 169} <= provider_ids("apif")
    assert provider_ids("apif", "identity_only") == {39, 140, 135, 78, 61, 88, 94}
    assert provider_ids("apif", "index_only") == {2}
    assert provider_ids("apif", "nations") == {5}
    assert registry["providers"]["apif"]["2"]["access_status"] == "unverified"
    assert registry["providers"]["apif"]["2"]["verified_scope"] == "legacy_index_route_only"
    assert "D2" not in daily_codes("fd")
    assert registry["providers"]["fd"]["D2"]["status"] == "candidate"


def test_purpose_preserves_legacy_order_and_special_nations_route():
    assert daily_codes("fdorg") == "PL ELC PD SA BL1 FL1 DED PPL BSA CL".split()
    assert daily_codes("apif") == "98 292 188 113 103 244 119 253 169 3 848".split()
    assert set(provider_entries("apif", purpose="backfill")) == {
        str(code) for code in provider_ids("apif")
    }
    for purpose in ("daily", "backfill"):
        assert "5" not in provider_entries("apif", purpose=purpose)


def test_all_scope_cannot_approve_a_candidate(tmp_path):
    doc = load_registry(FIXTURE)
    assert list(provider_entries("apif", path=FIXTURE)) == ["1001"]
    doc["download_policy"]["scope"] = "all"
    path = save(tmp_path, doc)
    assert list(provider_entries("apif", path=path)) == ["1001", "1003"]
    assert daily_codes("apif", path=path) == ["1001", "1003"]
    assert "1002" not in provider_config("apif", path=path)


def test_new_reviewed_league_works_by_config_alone(tmp_path):
    doc = load_registry(FIXTURE)
    new = copy.deepcopy(doc["providers"]["apif"]["1001"])
    new.update(groups=["B"], config={"league": "Synthetic new", "seasons_available": [2024]})
    doc["providers"]["apif"]["1004"] = new
    path = save(tmp_path, doc)
    assert provider_ids("apif", path=path) == {1001, 1004}
    assert daily_codes("apif", path=path) == ["1001", "1004"]
    assert provider_config("apif", path=path)["1004"] == new["config"]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(version=True),
        lambda d: d["download_policy"].update(scope="guess"),
        lambda d: d["download_policy"].update(max_requests=-1),
        lambda d: d["providers"]["apif"]["1001"].update(status="approved"),
        lambda d: d["providers"]["apif"]["1001"].update(status=[]),
        lambda d: d["providers"]["apif"]["1001"].update(mode="index"),
        lambda d: d["providers"]["apif"]["1001"].update(evidence=[]),
        lambda d: d["providers"]["apif"]["1001"].update(groups=["X"]),
        lambda d: d["providers"]["apif"]["1001"].update(daily="true"),
        lambda d: d["providers"]["apif"]["1001"]["config"].update(identity_only=True),
        lambda d: d["providers"]["apif"]["1001"]["config"].update(catalogue_seasons=[2025, 2025]),
        lambda d: d["providers"]["apif"]["1001"]["config"].update(api_id=True),
        lambda d: d["providers"]["apif"].update({"01001": d["providers"]["apif"]["1001"]}),
        lambda d: d["competitions"][0]["mappings"][0].update(code="9999"),
        lambda d: d["competitions"][0]["mappings"][0].update(code="1002"),
        lambda d: d["competitions"][0]["mappings"].append(d["competitions"][0]["mappings"][0]),
        lambda d: d["competitions"].append(d["competitions"][0]),
        lambda d: d["competitions"][0].update(source_names=["Guessed name"]),
        lambda d: d["master_table"].update(row_count=2),
        lambda d: d["providers"]["apif"]["1001"].update(backfil=True),
    ],
)
def test_invalid_registry_fails_closed(tmp_path, change):
    doc = load_registry(FIXTURE)
    change(doc)
    with pytest.raises(ValueError):
        load_registry(save(tmp_path, doc))


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(FIXTURE.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(ValueError, match="duplicate"):
        load_registry(path)


def test_unknown_provider_mode_and_purpose_are_errors():
    with pytest.raises(ValueError):
        provider_entries("guess", path=FIXTURE)
    with pytest.raises(ValueError):
        provider_entries("apif", path=FIXTURE, modes=["guess"])
    with pytest.raises(ValueError):
        provider_entries("apif", path=FIXTURE, purpose="guess")
    with pytest.raises(ValueError):
        provider_ids("fdorg")
    assert REGISTRY_PATH == ROOT / "data/league_map.json"
