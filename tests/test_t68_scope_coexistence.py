"""M1: scoped additions cannot revoke an independent legacy authorization."""

import hashlib
import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest

from test_cup_identity import identity_inputs
from zucai.alias_propagation import _plan
from zucai.sources.cup_identity import resolve_cup_aliases


def scoped_copy(entry, league):
    return {
        **deepcopy(entry),
        "binding_scope": {
            "leagues": [league],
            "category": "club/men/senior/first",
            "start_utc": "2018-01-01T00:00:00Z",
            "end_utc": "2018-12-31T23:59:59Z",
        },
    }


def without_alias_file_digest(entries, aliases):
    """The source file really changed; all authorization values must stay equal."""
    digest = hashlib.sha256(json.dumps(aliases, sort_keys=True).encode()).hexdigest()
    entries = deepcopy(entries)
    for entry in entries.values():
        for proof in entry.get("evidence", []):
            if "source_aliases_sha256" in proof:
                assert proof.pop("source_aliases_sha256") == digest
    return entries


@pytest.mark.parametrize("league,code", [("欧罗巴", "3"), ("欧协联", "848")])
@pytest.mark.parametrize("route", ["bridge", "database", "infer"])
@pytest.mark.parametrize("scope_at_target", [False, True])
def test_scoped_addition_keeps_legacy_cup_authorization(
    tmp_path, league, code, route, scope_at_target
):
    db, _, capture, aliases = identity_inputs(tmp_path, "apif", official=route == "infer")
    capture["scope"]["league_id"] = code
    capture["games"] = [replace(game, league=league) for game in capture["games"]]
    with sqlite3.connect(db) as con:
        con.execute("UPDATE matches SET league=?", (league,))
        if route == "database":
            con.execute(
                "INSERT INTO teams VALUES(1,?,?)",
                ("合成甲", json.dumps([f"apif:{league}:team:9101"])),
            )
    if route == "infer":
        aliases["合成国内联赛"].pop("9102")
    baseline = resolve_cup_aliases("apif", db, aliases, [capture], tmp_path / "before")
    assert baseline[league]["9101"]["canonical"] == "合成甲"
    assert baseline[league]["9102"]["canonical"] == "合成乙"

    mixed = deepcopy(aliases)
    added_league = league if scope_at_target else "新杯赛"
    mixed[added_league] = {"9101": scoped_copy(aliases["合成国内联赛"]["9101"], added_league)}
    original = deepcopy(mixed)
    before_db = db.read_bytes()
    result = resolve_cup_aliases("apif", db, mixed, [capture], tmp_path / "after")
    assert result[league]["9101"]["canonical"] == "合成甲"
    assert result[league]["9102"]["canonical"] == "合成乙"
    assert without_alias_file_digest(result[league], mixed) == without_alias_file_digest(
        baseline[league], aliases
    )
    assert mixed == original and db.read_bytes() == before_db


@pytest.mark.parametrize("scoped_league", ["英甲", "英联杯", "英足总杯"])
@pytest.mark.parametrize("legacy_propagated", [False, True])
@pytest.mark.parametrize("new_first", [False, True])
def test_scoped_addition_cannot_win_over_legacy_propagation(
    scoped_league, legacy_propagated, new_first
):
    old = dict(
        name="Club",
        canonical="甲队",
        status="auto_verified",
        evidence=[{"kind": "same_provider_id"}] if legacy_propagated else [],
    )
    legacy = {"英超": {"101": old}}
    _, baseline = _plan(legacy, "apif", {"101": {"英冠"}}, "a" * 64)
    assert len(baseline) == 1
    assert baseline[0]["canonical"] == "甲队"
    assert baseline[0]["evidence"][0]["source_league"] == "英超"
    addition = {scoped_league: {"101": scoped_copy(old, scoped_league)}}
    aliases = {**addition, **legacy} if new_first else {**legacy, **addition}
    before = deepcopy(aliases)
    updated, changes = _plan(aliases, "apif", {"101": {"英冠"}}, "a" * 64)
    assert changes == baseline
    assert updated["英冠"]["101"]["canonical"] == "甲队"
    for league, entries in before.items():
        assert updated[league] == entries
    assert aliases == before


@pytest.mark.parametrize("legacy_status", ["proposed", "rejected"])
def test_inactive_legacy_entry_cannot_unlock_scoped_only_cup(tmp_path, legacy_status):
    db, league, capture, aliases = identity_inputs(tmp_path, "apif")
    old = aliases["合成国内联赛"]["9101"]
    aliases["新杯赛"] = {"9101": scoped_copy(old, "新杯赛")}
    old["status"] = legacy_status
    aliases["合成国内联赛"].pop("9102")
    if legacy_status == "rejected":
        with pytest.raises(ValueError, match="别名状态无效"):
            resolve_cup_aliases("apif", db, aliases, [capture], tmp_path / "audit")
        return
    result = resolve_cup_aliases("apif", db, aliases, [capture], tmp_path / "audit")
    assert all(result[league][key]["canonical"] is None for key in ("9101", "9102"))


@pytest.mark.parametrize("consumer", ["cup", "propagation"])
def test_scoped_addition_does_not_hide_canonical_conflict(tmp_path, consumer):
    db, _, capture, aliases = identity_inputs(tmp_path, "apif")
    record = scoped_copy(aliases["合成国内联赛"]["9101"], "新杯赛")
    record["canonical"] = "另一队"
    aliases["新杯赛"] = {"9101": record}
    with pytest.raises(ValueError, match="冲突"):
        if consumer == "cup":
            resolve_cup_aliases("apif", db, aliases, [capture], tmp_path / "audit")
        else:
            _plan(aliases, "apif", {"9101": {"英冠"}}, "a" * 64)
