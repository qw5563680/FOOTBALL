"""Immutable, cutoff-aware result identity evidence and its optional archive."""

import copy
import hashlib
import importlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from zucai import models
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t63/result-links.json"
FIRST = "2026-11-10T12:00:00.000000Z"
LATER = "2026-11-11T12:00:00.000000Z"


def module():
    return importlib.import_module("zucai.result_source_links")


@pytest.fixture
def seed(tmp_path):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    database = tmp_path / "source.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in document["teams"])
        session.flush()
        session.add_all(Match(**row) for row in document["matches"])
    yield database, engine
    engine.dispose()


def record(session, target="jc:1", verified=FIRST):
    return module().record_result_link(
        session,
        session.get(Match, "apif:1"),
        session.get(Match, target),
        datetime.fromisoformat(verified),
    )


def publish(database, archive):
    with sqlite3.connect(database) as db:
        descriptor = module().export_result_links(db, archive)
    target = archive / module().RESULT_LINK_FILE
    return {
        "files": {module().RESULT_LINK_FILE: hashlib.sha256(target.read_bytes()).hexdigest()},
        "result_source_links": descriptor,
    }


def clear_links(database):
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM result_source_links")


def test_result_link_table_keeps_three_part_provenance_key_and_foreign_keys(seed):
    _, engine = seed
    assert hasattr(models, "ResultSourceLink"), "T63 result identity table is missing"
    schema = inspect(engine)
    assert schema.get_pk_constraint("result_source_links")["constrained_columns"] == [
        "provider_match_id",
        "counterpart_id",
        "verified_utc",
    ]
    assert {
        (tuple(row["constrained_columns"]), row["referred_table"])
        for row in schema.get_foreign_keys("result_source_links")
    } == {
        (("provider_match_id",), "matches"),
        (("counterpart_id",), "matches"),
        (("home_team_id",), "teams"),
        (("away_team_id",), "teams"),
    }


def test_records_all_supported_sources_without_requiring_results_or_changing_matches(seed):
    _, engine = seed
    with session_scope(engine) as session:
        for target in ("jc:1", "fd:1", "fdorg:1"):
            assert record(session, target) is True
        assert module().load_result_links(session) == [
            ("apif:1", "fd:1", FIRST),
            ("apif:1", "fdorg:1", FIRST),
            ("apif:1", "jc:1", FIRST),
        ]
        assert list(session.scalars(select(models.Result))) == []
        assert all(match.counterpart_id is None for match in session.scalars(select(Match)))


def test_repeated_snapshot_cannot_advance_or_backdate_first_stored_verification(seed):
    _, engine = seed
    with session_scope(engine) as session:
        assert record(session)
        assert record(session, verified=LATER) is False
        assert record(session, verified="2026-11-09T12:00:00Z") is False
        assert module().load_result_links(session) == [("apif:1", "jc:1", FIRST)]
        assert len(list(session.scalars(select(models.ResultSourceLink)))) == 1


@pytest.mark.parametrize("side", ["provider", "counterpart"])
@pytest.mark.parametrize(
    "field,value",
    [("home_team_id", 3), ("league", "英冠"), ("kickoff_utc", "2026-11-10T11:00:00Z")],
)
def test_identity_drift_disables_old_evidence_and_reverification_adds_snapshot(
    seed, side, field, value
):
    _, engine = seed
    with session_scope(engine) as session:
        assert record(session)
        provider = session.get(Match, "apif:1")
        target = session.get(Match, "jc:1")
        setattr(provider if side == "provider" else target, field, value)
        session.flush()
        assert module().load_result_links(session) == []
        if field != "kickoff_utc":
            setattr(target if side == "provider" else provider, field, value)
        assert record(session, verified=LATER)
        assert module().load_result_links(session) == [("apif:1", "jc:1", LATER)]
        rows = list(
            session.scalars(
                select(models.ResultSourceLink).order_by(models.ResultSourceLink.verified_utc)
            )
        )
        assert [row.verified_utc for row in rows] == [FIRST, LATER]
        assert rows[0].home_team_id == 1
        assert rows[0].league == "英超"
        assert rows[0].provider_kickoff_utc == "2026-11-10T10:00:00.000000Z"
        assert rows[0].counterpart_kickoff_utc == "2026-11-10T10:00:00.000000Z"


def test_loader_requires_exact_persisted_kickoff_snapshot(seed):
    database, engine = seed
    with session_scope(engine) as session:
        record(session)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE matches SET kickoff_utc=? WHERE match_id='jc:1'", ("2026-11-10T10:00:00Z",)
        )
    with session_scope(engine) as session:
        assert module().load_result_links(session) == []


def test_loader_requires_current_source_to_match_saved_counterpart_prefix(seed):
    database, engine = seed
    with session_scope(engine) as session:
        record(session)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE matches SET source='fd' WHERE match_id='jc:1'")
    with session_scope(engine) as session:
        assert module().load_result_links(session) == []


@pytest.mark.parametrize(
    "name", ["apif:team:1", "fdorg:team:1", "jc:team:1", "fd:E0:Home", "APIF:英超:team:1"]
)
def test_provider_placeholder_names_cannot_authorize_evidence(seed, name):
    _, engine = seed
    with session_scope(engine) as session:
        session.get(Team, 1).name = name
        with pytest.raises(ValueError):
            record(session)
        assert module().load_result_links(session) == []


@pytest.mark.parametrize("damage", ["home", "away", "league", "window", "source", "self"])
def test_mismatched_identity_source_or_window_is_rejected(seed, damage):
    _, engine = seed
    with session_scope(engine) as session:
        provider = session.get(Match, "apif:1")
        target = session.get(Match, "jc:1")
        if damage == "home":
            target.home_team_id = 3
        elif damage == "away":
            target.away_team_id = 3
        elif damage == "league":
            target.league = "英冠"
        elif damage == "window":
            target.kickoff_utc = "2026-11-10T13:00:00.000001Z"
        elif damage == "source":
            provider = session.get(Match, "fd:1")
        else:
            target = provider
        with pytest.raises(ValueError):
            module().record_result_link(session, provider, target, datetime.fromisoformat(FIRST))
        assert module().load_result_links(session) == []


def test_same_provider_cannot_claim_another_target_from_same_source(seed):
    _, engine = seed
    with session_scope(engine) as session:
        record(session)
        with pytest.raises(ValueError):
            record(session, "jc:2", LATER)
        assert module().load_result_links(session) == [("apif:1", "jc:1", FIRST)]


def test_fdorg_links_to_higher_priority_sources_without_blocking_apif(seed):
    _, engine = seed
    with session_scope(engine) as session:
        assert record(session)
        assert module().record_result_link(
            session,
            session.get(Match, "fdorg:1"),
            session.get(Match, "jc:1"),
            datetime.fromisoformat(FIRST),
        )
        assert module().load_result_links(session) == [
            ("apif:1", "jc:1", FIRST),
            ("fdorg:1", "jc:1", FIRST),
        ]
        with pytest.raises(ValueError):
            module().record_result_link(
                session,
                session.get(Match, "fdorg:1"),
                session.get(Match, "apif:1"),
                datetime.fromisoformat(FIRST),
            )


@pytest.mark.parametrize("source", ["apif", "fdorg"])
@pytest.mark.parametrize("operation", ["record", "load", "export", "restore"])
def test_counterpart_cannot_be_claimed_by_two_matches_from_same_provider_source(
    seed, tmp_path, source, operation
):
    database, engine = seed
    with session_scope(engine) as session:
        first = session.get(Match, source + ":1")
        second = Match(
            match_id=source + ":2",
            source=source,
            league=first.league,
            home_team_id=first.home_team_id,
            away_team_id=first.away_team_id,
            kickoff_utc=first.kickoff_utc,
            status="scheduled",
        )
        session.add(second)
        session.flush()
        module().record_result_link(
            session, first, session.get(Match, "jc:1"), datetime.fromisoformat(FIRST)
        )
        if operation == "record":
            with pytest.raises(ValueError):
                module().record_result_link(
                    session, second, session.get(Match, "jc:1"), datetime.fromisoformat(LATER)
                )
            return
    archive = tmp_path / "archive"
    meta = publish(database, archive)
    with sqlite3.connect(database) as db:
        row = list(db.execute("SELECT * FROM result_source_links").fetchone())
        row[0] = source + ":2"
        row[2] = LATER
        if operation == "restore":
            target = archive / module().RESULT_LINK_FILE
            target.write_text(
                target.read_text(encoding="utf-8") + json.dumps(row) + "\n", encoding="utf-8"
            )
            meta["result_source_links"]["count"] = 2
            meta["files"][module().RESULT_LINK_FILE] = hashlib.sha256(
                target.read_bytes()
            ).hexdigest()
            db.execute("DELETE FROM result_source_links")
            with pytest.raises(ValueError):
                module().restore_result_links(db, archive, meta)
            assert db.execute("SELECT count(*) FROM result_source_links").fetchone() == (0,)
            return
        db.execute("INSERT INTO result_source_links VALUES (?,?,?,?,?,?,?,?)", row)
    if operation == "load":
        with session_scope(engine) as session, pytest.raises(ValueError):
            module().load_result_links(session)
    else:
        with sqlite3.connect(database) as db, pytest.raises(ValueError):
            module().export_result_links(db, tmp_path / "invalid")


@pytest.mark.parametrize("operation", ["record", "load", "export", "restore"])
def test_component_bridge_cannot_merge_two_api_matches_from_the_same_source(
    seed, tmp_path, operation
):
    database, engine = seed
    with session_scope(engine) as session:
        for match in session.scalars(select(Match)):
            match.kickoff_utc = "2026-11-10T10:00:00Z"
        session.add(
            Match(
                match_id="apif:2",
                source="apif",
                league="英超",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc="2026-11-10T10:00:00Z",
                status="scheduled",
            )
        )
        session.flush()
        for provider_id, counterpart_id in [
            ("apif:1", "fd:1"),
            ("apif:2", "jc:1"),
            ("fdorg:1", "fd:1"),
        ]:
            module().record_result_link(
                session,
                session.get(Match, provider_id),
                session.get(Match, counterpart_id),
                datetime.fromisoformat(FIRST),
            )
        if operation == "record":
            with pytest.raises(ValueError):
                module().record_result_link(
                    session,
                    session.get(Match, "fdorg:1"),
                    session.get(Match, "jc:1"),
                    datetime.fromisoformat(LATER),
                )
            return
    archive = tmp_path / "archive"
    meta = publish(database, archive)
    with sqlite3.connect(database) as db:
        row = list(
            db.execute(
                "SELECT * FROM result_source_links WHERE provider_match_id='fdorg:1'"
            ).fetchone()
        )
        row[1], row[2] = "jc:1", LATER
        if operation == "restore":
            target = archive / module().RESULT_LINK_FILE
            target.write_text(
                target.read_text(encoding="utf-8") + json.dumps(row) + "\n", encoding="utf-8"
            )
            meta["result_source_links"]["count"] = 4
            meta["files"][module().RESULT_LINK_FILE] = hashlib.sha256(
                target.read_bytes()
            ).hexdigest()
            db.execute("DELETE FROM result_source_links")
            with pytest.raises(ValueError):
                module().restore_result_links(db, archive, meta)
            assert db.execute("SELECT count(*) FROM result_source_links").fetchone() == (0,)
            return
        db.execute("INSERT INTO result_source_links VALUES (?,?,?,?,?,?,?,?)", row)
    if operation == "load":
        with session_scope(engine) as session, pytest.raises(ValueError):
            module().load_result_links(session)
    else:
        with sqlite3.connect(database) as db, pytest.raises(ValueError):
            module().export_result_links(db, tmp_path / "invalid")


def test_naive_verification_is_rejected_and_explicit_offsets_are_utc(seed):
    _, engine = seed
    with session_scope(engine) as session:
        with pytest.raises(ValueError):
            record(session, verified="2026-11-10T12:00:00")
        assert record(session, verified="2026-11-10T20:00:00+08:00")
        assert module().load_result_links(session) == [("apif:1", "jc:1", FIRST)]


def test_snapshot_cannot_overwrite_same_pair_and_verification_key(seed):
    _, engine = seed
    with session_scope(engine) as session:
        record(session)
        session.get(Match, "apif:1").kickoff_utc = "2026-11-10T11:00:00Z"
        with pytest.raises(ValueError):
            record(session)
        assert len(list(session.scalars(select(models.ResultSourceLink)))) == 1


def test_old_database_and_manifest_are_read_without_adding_optional_table(seed, tmp_path):
    database, engine = seed
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE result_source_links")
    with session_scope(engine) as session:
        assert module().load_result_links(session) == []
    with sqlite3.connect(database) as db:
        assert module().export_result_links(db, tmp_path / "archive") is None
        module().restore_result_links(db, tmp_path / "archive", {"files": {}})
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='result_source_links'").fetchone()
            is None
        )
    assert module().validate_result_links_manifest({"files": {}}) is False
    assert not (tmp_path / "archive" / module().RESULT_LINK_FILE).exists()


def test_archive_roundtrip_preserves_historical_snapshots_without_reactivating_them(seed, tmp_path):
    database, engine = seed
    with session_scope(engine) as session:
        record(session)
        session.get(Match, "apif:1").kickoff_utc = "2026-11-10T11:00:00Z"
        record(session, verified=LATER)
    archive = tmp_path / "archive"
    meta = publish(database, archive)
    assert meta["result_source_links"]["count"] == 2
    assert module().validate_result_links_manifest(meta) is True
    clear_links(database)
    with sqlite3.connect(database) as db:
        module().restore_result_links(db, archive, meta)
    with session_scope(engine) as session:
        assert module().load_result_links(session) == [("apif:1", "jc:1", LATER)]
    again = publish(database, tmp_path / "again")
    assert again == meta


@pytest.mark.parametrize(
    "damage",
    [
        "descriptor",
        "unlisted",
        "version",
        "columns",
        "count",
        "hash",
        "missing",
        "shape",
        "reference",
        "time",
        "source",
        "window",
        "identical_teams",
        "duplicate",
        "conflicting_target",
    ],
)
def test_corrupt_archive_is_rejected_before_any_link_is_restored(seed, tmp_path, damage):
    database, engine = seed
    with session_scope(engine) as session:
        record(session)
    archive = tmp_path / "archive"
    meta = publish(database, archive)
    target = archive / module().RESULT_LINK_FILE
    if damage == "descriptor":
        del meta["result_source_links"]
    elif damage == "unlisted":
        del meta["files"][module().RESULT_LINK_FILE]
    elif damage == "version":
        meta["result_source_links"]["version"] = True
    elif damage == "columns":
        meta["result_source_links"]["columns"] = []
    elif damage == "count":
        meta["result_source_links"]["count"] += 1
    elif damage == "hash":
        target.write_bytes(target.read_bytes() + b"\n")
    elif damage == "missing":
        target.unlink()
    else:
        rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        columns = meta["result_source_links"]["columns"]
        if damage == "shape":
            rows[0].pop()
        elif damage in {"duplicate", "conflicting_target"}:
            other = copy.deepcopy(rows[0])
            other[columns.index("verified_utc")] = LATER
            if damage == "conflicting_target":
                other[columns.index("counterpart_id")] = "jc:2"
            rows.append(other)
            meta["result_source_links"]["count"] += 1
        else:
            field, value = {
                "reference": ("home_team_id", 999),
                "time": ("verified_utc", "2026-11-10T12:00:00"),
                "source": ("provider_match_id", "fd:1"),
                "window": ("provider_kickoff_utc", "2026-11-09T10:00:00.000000Z"),
                "identical_teams": ("home_team_id", 2),
            }[damage]
            rows[0][columns.index(field)] = value
        target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        meta["files"][module().RESULT_LINK_FILE] = hashlib.sha256(target.read_bytes()).hexdigest()
    clear_links(database)
    with sqlite3.connect(database) as db:
        with pytest.raises(ValueError):
            module().restore_result_links(db, archive, meta)
        assert db.execute("SELECT count(*) FROM result_source_links").fetchone() == (0,)
