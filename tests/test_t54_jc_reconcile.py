"""T54 narrowly repairs the approved JC identity, with all evidence retained."""

import csv
import importlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, Team
from zucai.sources import jc_counterpart

KICKOFF = "2026-11-12T19:45:00Z"
BINDING = "apif:欧国联:team:778"
APPROVED = {
    "欧国联": {
        "阿尔巴尼": {
            "canonical": "阿尔巴尼亚",
            "status": "approved",
            "approval_source": "docs/tasks/T54-jc-short-name-duplicate-team.md",
            "evidence": [
                {
                    "jc_match_id": "jc:2042694",
                    "provider_match_id": "apif:1528963",
                    "kickoff_utc": KICKOFF,
                    "league": "欧国联",
                    "side": "home",
                    "raw_team_id": 1156,
                    "canonical_team_id": 1149,
                    "canonical_binding": BINDING,
                    "opponent_team_id": 1148,
                }
            ],
        }
    }
}


@pytest.fixture
def sample(tmp_path):
    db = tmp_path / "data/zucai.db"
    engine = get_engine(db)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(
            [
                Team(id=1148, name="芬兰", aliases=["apif:欧国联:team:1099"]),
                Team(id=1149, name="阿尔巴尼亚", aliases=[BINDING]),
                Team(id=1156, name="阿尔巴尼", aliases=[]),
                Team(id=1200, name="另一球队", aliases=[]),
            ]
        )
        session.flush()
        for match_id, source, home in (
            ("jc:2042694", "jc", 1156),
            ("apif:1528963", "apif", 1149),
            ("jc:history", "jc", 1156),
        ):
            session.add(
                Match(
                    match_id=match_id,
                    source=source,
                    league="欧国联",
                    home_team_id=home,
                    away_team_id=1148,
                    kickoff_utc=("2026-10-12T19:45:00Z" if match_id == "jc:history" else KICKOFF),
                    status="finished",
                    result_provider=source,
                    result_checked_utc="2026-11-12T22:00:00Z",
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=match_id,
                    home_goals=1,
                    away_goals=0,
                    available_utc="2026-11-12T22:00:00Z",
                    available_source="captured",
                )
            )
            session.add(
                Odds(
                    match_id=match_id,
                    pool="had",
                    selection="h",
                    price=2.5,
                    snapshot_utc="2026-11-12T10:00:00Z",
                )
            )
    engine.dispose()
    aliases = tmp_path / "sporttery.json"
    aliases.write_text(json.dumps(APPROVED, ensure_ascii=False), encoding="utf-8")
    frozen = db.parent / "forecasts/forecast.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(b'{"immutable":"original forecast"}\n')
    return db, aliases


def reconcile(sample, run_id="first"):
    module = importlib.import_module("zucai.sources.jc_identity")
    return module.reconcile(sample[0], run_id, aliases_path=sample[1])


def inventory(db):
    return {p.relative_to(db.parent): p.read_bytes() for p in db.parent.rglob("*") if p.is_file()}


def evidence(db):
    engine = get_engine(db)
    try:
        with session_scope(engine) as session:
            return {
                table.name: list(
                    session.execute(
                        select(
                            *[
                                c
                                for c in table.columns
                                if not (
                                    table.name == "matches"
                                    and c.name in {"home_team_id", "away_team_id", "counterpart_id"}
                                )
                            ]
                        )
                    )
                )
                for table in Match.metadata.sorted_tables
            }
    finally:
        engine.dispose()


def read_audit(db, name):
    with (db.parent / f"state/audit/{name}.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_approved_rebind_preserves_evidence_history_and_repeat_bytes(sample, monkeypatch):
    db, _aliases = sample
    before = evidence(db)
    shared = Mock(wraps=jc_counterpart.late_jc_candidate)
    monkeypatch.setattr(jc_counterpart, "late_jc_candidate", shared)
    report = reconcile(sample)
    assert report["status"] == "complete"
    assert report["requests"] == report["skipped_conflicts"] == 0
    assert report["rekeyed_matches"] == report["linked_matches"] == 1
    assert shared.called
    assert evidence(db) == before
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT home_team_id,away_team_id,counterpart_id FROM matches WHERE match_id=?",
            ("jc:2042694",),
        ).fetchone() == (1149, 1148, "apif:1528963")
        assert con.execute(
            "SELECT home_team_id FROM matches WHERE match_id='jc:history'"
        ).fetchone() == (1156,)
        assert con.execute("SELECT name FROM teams WHERE id=1156").fetchone() == ("阿尔巴尼",)
    audit = read_audit(db, "jc_identity_merges")
    assert len(audit) == 1
    assert audit[0]["reason"] == "rebound_and_linked"
    assert json.loads(audit[0]["alias_source"])["entry"] == APPROVED["欧国联"]["阿尔巴尼"]
    assert read_audit(db, "jc_relink")[0]["reason"] == "jc_identity_reconcile"
    frozen = inventory(db)
    repeated = reconcile(sample, "repeat")
    assert repeated["rekeyed_matches"] == repeated["linked_matches"] == 0
    assert repeated["already_reconciled"] == 1
    assert inventory(db) == frozen


def add_copy(session, original, key, **changes):
    fields = {c.name: getattr(original, c.name) for c in Match.__table__.columns}
    fields.update(match_id=key, source=key.split(":")[0], counterpart_id=None)
    fields.update(changes)
    session.add(Match(**fields))


@pytest.mark.parametrize(
    "kind",
    [
        "reverse",
        "other_opponent",
        "jc_league",
        "api_league",
        "jc_time",
        "api_time",
        "multiple_jc",
        "multiple_api",
        "jc_linked",
        "api_linked",
        "jc_claimed",
        "api_claimed",
        "raw_alias",
        "raw_name",
        "canonical_name",
        "canonical_alias_owner",
        "raw_alias_owner",
        "binding_owner",
        "binding_missing",
        "binding_conflict",
        "canonical_provisional",
        "fd_conflict",
    ],
)
def test_identity_and_counterpart_conflicts_do_not_mutate_database(sample, kind):
    db, _aliases = sample
    engine = get_engine(db)
    with session_scope(engine) as session:
        jc = session.get(Match, "jc:2042694")
        api = session.get(Match, "apif:1528963")
        canonical = session.get(Team, 1149)
        raw_team = session.get(Team, 1156)
        other_team = session.get(Team, 1200)
        if kind == "reverse":
            jc.home_team_id, jc.away_team_id = jc.away_team_id, jc.home_team_id
        elif kind == "other_opponent":
            jc.away_team_id = 1200
        elif kind in {"jc_league", "api_league"}:
            (jc if kind == "jc_league" else api).league = "欧洲杯"
        elif kind in {"jc_time", "api_time"}:
            (jc if kind == "jc_time" else api).kickoff_utc = datetime.fromisoformat(
                KICKOFF
            ) + timedelta(seconds=1)
        elif kind == "multiple_jc":
            add_copy(session, jc, "jc:other")
        elif kind == "multiple_api":
            add_copy(session, api, "apif:other")
        elif kind == "fd_conflict":
            add_copy(session, api, "fd:other")
        elif kind in {"jc_linked", "api_linked"}:
            (jc if kind == "jc_linked" else api).counterpart_id = "fd:elsewhere"
        elif kind in {"jc_claimed", "api_claimed"}:
            add_copy(
                session,
                jc,
                "fd:claim",
                kickoff_utc="2020-01-01T12:00:00Z",
                counterpart_id=jc.match_id if kind == "jc_claimed" else api.match_id,
            )
        elif kind == "raw_alias":
            raw_team.aliases.append("some-binding")
        elif kind == "raw_name":
            raw_team.name = "并非阿尔巴尼"
        elif kind == "canonical_name":
            canonical.name = "并非阿尔巴尼亚"
        elif kind == "canonical_alias_owner":
            other_team.aliases.append("阿尔巴尼亚")
        elif kind == "raw_alias_owner":
            other_team.aliases.append("阿尔巴尼")
        elif kind == "binding_owner":
            other_team.aliases.append(BINDING)
        elif kind == "binding_missing":
            canonical.aliases = []
        elif kind == "binding_conflict":
            canonical.aliases.append("apif:欧国联:team:999")
        elif kind == "canonical_provisional":
            session.get(Team, 1148).name = "apif:team:1099"
    engine.dispose()
    before = db.read_bytes()
    report = reconcile(sample, kind)
    assert report["status"] == "partial"
    assert report["rekeyed_matches"] == report["linked_matches"] == 0
    assert report["skipped_conflicts"] == 1
    assert db.read_bytes() == before
    assert read_audit(db, "jc_identity_merges")[0]["reason"]
    assert not (db.parent / "state/audit/jc_relink.csv").exists()


@pytest.mark.parametrize("kind", ["proposed", "unlisted", "no_evidence"])
def test_only_explicit_approved_evidence_is_repaired(sample, kind):
    db, path = sample
    aliases = deepcopy(APPROVED)
    if kind == "proposed":
        aliases["欧国联"]["阿尔巴尼"]["status"] = "proposed"
    elif kind == "unlisted":
        aliases = {}
    else:
        aliases["欧国联"]["阿尔巴尼"]["evidence"] = []
    path.write_text(json.dumps(aliases, ensure_ascii=False), encoding="utf-8")
    before = inventory(db)
    report = reconcile(sample)
    assert report["rekeyed_matches"] == report["linked_matches"] == 0
    assert inventory(db) == before


@pytest.mark.parametrize("kind", ["missing_side", "duplicate", "league", "time", "binding"])
def test_malformed_explicit_repair_proof_is_rejected_before_writes(sample, kind):
    db, path = sample
    aliases = deepcopy(APPROVED)
    proofs = aliases["欧国联"]["阿尔巴尼"]["evidence"]
    if kind == "missing_side":
        del proofs[0]["side"]
    elif kind == "duplicate":
        proofs.append(deepcopy(proofs[0]))
    elif kind == "league":
        proofs[0]["league"] = "欧洲杯"
    elif kind == "time":
        proofs[0]["kickoff_utc"] = "2026-11-12T19:45:00"
    elif kind == "binding":
        proofs[0]["canonical_binding"] = "apif:欧洲杯:team:778"
    path.write_text(json.dumps(aliases, ensure_ascii=False), encoding="utf-8")
    before = inventory(db)
    with pytest.raises(ValueError):
        reconcile(sample)
    assert inventory(db) == before


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("existing_audits", [False, True])
def test_identity_and_link_audit_roll_back_with_transaction(
    sample, monkeypatch, failure, existing_audits
):
    module = importlib.import_module("zucai.sources.jc_identity")
    db, _aliases = sample
    if existing_audits:
        for name, fields in (
            ("jc_identity_merges", module.FIELDS),
            ("jc_relink", jc_counterpart.FIELDS),
        ):
            path = db.parent / f"state/audit/{name}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(",".join(fields) + "\n", encoding="utf-8")
    before = inventory(db)
    if failure == "audit":
        original = jc_counterpart._write_audit

        def fail(path, rows):
            original(path, rows)
            raise OSError("synthetic audit failure")

        monkeypatch.setattr(jc_counterpart, "_write_audit", fail)
        with pytest.raises(OSError, match="synthetic"):
            reconcile(sample)
    else:

        def short_timeout(path):
            engine = get_engine(path)

            @event.listens_for(engine, "connect")
            def timeout(connection, _record):
                connection.execute("PRAGMA busy_timeout=1")

            return engine

        monkeypatch.setattr(module, "get_engine", short_timeout)
        with sqlite3.connect(db) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM teams").fetchall()
            with pytest.raises(OperationalError, match="database is locked"):
                reconcile(sample)
            reader.rollback()
    assert inventory(db) == before
