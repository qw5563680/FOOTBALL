"""Cup identities use provider IDs and complete oriented evidence, never spelling."""

import csv
import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db
from zucai.sources.api_common import ApiGame

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t20"


def identity_inputs(tmp_path, provider, official=True):
    fixture = json.loads((FIXTURES / "identity.json").read_bytes())
    league = "欧冠" if provider == "fdorg" else "欧罗巴"
    db = tmp_path / "data/zucai.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    if official:
        with sqlite3.connect(db) as con:
            con.execute("INSERT INTO teams VALUES(1,?, '[]'),(2,?, '[]')", ("合成甲", "合成乙"))
            con.execute(
                "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) "
                "VALUES('jc:cup-test','jc',?,1,2,?,'scheduled')",
                (league, fixture["official"][0]["kickoff_utc"]),
            )
    games = []
    for row in fixture["games"]:
        row["kickoff_utc"] = datetime.fromisoformat(row["kickoff_utc"])
        row["full"], row["half"] = tuple(row["full"]), tuple(row["half"])
        games.append(ApiGame(provider=provider, league=league, **row))
    capture = {
        "games": games,
        "scope": {"competition": "CL", "season": 2024}
        if provider == "fdorg"
        else {"league_id": "3", "season": 2024},
        "complete": True,
        "raw_sha256": "a" * 64,
        "sidecar": "synthetic.meta.json",
        "seen_utc": "2026-11-03T00:00:00+00:00",
    }
    return db, league, capture, fixture["domestic_aliases"]


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_complete_jc_evidence_creates_auto_verified_cup_ids(tmp_path, provider):
    from zucai.sources.cup_identity import resolve_cup_aliases

    db, league, capture, _ = identity_inputs(tmp_path, provider)
    aliases = resolve_cup_aliases(provider, db, {}, [capture], tmp_path / "audit")
    assert aliases[league]["9101"]["canonical"] == "合成甲"
    assert aliases[league]["9102"]["canonical"] == "合成乙"
    assert aliases[league]["9101"]["status"] == "auto_verified"
    assert aliases[league]["9101"]["evidence"][0]["raw_sha256"] == "a" * 64
    audit = list(csv.DictReader((tmp_path / f"audit/{provider}_cup_aliases.csv").open()))
    assert len(audit) == 2


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_no_jc_proof_does_not_guess_labels_or_activate_proposals(tmp_path, provider):
    from zucai.sources.cup_identity import resolve_cup_aliases

    db, league, capture, base = identity_inputs(tmp_path, provider, official=False)
    for entry in base["合成国内联赛"].values():
        entry["status"] = "proposed"
    aliases = resolve_cup_aliases(provider, db, base, [capture], tmp_path / "audit")
    assert all(aliases[league][key]["status"] == "proposed" for key in ("9101", "9102"))
    assert all(aliases[league][key]["canonical"] is None for key in ("9101", "9102"))


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_active_same_provider_domestic_id_bridge_is_recorded_without_jc(tmp_path, provider):
    from zucai.sources.cup_identity import resolve_cup_aliases

    db, league, capture, base = identity_inputs(tmp_path, provider, official=False)
    original = deepcopy(base)
    aliases = resolve_cup_aliases(provider, db, base, [capture], tmp_path / "audit")
    assert base == original
    assert aliases[league]["9101"]["canonical"] == "合成甲"
    assert aliases[league]["9101"]["evidence"][0]["kind"] == "same_provider_id"


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
@pytest.mark.parametrize("problem", ["incomplete", "midnight", "competing"])
def test_missing_or_competing_capture_evidence_cannot_prove_identity(tmp_path, provider, problem):
    from dataclasses import replace

    from zucai.sources.cup_identity import resolve_cup_aliases

    db, league, capture, _ = identity_inputs(tmp_path, provider)
    if problem == "incomplete":
        capture["complete"] = False
    elif problem == "midnight":
        stamp = "2024-08-20T00:30:00+00:00"
        capture["games"] = [replace(capture["games"][0], kickoff_utc=datetime.fromisoformat(stamp))]
        capture["scope"] = (
            {"date": "2024-08-20"}
            if provider == "apif"
            else {"competition": "CL", "date_from": "2024-08-20", "date_to": "2024-08-21"}
        )
        with sqlite3.connect(db) as con:
            con.execute("UPDATE matches SET kickoff_utc=?", (stamp,))
    else:
        capture["games"].append(
            replace(
                capture["games"][0],
                provider_match_id=90002,
                home_provider_id=9103,
                away_provider_id=9104,
            )
        )
    aliases = resolve_cup_aliases(provider, db, {}, [capture], tmp_path / "audit")
    assert not any(e["status"] == "auto_verified" for e in aliases[league].values())


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_conflicting_domestic_provider_id_cannot_cross_into_cup(tmp_path, provider):
    from zucai.sources.cup_identity import resolve_cup_aliases

    db, _, capture, base = identity_inputs(tmp_path, provider)
    base["另一联赛"] = {"9101": {"canonical": "另一队", "status": "approved"}}
    with pytest.raises(ValueError, match="冲突"):
        resolve_cup_aliases(provider, db, base, [capture], tmp_path / "audit")


def test_another_apif_league_cannot_supply_missing_date_coverage(tmp_path):
    from dataclasses import replace

    from zucai.sources.cup_identity import resolve_cup_aliases

    db, league, capture, _ = identity_inputs(tmp_path, "apif")
    stamp = "2024-08-20T00:30:00+00:00"
    capture["games"] = [replace(capture["games"][0], kickoff_utc=datetime.fromisoformat(stamp))]
    capture["scope"] = {"date": "2024-08-20"}
    with sqlite3.connect(db) as con:
        con.execute("UPDATE matches SET kickoff_utc=?", (stamp,))
    other = {**capture, "games": [], "scope": {"league_id": "848", "season": 2024}}
    aliases = resolve_cup_aliases("apif", db, {}, [capture, other], tmp_path / "audit")
    assert aliases[league]["9101"]["status"] == "proposed"


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_persisted_cup_binding_survives_missing_jc_evidence(tmp_path, provider):
    from zucai.sources.cup_identity import resolve_cup_aliases

    db, league, capture, _ = identity_inputs(tmp_path, provider, official=False)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO teams VALUES(1,?,?)",
            ("合成甲", json.dumps([f"{provider}:{league}:team:9101"])),
        )
    aliases = resolve_cup_aliases(provider, db, {}, [capture], tmp_path / "audit")
    assert aliases[league]["9101"]["canonical"] == "合成甲"
    assert aliases[league]["9101"]["evidence"][0]["kind"] == "database_binding"
    assert aliases[league]["9102"]["canonical"] is None


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_domestic_reverse_collision_outside_cup_cannot_supply_anchor(tmp_path, provider):
    from zucai.sources.cup_identity import resolve_cup_aliases

    db, _, capture, base = identity_inputs(tmp_path, provider, official=False)
    base["合成国内联赛"]["9999"] = {"canonical": "合成甲", "status": "approved"}
    with pytest.raises(ValueError, match="conflict|冲突"):
        resolve_cup_aliases(provider, db, base, [capture], tmp_path / "audit")


@pytest.mark.parametrize("problem", ["missing_count", "filtered_request"])
def test_incomplete_fdorg_response_cannot_supply_identity_proof(tmp_path, problem):
    from zucai.sources.cup_identity import identity_capture, resolve_cup_aliases
    from zucai.sources.football_data_org import FootballDataOrg

    db, league, _, _ = identity_inputs(tmp_path, "fdorg")
    doc = json.loads((FIXTURES / "fdorg_cl_2024.json").read_bytes())
    scope = dict(competition="CL", season=2024)
    if problem == "missing_count":
        doc.pop("resultSet")
        doc["matches"] = doc["matches"][:1]
    else:
        scope["status"] = "FINISHED"
        doc["filters"].pop("status", None)
    source = FootballDataOrg()
    raw = json.dumps(doc).encode()
    games = source.parse(raw, **scope)
    source.session.close()
    assert games  # Existing parser permits this; it must not authorize new identity.
    with sqlite3.connect(db) as con:
        con.execute("UPDATE matches SET kickoff_utc=?", (games[0].kickoff_utc.isoformat(),))
    capture = identity_capture(
        games,
        [],
        scope,
        raw,
        {
            "sha256": "a" * 64,
            "fetched_at_utc": "2026-11-02T00:00:00Z",
        },
        "synthetic.meta.json",
    )
    assert capture["complete"] is False
    aliases = resolve_cup_aliases("fdorg", db, {}, [capture], tmp_path / "audit")
    assert all(record["status"] == "proposed" for record in aliases[league].values())
