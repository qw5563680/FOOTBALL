"""Source bindings become historical evidence only when explicitly observed."""

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

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t63/result-identity.json"
FIRST = "2026-11-10T12:00:00.000000Z"
LATER = "2026-11-11T12:00:00.000000Z"


def module():
    return importlib.import_module("zucai.result_source_identity")


@pytest.fixture
def seed(tmp_path):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    database = tmp_path / "identity.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in document["teams"])
        session.flush()
        session.add_all(Match(**row) for row in document["matches"])
    yield database, engine
    engine.dispose()


def record(session, match_id="apif:1", verified=FIRST):
    return module().record_result_identity(
        session, session.get(Match, match_id), datetime.fromisoformat(verified)
    )


def publish(database, archive):
    with sqlite3.connect(database) as db:
        descriptor = module().export_result_identities(db, archive)
    target = archive / module().RESULT_IDENTITY_FILE
    return {
        "files": {module().RESULT_IDENTITY_FILE: hashlib.sha256(target.read_bytes()).hexdigest()},
        "result_source_identities": descriptor,
    }


def test_identity_table_tracks_verification_separately_from_matches(seed):
    _, engine = seed
    assert hasattr(models, "ResultSourceIdentity"), "timestamped identity evidence is missing"
    assert inspect(engine).get_pk_constraint("result_source_identities")["constrained_columns"] == [
        "match_id",
        "verified_utc",
    ]


def test_current_aliases_without_observation_do_not_prove_past_identity(seed):
    _, engine = seed
    with session_scope(engine) as session:
        assert module().load_result_identities(session) == {}
        assert record(session)
        assert record(session, "fdorg:1")
        assert module().load_result_identities(session) == {"apif:1": FIRST, "fdorg:1": FIRST}
        assert list(session.scalars(select(models.Result))) == []


def test_repeat_observation_does_not_advance_or_backdate_verification(seed):
    _, engine = seed
    with session_scope(engine) as session:
        assert record(session)
        assert record(session, verified=LATER) is False
        assert record(session, verified="2026-11-09T12:00:00Z") is False
        assert module().load_result_identities(session) == {"apif:1": FIRST}
        assert len(list(session.scalars(select(models.ResultSourceIdentity)))) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "friendly",
        "unscoped",
        "wrong_league",
        "bad_number",
        "multiple",
        "shared",
        "provisional",
        "source",
        "time",
    ],
)
def test_only_unique_scoped_canonical_bindings_authorize_identity(seed, damage):
    _, engine = seed
    with session_scope(engine) as session:
        home = session.get(Team, 1)
        if damage == "friendly":
            home.aliases = ["Home FC"]
        elif damage == "unscoped":
            home.aliases = ["apif:team:11"]
        elif damage == "wrong_league":
            home.aliases = ["apif:英冠:team:11"]
        elif damage == "bad_number":
            home.aliases = ["apif:英超:team:01"]
        elif damage == "multiple":
            home.aliases.append("apif:英超:team:111")
        elif damage == "shared":
            other = session.get(Team, 3)
            other.aliases.append("apif:英超:team:11")
        elif damage == "provisional":
            home.name = "apif:team:11"
        with pytest.raises(ValueError):
            record(
                session,
                "jc:1" if damage == "source" else "apif:1",
                "2026-11-10T12:00:00" if damage == "time" else FIRST,
            )
        assert module().load_result_identities(session) == {}


@pytest.mark.parametrize(
    "field,value",
    [("home_team_id", 3), ("kickoff_utc", "2026-11-10T11:00:00Z"), ("league", "英冠")],
)
def test_changed_snapshot_cannot_inherit_earlier_identity_time(seed, field, value):
    _, engine = seed
    with session_scope(engine) as session:
        assert record(session)
        setattr(session.get(Match, "apif:1"), field, value)
        if field == "league":
            home, away = session.get(Team, 1), session.get(Team, 2)
            home.aliases.append("apif:英冠:team:11")
            away.aliases.append("apif:英冠:team:12")
        assert module().load_result_identities(session) == {}
        assert record(session, verified=LATER)
        assert module().load_result_identities(session) == {"apif:1": LATER}
        assert len(list(session.scalars(select(models.ResultSourceIdentity)))) == 2


def test_removed_binding_disables_proof_without_deleting_history(seed):
    _, engine = seed
    with session_scope(engine) as session:
        record(session)
        session.get(Team, 1).aliases = []
        assert module().load_result_identities(session) == {}
        assert len(list(session.scalars(select(models.ResultSourceIdentity)))) == 1


@pytest.mark.parametrize("source", ["apif", "fdorg"])
@pytest.mark.parametrize("side,team_id,new_id", [("home", 1, 999), ("away", 2, 998)])
def test_replaced_binding_gets_new_proof_time_and_preserves_inactive_history(
    seed, tmp_path, source, side, team_id, new_id
):
    database, engine = seed
    match_id = f"{source}:1"
    with session_scope(engine) as session:
        assert record(session, match_id)
        team = session.get(Team, team_id)
        prefix = f"{source}:英超:team:"
        original = next(alias for alias in team.aliases if alias.startswith(prefix))
        replacement = prefix + str(new_id)
        team.aliases = [replacement if alias == original else alias for alias in team.aliases]
        assert module().load_result_identities(session) == {}
        assert record(session, match_id, LATER)
        assert module().load_result_identities(session) == {match_id: LATER}
        assert record(session, match_id, "2026-11-12T12:00:00Z") is False
        rows = list(
            session.scalars(
                select(models.ResultSourceIdentity).order_by(
                    models.ResultSourceIdentity.verified_utc
                )
            )
        )
        assert [getattr(row, f"{side}_provider_binding") for row in rows] == [original, replacement]
        assert [row.verified_utc for row in rows] == [FIRST, LATER]
    archive = tmp_path / "archive"
    meta = publish(database, archive)
    assert meta["result_source_identities"]["count"] == 2
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM result_source_identities")
        module().restore_result_identities(db, archive, meta)
    with session_scope(engine) as session:
        assert module().load_result_identities(session) == {match_id: LATER}
    assert publish(database, tmp_path / "again") == meta


def test_duplicate_verification_key_cannot_replace_a_different_snapshot(seed):
    _, engine = seed
    with session_scope(engine) as session:
        record(session)
        session.get(Match, "apif:1").home_team_id = 3
        with pytest.raises(ValueError):
            record(session)
        assert len(list(session.scalars(select(models.ResultSourceIdentity)))) == 1


def test_old_database_and_old_archive_need_no_identity_table(seed, tmp_path):
    database, engine = seed
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE result_source_identities")
    with session_scope(engine) as session:
        assert module().load_result_identities(session) == {}
    with sqlite3.connect(database) as db:
        assert module().export_result_identities(db, tmp_path / "archive") is None
        module().restore_result_identities(db, tmp_path / "archive", {"files": {}})
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='result_source_identities'"
            ).fetchone()
            is None
        )
    assert module().validate_result_identities_manifest({"files": {}}) is False


def test_archive_preserves_history_after_current_identity_changes(seed, tmp_path):
    database, engine = seed
    with session_scope(engine) as session:
        record(session)
        session.get(Match, "apif:1").home_team_id = 3
        record(session, verified=LATER)
    meta = publish(database, tmp_path / "archive")
    assert meta["result_source_identities"]["count"] == 2
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM result_source_identities")
        module().restore_result_identities(db, tmp_path / "archive", meta)
    with session_scope(engine) as session:
        assert module().load_result_identities(session) == {"apif:1": LATER}
    assert publish(database, tmp_path / "again") == meta


@pytest.mark.parametrize(
    "damage",
    [
        "descriptor",
        "unlisted",
        "count",
        "columns",
        "version",
        "hash",
        "missing",
        "shape",
        "team",
        "time",
        "source",
        "type",
        "duplicate",
        "binding_source",
        "binding_league",
        "binding_number",
        "binding_type",
        "same_bindings",
    ],
)
def test_damaged_identity_archive_never_inserts_partial_evidence(seed, tmp_path, damage):
    database, engine = seed
    with session_scope(engine) as session:
        record(session)
    archive = tmp_path / "archive"
    meta = publish(database, archive)
    key = module().RESULT_IDENTITY_FILE
    target = archive / key
    if damage == "descriptor":
        del meta["result_source_identities"]
    elif damage == "unlisted":
        del meta["files"][key]
    elif damage in {"count", "columns", "version"}:
        meta["result_source_identities"][damage] = {"count": 2, "columns": [], "version": True}[
            damage
        ]
    elif damage == "hash":
        target.write_bytes(target.read_bytes() + b"\n")
    elif damage == "missing":
        target.unlink()
    else:
        rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        columns = meta["result_source_identities"]["columns"]
        if damage == "shape":
            rows[0].pop()
        elif damage == "duplicate":
            other = list(rows[0])
            other[columns.index("verified_utc")] = LATER
            rows.append(other)
            meta["result_source_identities"]["count"] = 2
        else:
            field, value = {
                "team": ("home_team_id", 999),
                "time": ("verified_utc", "2026-11-10T12:00:00"),
                "source": ("source", "jc"),
                "type": ("home_team_id", True),
                "binding_source": ("home_provider_binding", "fdorg:英超:team:11"),
                "binding_league": ("home_provider_binding", "apif:英冠:team:11"),
                "binding_number": ("home_provider_binding", "apif:英超:team:01"),
                "binding_type": ("home_provider_binding", 11),
                "same_bindings": ("home_provider_binding", "apif:英超:team:12"),
            }[damage]
            rows[0][columns.index(field)] = value
        target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        meta["files"][key] = hashlib.sha256(target.read_bytes()).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM result_source_identities")
        with pytest.raises(ValueError):
            module().restore_result_identities(db, archive, meta)
        assert db.execute("SELECT count(*) FROM result_source_identities").fetchone() == (0,)
