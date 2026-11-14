"""Section 7(1): independently prove legacy results without scoped anchors."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from test_cup_identity import identity_inputs
from test_t68_scope_coexistence import scoped_copy, without_alias_file_digest
from zucai.sources.cup_identity import _bridge, resolve_cup_aliases


@pytest.mark.parametrize("league,code", [("欧罗巴", "3"), ("欧协联", "848")])
@pytest.mark.parametrize("at_target", [False, True])
def test_database_only_binding_ignores_scoped_addition(tmp_path, league, code, at_target):
    db, _, capture, aliases = identity_inputs(tmp_path, "apif", official=False)
    capture["scope"]["league_id"] = code
    capture["games"] = [replace(g, league=league) for g in capture["games"]]
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO teams VALUES(1,?,?)",
            ("合成甲", json.dumps([f"apif:{league}:team:9101"])),
        )
    baseline = resolve_cup_aliases("apif", db, {}, [capture], tmp_path / "baseline")
    target = league if at_target else "新联赛"
    scoped = {target: {"9101": scoped_copy(aliases["合成国内联赛"]["9101"], target)}}
    original = deepcopy(scoped)
    before_db = db.read_bytes()
    result = resolve_cup_aliases("apif", db, scoped, [capture], tmp_path / "added")
    assert result[league] == baseline[league]
    assert result[league]["9101"]["evidence"][0]["kind"] == "database_binding"
    assert (
        _bridge("apif", db, league, {"9101": "Synthetic Alpha"}, scoped)["9101"]
        == baseline[league]["9101"]
    )
    assert scoped == original and db.read_bytes() == before_db


@pytest.mark.parametrize("at_target", [False, True])
def test_independent_fixture_proof_ignores_scoped_only_entry(tmp_path, at_target):
    db, league, capture, aliases = identity_inputs(tmp_path, "apif")
    baseline = resolve_cup_aliases("apif", db, {}, [capture], tmp_path / "baseline")
    target = league if at_target else "新联赛"
    mixed = {target: {"9101": scoped_copy(aliases["合成国内联赛"]["9101"], target)}}
    result = resolve_cup_aliases("apif", db, mixed, [capture], tmp_path / "added")
    assert result[league] == baseline[league]
    assert all(result[league][key]["canonical"] for key in ("9101", "9102"))
    assert "binding_scope" not in result[league]["9101"]
    assert result[league]["9101"]["evidence"][0]["apif_id"] == "apif:90001"


@pytest.mark.parametrize("days", [0, 7])
@pytest.mark.parametrize("new_first", [False, True])
def test_unrelated_scoped_row_keeps_legacy_inference_and_competitors(tmp_path, days, new_first):
    db, league, capture, aliases = identity_inputs(tmp_path, "apif")
    old = capture["games"][0]
    capture["games"].append(
        replace(
            old,
            provider_match_id=90002,
            home_provider_id=9103,
            away_provider_id=9104,
            kickoff_utc=old.kickoff_utc + timedelta(days=days),
        )
    )
    aliases["合成国内联赛"].pop("9102")
    baseline = resolve_cup_aliases("apif", db, aliases, [capture], tmp_path / "baseline")
    scoped = {
        "新联赛": {
            "9103": scoped_copy(
                dict(name="Gamma", canonical="合成丙", status="auto_verified", evidence=[]),
                "新联赛",
            )
        }
    }
    mixed = {**scoped, **deepcopy(aliases)} if new_first else {**deepcopy(aliases), **scoped}
    original = deepcopy(mixed)
    result = resolve_cup_aliases("apif", db, mixed, [capture], tmp_path / "added")
    assert result[league]["9102"]["canonical"] == "合成乙"
    assert without_alias_file_digest(result[league], mixed) == without_alias_file_digest(
        baseline[league], aliases
    )
    assert result[league]["9103"]["canonical"] is None
    assert result[league]["9104"]["canonical"] is None
    assert mixed == original


@pytest.mark.parametrize("at_target", [False, True])
def test_scoped_only_anchor_cannot_disambiguate_rival_games(tmp_path, at_target):
    db, league, capture, aliases = identity_inputs(tmp_path, "apif")
    old = capture["games"][0]
    capture["games"].append(
        replace(old, provider_match_id=90002, home_provider_id=9103, away_provider_id=9104)
    )
    baseline = resolve_cup_aliases("apif", db, {}, [capture], tmp_path / "baseline")
    target = league if at_target else "新联赛"
    entry = scoped_copy(aliases["合成国内联赛"]["9101"], target)
    mixed = {target: {"9101": entry}}
    result = resolve_cup_aliases("apif", db, mixed, [capture], tmp_path / "added")
    assert all(
        baseline[league][key]["canonical"] is None for key in ("9101", "9102", "9103", "9104")
    )
    expected = deepcopy(baseline[league])
    if at_target:
        expected["9101"] = entry
    assert result[league] == expected
    assert mixed == {target: {"9101": entry}}


@pytest.mark.parametrize("at_target", [False, True])
@pytest.mark.parametrize("conflict", ["same_id", "reverse"])
def test_inferred_aliases_still_check_scoped_canonical_conflicts(tmp_path, at_target, conflict):
    db, league, capture, aliases = identity_inputs(tmp_path, "apif")
    target = league if at_target else "新联赛"
    entry = scoped_copy(aliases["合成国内联赛"]["9101"], target)
    if conflict == "same_id":
        entry["canonical"] = "另一队"
        mixed = {target: {"9101": entry}}
    else:
        # A scoped target must remain part of the reverse-map validation even
        # when inference independently proves another ID with that same name.
        mixed = {league: {"9999": scoped_copy(entry, league)}}
    with pytest.raises(ValueError, match="conflict|冲突"):
        resolve_cup_aliases("apif", db, mixed, [capture], tmp_path / "audit")


def test_database_bridge_keeps_scoped_target_reverse_conflict_check(tmp_path):
    db, league, _, aliases = identity_inputs(tmp_path, "apif", official=False)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO teams VALUES(1,?,?)",
            ("合成甲", json.dumps([f"apif:{league}:team:9101"])),
        )
    mixed = {league: {"9999": scoped_copy(aliases["合成国内联赛"]["9101"], league)}}
    with pytest.raises(ValueError, match="conflict|冲突"):
        _bridge("apif", db, league, {"9101": "Synthetic Alpha"}, mixed)
