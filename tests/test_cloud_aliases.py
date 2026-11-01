"""Candidate uniqueness must hold across all complete captures, including midnight."""

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from zucai.cloud_aliases import write_candidates
from zucai.db import get_engine, init_db
from zucai.sources.base import RawStore

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t14"


def alias_inputs(tmp_path, provider, kickoff):
    database = tmp_path / "data/zucai.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    league = "英超" if provider == "fdorg" else "日职"
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO teams VALUES (1,'Home','[]'),(2,'Away','[]')")
        db.execute(
            "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES ('jc:1','jc',?,1,2,?,'scheduled')",
            (league, kickoff),
        )
    return database, tmp_path / "raw", tmp_path / "candidates"


def capture(raw_dir, provider, kickoff, index=0):
    day = kickoff[:10]
    instant = datetime.fromisoformat(kickoff)
    from datetime import timedelta

    next_day = (instant + timedelta(days=1)).date().isoformat()
    doc = json.loads((FIXTURES / f"{provider}.json").read_bytes())
    if provider == "fdorg":
        scope = dict(competition="PL", date_from=day, date_to=next_day)
        source = "football-data-org"
        row = doc["matches"][0]
        row.update(id=990001 + index, utcDate=kickoff)
        row["homeTeam"]["id"] += index * 2
        row["awayTeam"]["id"] += index * 2
    else:
        scope = dict(date=day)
        source = "api-football"
        doc["parameters"] = scope
        row = doc["response"][0]
        row["fixture"].update(id=9901 + index, date=kickoff, timestamp=int(instant.timestamp()))
        row["teams"]["home"]["id"] += index * 2
        row["teams"]["away"]["id"] += index * 2
    raw = json.dumps(doc).encode()
    RawStore(raw_dir).save(
        source,
        "complete",
        raw,
        "json",
        status_code=200,
        captured_at=datetime(2026, 11, 3, tzinfo=UTC),
    )
    return dict(scope=scope, raw_sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_adjacent_captures_do_not_hide_competing_midnight_fixtures(tmp_path, provider):
    db, raw, out = alias_inputs(tmp_path, provider, "2026-10-31T23:30:00+00:00")
    captures = [
        capture(raw, provider, "2026-10-31T23:00:00+00:00"),
        capture(raw, provider, "2026-11-01T00:30:00+00:00", 1),
    ]
    assert write_candidates(provider, db, raw, captures, out) == 0
    assert json.loads((out / f"{provider}.json").read_bytes()) == {}


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
@pytest.mark.parametrize("time", ["00:30:00", "23:30:00"])
def test_outer_window_boundary_cannot_prove_unique_identity(tmp_path, provider, time):
    kickoff = f"2026-11-01T{time}+00:00"
    db, raw, out = alias_inputs(tmp_path, provider, kickoff)
    captures = [capture(raw, provider, kickoff)]
    assert write_candidates(provider, db, raw, captures, out) == 0
    assert json.loads((out / f"{provider}.json").read_bytes()) == {}


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_persisted_reverse_alias_collision_never_replaces_candidate_file(tmp_path, provider):
    db, raw, out = alias_inputs(tmp_path, provider, "2026-11-01T12:00:00+00:00")
    captures = [capture(raw, provider, "2026-11-01T12:00:00+00:00")]
    out.mkdir()
    target = out / f"{provider}.json"
    league = "英超" if provider == "fdorg" else "日职"
    previous = {
        league: {
            "99999": dict(status="auto_verified", canonical="Home", evidence=[dict(note="old")])
        }
    }
    target.write_text(json.dumps(previous))
    with pytest.raises(ValueError, match="冲突"):
        write_candidates(provider, db, raw, captures, out)
    assert json.loads(target.read_bytes()) == previous


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_repeated_fixture_capture_keeps_one_identity_and_witness(tmp_path, provider):
    db, raw, out = alias_inputs(tmp_path, provider, "2026-11-01T12:00:00+00:00")
    captures = [capture(raw, provider, "2026-11-01T12:00:00+00:00") for _ in range(2)]
    assert write_candidates(provider, db, raw, captures, out) == 2
    records = next(iter(json.loads((out / f"{provider}.json").read_bytes()).values()))
    assert len(records) == 2
    assert all(len(record["evidence"]) == 1 for record in records.values())


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_conflicting_identity_for_same_fixture_cannot_produce_candidates(tmp_path, provider):
    db, raw, out = alias_inputs(tmp_path, provider, "2026-11-01T12:00:00+00:00")
    captures = [
        capture(raw, provider, "2026-11-01T12:00:00+00:00"),
        capture(raw, provider, "2026-11-01T13:00:00+00:00"),
    ]
    with pytest.raises(ValueError, match="跨原件比赛身份冲突"):
        write_candidates(provider, db, raw, captures, out)
    assert not (out / f"{provider}.json").exists()


@pytest.mark.parametrize("provider", ["fdorg", "apif"])
def test_merged_graph_evidence_points_to_each_actual_capture(tmp_path, provider):
    db, raw, out = alias_inputs(tmp_path, provider, "2026-11-01T12:00:00+00:00")
    league = "英超" if provider == "fdorg" else "日职"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO teams VALUES (3,'Other Home','[]'),(4,'Other Away','[]')")
        conn.execute(
            "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES ('jc:2','jc',?,3,4,"
            "'2026-11-02T12:00:00+00:00','scheduled')",
            (league,),
        )
    captures = [
        capture(raw, provider, "2026-11-01T12:00:00+00:00"),
        capture(raw, provider, "2026-11-02T12:00:00+00:00", 1),
    ]
    assert write_candidates(provider, db, raw, captures, out) == 4
    records = json.loads((out / f"{provider}.json").read_bytes())[league]
    first_id = 90001 if provider == "fdorg" else 551
    for index in range(2):
        evidence = records[str(first_id + index * 2)]["evidence"][0]
        assert evidence["raw_sha256"] == captures[index]["raw_sha256"]
        archived = raw / evidence["sidecar"].removesuffix(".meta.json")
        assert sha256(archived.read_bytes()).hexdigest() == captures[index]["raw_sha256"]
