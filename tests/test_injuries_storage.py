"""T38 uses small offline pages only, including its timestamp and conflict variants."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from zucai.db import get_engine, init_db, session_scope
from zucai.injuries_storage import (
    AUDIT_NAME,
    capture_receipt_paths,
    ingest_injuries,
    read_capture_receipt,
)
from zucai.models import ApifFixtureIndex, Match, MatchInjury, Team
from zucai.sources.apif_injuries import ApifInjuries, parse_injuries
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_storage import read_audit

FIX = Path(__file__).parents[1] / "data/fixtures/t38"
CAPTURE = "2024-09-09T10:00:00Z"
KICKOFF = "2024-09-10T12:00:00Z"


def document():
    return json.loads((FIX / "injuries.json").read_bytes())


@pytest.fixture
def injury_database(tmp_path):
    path = tmp_path / "feeds.db"
    engine = get_engine(path)
    init_db(engine)
    context = json.loads((FIX / "injuries_context.json").read_bytes())
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in context["teams"])
        session.flush()
        session.add_all(Match(**row) for row in context["matches"])
        session.flush()
        for row in context["indices"]:
            fixture = dict(
                fixture=dict(
                    id=int(row["provider_fixture_id"]),
                    date=row["kickoff_utc"],
                    status=dict(short="NS" if row["match_id"] == "fd:target" else "FT"),
                ),
                league=dict(id=39, season=2024),
                teams=dict(
                    home=dict(id=row["home_provider_id"]), away=dict(id=row["away_provider_id"])
                ),
            )
            session.add(
                ApifFixtureIndex(
                    **row,
                    captured_utc="2024-08-01T00:00:00Z",
                    available_utc=None,
                    availability_basis="backfilled",
                    raw_sha256="a" * 64,
                    raw_location="fixture-context",
                    parser_version="t38-test",
                    evidence=dict(fixture=fixture),
                )
            )
    engine.dispose()
    return path


def save_injuries(database, data=None, captured=CAPTURE):
    return RawStore(database.parent / "raw").save(
        "api-football",
        "injuries",
        json.dumps(document() if data is None else data).encode(),
        "json",
        captured_at=instant(captured),
        status_code=200,
        url="https://v3.football.api-sports.io/injuries?league=39&season=2024",
    )


def ingest(database, *, data=None, captured=CAPTURE, daily=True):
    return ingest_injuries(
        database, save_injuries(database, data, captured), league_id=39, season=2024, daily=daily
    )


def rows(database):
    engine = get_engine(database)
    with session_scope(engine) as session:
        result = [dict(row) for row in session.execute(select(MatchInjury.__table__)).mappings()]
    engine.dispose()
    return result


def receipts(database):
    return read_audit(database.parent / "state/audit" / AUDIT_NAME)


@pytest.mark.parametrize(
    "captured,daily,basis,before",
    [
        (CAPTURE, True, "captured", True),
        (KICKOFF, True, "backfilled", False),
        ("2024-09-11T12:00:00Z", True, "backfilled", False),
        (CAPTURE, False, "backfilled", True),
    ],
)
def test_parser_preserves_real_capture_boundary(captured, daily, basis, before):
    parsed = parse_injuries(
        (FIX / "injuries.json").read_bytes(),
        league_id=39,
        season=2024,
        captured_utc=captured,
        daily=daily,
    )
    assert len(parsed) == 4
    assert all(
        row.availability_basis == basis and row.captured_before_kickoff == before for row in parsed
    )
    assert all(
        row.available_utc == (instant(captured) if basis == "captured" else None) for row in parsed
    )
    assert parsed[1].reason is None


def test_source_fetch_delegates_existing_client_once_and_parse_is_pure():
    calls = []

    class Client:
        def fetch(self, endpoint, **scope):
            calls.append((endpoint, scope))
            return FIX / "injuries.json"

    source = ApifInjuries(
        league_id=39, season=2024, captured_utc=CAPTURE, daily=True, client=Client()
    )
    assert len(source.parse((FIX / "injuries.json").read_bytes())) == 4
    assert calls == []
    assert source.fetch_raw() == FIX / "injuries.json"
    assert calls == [("injuries", dict(league_id=39, season=2024))]


def test_cropped_real_page_preserves_provider_fields_and_original_capture_basis():
    raw = (FIX / "injuries_actual_excerpt.json").read_bytes()
    provenance = json.loads((FIX / "injuries_actual_provenance.json").read_bytes())
    assert hashlib.sha256(raw).hexdigest() == provenance["excerpt_sha256"]
    parsed = parse_injuries(
        raw, league_id=39, season=2026, captured_utc=provenance["captured_utc"], daily=True
    )
    original = json.loads(raw)
    assert len(parsed) == provenance["excerpt_response_count"] == 4
    assert all(
        row.raw_value == source for row, source in zip(parsed, original["response"], strict=True)
    )
    assert all(
        row.availability_basis == "backfilled"
        and row.available_utc is None
        and not row.captured_before_kickoff
        for row in parsed
    )
    assert parsed[0].raw_value["player"]["photo"].startswith("https://")
    assert parsed[0].raw_value["fixture"]["timestamp"] == parsed[0].fixture_kickoff_utc.timestamp()


def test_same_capture_is_idempotent_and_only_changed_type_appends_versions(injury_database):
    database = injury_database
    assert ingest(database)["inserted"] == 4
    audit = database.parent / "state/audit" / AUDIT_NAME
    before_db, before_audit = database.read_bytes(), audit.read_bytes()
    assert ingest(database)["duplicate"] is True
    assert database.read_bytes() == before_db and audit.read_bytes() == before_audit
    assert ingest(database, captured="2024-09-09T11:00:00Z")["inserted"] == 0
    assert len([r for r in receipts(database) if r["outcome"] == "ingested"]) == 2
    data = document()
    data["response"][1]["player"]["type"] = "Missing Fixture"
    assert ingest(database, data=data, captured="2024-09-09T12:00:00Z")["inserted"] == 1
    assert [r["version"] for r in rows(database) if r["player_id"] == 11] == [1, 2]
    assert ingest(database, data=data, captured="2024-09-10T10:00:00Z")["inserted"] == 0
    assert [r["version"] for r in rows(database) if r["player_id"] == 11] == [1, 2]
    assert {r["captured_day"] for r in rows(database)} == {"2024-09-09"}


def test_capture_day_is_utc(injury_database):
    ingest(injury_database, captured="2024-09-10T01:00:00+02:00")
    assert {r["captured_day"] for r in rows(injury_database)} == {"2024-09-09"}


def test_same_day_changed_content_keeps_first_version_and_complete_receipt(injury_database):
    ingest(injury_database)
    original = rows(injury_database)
    data = document()
    data["response"][0]["player"]["reason"] = "Revised diagnosis"
    result = ingest(injury_database, data=data, captured="2024-09-09T11:00:00Z")
    assert result["inserted"] == 1 and result["conflicts"] == 0
    stored = rows(injury_database)
    assert stored[0] == original[0]
    assert stored[-1]["reason"] == "Revised diagnosis" and stored[-1]["version"] == 2
    page = json.loads(receipts(injury_database)[-1]["detail"])
    payload = read_capture_receipt(injury_database.parent, page)
    entries = payload["fixtures"]["100"]["entries"]
    assert len(entries) == 4
    assert entries[0]["row_id"] == stored[-1]["id"]
    assert page["raw_location"] and page["captured_utc"]


@pytest.mark.parametrize("change", ["fixture", "team", "player", "kickoff", "index_scope"])
def test_unresolved_identities_preserved_and_audited_without_creating_entities(
    injury_database, change
):
    data = document()
    row = data["response"][0]
    if change == "fixture":
        row["fixture"]["id"] = 999
    elif change == "team":
        row["team"]["id"] = 999
    elif change == "player":
        row["player"]["id"] = None
    elif change == "kickoff":
        row["fixture"]["date"] = "2024-10-10T12:00:00Z"
    else:
        engine = get_engine(injury_database)
        with session_scope(engine) as session:
            session.get(ApifFixtureIndex, "100").season = 2023
        engine.dispose()
    result = ingest(injury_database, data=data)
    assert result["inserted"] == 4 and result["unlinked"] >= 1
    observed = rows(injury_database)[0]
    if change != "player":
        assert observed["match_id"] is observed["team_id"] is observed["side"] is None
    page = json.loads(receipts(injury_database)[0]["detail"])
    assert page["unresolved"] >= 1
    payload = read_capture_receipt(injury_database.parent, page)
    assert any(value["unresolved"] for value in payload["fixtures"].values())
    assert len(receipts(injury_database)) == 1
    engine = get_engine(injury_database)
    with session_scope(engine) as session:
        assert session.scalar(select(func.count()).select_from(Team)) == 4
        assert session.scalar(select(func.count()).select_from(Match)) == 5
    engine.dispose()


def test_invalid_rows_audit_but_good_rows_continue(injury_database):
    data = document()
    data["response"][0]["fixture"]["date"] = "2024-09-10T12:00:00"
    data["response"][1]["league"]["season"] = 2023
    result = ingest(injury_database, data=data)
    assert result["inserted"] == 2 and result["rejected"] == 2
    page = json.loads(receipts(injury_database)[0]["detail"])
    payload = read_capture_receipt(injury_database.parent, page)
    assert sum(len(value["rejected"]) for value in payload["fixtures"].values()) == 2
    assert page["rejected"] == 2 and len(receipts(injury_database)) == 1


@pytest.mark.parametrize("change", ["hash", "url", "status", "clock"])
def test_rawstore_sidecar_is_required_and_verified(injury_database, change):
    path = save_injuries(injury_database)
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    meta = json.loads(meta_path.read_bytes())
    if change == "hash":
        meta["sha256"] = "0" * 64
    elif change == "url":
        meta["url"] = meta["url"].replace("season=2024", "season=2023")
    elif change == "status":
        meta["status_code"] = 500
    else:
        meta["fetched_at_utc"] = "2024-09-09T10:00:00"
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        ingest_injuries(injury_database, path, league_id=39, season=2024)
    assert rows(injury_database) == []


@pytest.mark.parametrize("change", ["errors", "paging", "count", "scope"])
def test_invalid_envelope_is_audited_without_rows(injury_database, change):
    data = document()
    if change == "errors":
        data["errors"] = {"access": "Offline denied fixture"}
    elif change == "paging":
        data["paging"]["total"] = 2
    elif change == "count":
        data["results"] += 1
    else:
        data["parameters"]["fixture"] = "100"
    assert ingest(injury_database, data=data)["invalid"]
    assert not rows(injury_database)
    assert receipts(injury_database)[0]["reason"] == "invalid_page"


def test_empty_page_preserves_receipt_and_audit_failure_rolls_back(injury_database, monkeypatch):
    import zucai.injuries_storage as storage

    data = document()
    data.update(response=[], results=0)
    assert ingest(injury_database, data=data)["empty"]
    audit = injury_database.parent / "state/audit" / AUDIT_NAME
    before = audit.read_bytes()
    before_payloads = capture_receipt_paths(injury_database.parent)
    real = storage.write_audit

    def fail(path, items):
        real(path, items)
        raise OSError("offline simulated audit failure")

    monkeypatch.setattr(storage, "write_audit", fail)
    with pytest.raises(OSError):
        ingest(injury_database)
    assert rows(injury_database) == [] and audit.read_bytes() == before
    assert (
        sorted((injury_database.parent / "state/injuries_captures").glob("*.gz")) == before_payloads
    )


def test_duplicate_unknown_player_has_no_name_binding(injury_database):
    data = document()
    row = copy.deepcopy(data["response"][0])
    row["player"]["id"] = None
    data["response"] = [row, copy.deepcopy(row)]
    data["results"] = 2
    assert ingest(injury_database, data=data)["inserted"] == 1
    assert rows(injury_database)[0]["player_id"] is None


def test_amplified_fixture_page_has_bounded_receipts_and_replays_complete_snapshot(
    injury_database,
):
    from zucai.features.injuries import InjuryFeatures

    data = document()
    original = data["response"]
    data["response"] = []
    for number in range(800):
        row = copy.deepcopy(original[number % len(original)])
        row["player"]["id"] = 10_000 + number
        data["response"].append(row)
    data["results"] = len(data["response"])
    assert ingest(injury_database, data=data)["inserted"] == 800
    stored = receipts(injury_database)  # Exercise default CSV parser ceiling.
    assert len(stored) == 1
    assert len(capture_receipt_paths(injury_database.parent)) == 1
    assert all(len(value.encode()) <= 100_000 for r in stored for value in r.values())
    assert ingest(injury_database, data=data)["duplicate"]
    engine = get_engine(injury_database)
    with session_scope(engine) as session:
        builder = InjuryFeatures(session)
        result = builder.build(builder.matches["fd:target"], as_of=KICKOFF)
    assert result["home"]["missing_n"]["value"] == 400
    assert result["away"]["missing_n"]["value"] == 200
    # A truncated compressed payload never masquerades as the full player list.
    payload = capture_receipt_paths(injury_database.parent)[0]
    payload.write_bytes(payload.read_bytes()[:-1])
    with session_scope(engine) as session:
        builder = InjuryFeatures(session)
        result = builder.build(builder.matches["fd:target"], as_of=KICKOFF)
    engine.dispose()
    assert result["home"]["missing_n"]["missing_reason"] == "incomplete_capture"


def test_content_versions_keep_first_clock_and_extend_only_last_clock(injury_database):
    ingest(injury_database)
    first = rows(injury_database)
    assert ingest(injury_database, captured="2024-09-10T10:00:00Z")["inserted"] == 0
    later = rows(injury_database)
    assert len(later) == len(first) == 4
    for original, refreshed in zip(first, later, strict=True):
        assert refreshed["captured_utc"] == original["captured_utc"]
        assert instant(refreshed["last_captured_utc"]) == instant("2024-09-10T10:00:00Z")
        assert refreshed["raw_sha256"] == original["raw_sha256"]


def test_content_a_b_a_creates_three_versions(injury_database):
    ingest(injury_database)
    changed = document()
    changed["response"][0]["player"]["reason"] = "Revised diagnosis"
    assert ingest(injury_database, data=changed, captured="2024-09-09T11:00:00Z")["inserted"] == 1
    assert ingest(injury_database, captured="2024-09-09T12:00:00Z")["inserted"] == 1
    versions = [row for row in rows(injury_database) if row["player_id"] == 10]
    assert [row["version"] for row in versions] == [1, 2, 3]
    assert versions[0]["content_sha256"] == versions[2]["content_sha256"]
    assert versions[0]["content_sha256"] != versions[1]["content_sha256"]


def test_each_capture_has_one_small_csv_receipt_and_gzip_payload(injury_database):
    data = document()
    original = data["response"][0]
    data["response"] = []
    for number in range(800):
        row = copy.deepcopy(original)
        row["fixture"]["id"] = 1000 + number
        row["player"]["id"] = 10_000 + number
        data["response"].append(row)
    data["results"] = len(data["response"])
    ingest(injury_database, data=data)
    audit = injury_database.parent / "state/audit" / AUDIT_NAME
    assert len(receipts(injury_database)) == 1
    first_size = audit.stat().st_size
    assert first_size < 3000
    ingest(injury_database, data=data, captured="2024-09-10T10:00:00Z")
    assert audit.stat().st_size - first_size < 3000
    assert len(rows(injury_database)) == 800
    assert len(capture_receipt_paths(injury_database.parent)) == 2


def test_unknown_players_with_same_name_are_not_bound(injury_database):
    data = document()
    first, second = copy.deepcopy(data["response"][0]), copy.deepcopy(data["response"][1])
    first["player"].update(id=None, name="Unknown Player")
    second["player"].update(id=None, name="Unknown Player")
    data.update(response=[first, second, copy.deepcopy(first)], results=3)
    assert ingest(injury_database, data=data)["inserted"] == 2
    assert len({row["record_key"] for row in rows(injury_database)}) == 2
    assert all(row["player_id"] is None for row in rows(injury_database))


def test_interval_update_and_new_gzip_roll_back_on_audit_failure(injury_database, monkeypatch):
    import zucai.injuries_storage as storage

    ingest(injury_database)
    before_rows = rows(injury_database)
    before_paths = capture_receipt_paths(injury_database.parent)

    def fail(*args):
        raise OSError("fixture simulated audit failure")

    monkeypatch.setattr(storage, "write_audit", fail)
    with pytest.raises(OSError):
        ingest(injury_database, captured="2024-09-10T10:00:00Z")
    assert rows(injury_database) == before_rows
    assert sorted((injury_database.parent / "state/injuries_captures").glob("*.gz")) == before_paths


def test_predeployment_draft_schema_requires_explicit_replay(injury_database):
    import sqlite3

    from zucai.stats_schema import migrate_stats

    with sqlite3.connect(injury_database) as db:
        db.execute("DROP TABLE match_injuries")
        db.execute("CREATE TABLE match_injuries (id INTEGER PRIMARY KEY)")
    before = injury_database.read_bytes()
    engine = get_engine(injury_database)
    try:
        with pytest.raises(ValueError, match="草稿表不支持迁移"):
            migrate_stats(engine)
    finally:
        engine.dispose()
    assert injury_database.read_bytes() == before
