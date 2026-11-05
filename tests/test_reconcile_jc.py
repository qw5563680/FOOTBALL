"""T41: approved identity projection uses T40's exact stored-ID link checks."""

import csv
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import responses
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, Team
from zucai.predict.backtest import read_history
from zucai.sources import apif_reconcile, jc_counterpart

SAMPLE = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t41/reconcile_jc.json").read_bytes()
)
CONFLICT = "同联赛三小时内规范身份已有比赛"


@pytest.fixture(params=["apif", "fdorg"])
def provider(request):
    return request.param


@pytest.fixture
def make_database(tmp_path, monkeypatch, provider):
    def create(pairs=None):
        pairs = pairs or SAMPLE["pairs"][:1]
        db = tmp_path / "data/zucai.db"
        engine = get_engine(db)
        init_db(engine)
        aliases = {}
        with session_scope(engine) as session:
            session.add_all(Team(id=key, name=name) for key, name in SAMPLE["teams"])
            for pair in pairs:
                session.add(
                    Team(id=pair["temporary_id"], name=f"{provider}:team:{pair['team_id']}")
                )
                aliases.setdefault(pair["league"], {})[pair["team_id"]] = dict(
                    canonical=pair["canonical"],
                    status="approved",
                    approved_by="fixture-owner",
                    approved_utc=SAMPLE["approved_utc"],
                    approval_pr=SAMPLE["approval_pr"],
                )
            session.flush()
            for pair in pairs:
                for source in (provider, "jc"):
                    ids = [pair["home"], pair["away"]]
                    if source == "jc":
                        ids = [
                            pair["canonical_id"] if t == pair["temporary_id"] else t for t in ids
                        ]
                    key = pair["jc_id"] if source == "jc" else f"{provider}:{pair['api_id']}"
                    session.add(
                        Match(
                            match_id=key,
                            source=source,
                            league=pair["league"],
                            home_team_id=ids[0],
                            away_team_id=ids[1],
                            kickoff_utc=SAMPLE["kickoff_utc"],
                            status="finished",
                            result_provider=source,
                            result_checked_utc=SAMPLE["available_utc"],
                        )
                    )
                    session.flush()
                    score = SAMPLE["jc_score" if source == "jc" else "api_score"]
                    session.add(
                        Result(
                            match_id=key,
                            home_goals=score[0],
                            away_goals=score[1],
                            available_utc=SAMPLE["available_utc"],
                            available_source="captured",
                        )
                    )
                    session.add(
                        Odds(
                            match_id=key,
                            pool="had",
                            line=0,
                            selection="h",
                            price=2.5,
                            snapshot_utc=SAMPLE["approved_utc"],
                        )
                    )
        engine.dispose()
        for source, attr in (("apif", "ALIASES"), ("fdorg", "FDORG_ALIASES")):
            path = tmp_path / f"{source}.json"
            path.write_text(json.dumps(aliases if source == provider else {}), encoding="utf-8")
            monkeypatch.setattr(apif_reconcile, attr, path)
        return db

    return create


def audit(db, name):
    path = db.parent / f"state/audit/{name}.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


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
                if table.name != "teams"
            }
    finally:
        engine.dispose()


@pytest.mark.parametrize("hours", [-3, 0, 3])
def test_merge_links_exact_jc_and_preserves_evidence_and_repeat_bytes(
    make_database, provider, monkeypatch, hours
):
    db = make_database()
    pair = SAMPLE["pairs"][0]
    api_id = f"{provider}:{pair['api_id']}"
    engine = get_engine(db)
    with session_scope(engine) as session:
        session.get(Match, pair["jc_id"]).kickoff_utc = datetime.fromisoformat(
            SAMPLE["kickoff_utc"]
        ) + timedelta(hours=hours)
    engine.dispose()
    before = evidence(db)
    shared_rule = Mock(wraps=jc_counterpart.late_jc_candidate)
    monkeypatch.setattr(jc_counterpart, "late_jc_candidate", shared_rule)
    with responses.RequestsMock() as http:
        report = apif_reconcile.reconcile(db, "first")
        assert not http.calls
    assert report["merged_teams"] == report["rekeyed_matches"] == 1
    assert report["skipped_conflicts"] == 0
    assert shared_rule.called
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT home_team_id,away_team_id,counterpart_id FROM matches WHERE match_id=?",
            (api_id,),
        ).fetchone() == (pair["home"], pair["canonical_id"], None)
        assert con.execute(
            "SELECT counterpart_id FROM matches WHERE match_id=?", (pair["jc_id"],)
        ).fetchone() == (api_id,)
    assert evidence(db) == before
    identity = audit(db, f"{provider}_identity_merges")
    assert len(identity) == 1 and identity[0]["reason"] == "merged_and_linked"
    links = audit(db, "jc_relink")
    assert len(links) == 1
    assert (links[0]["api_id"], links[0]["jc_id"]) == (api_id, pair["jc_id"])
    assert (links[0]["outcome"], links[0]["reason"]) == ("linked", "reconcile")
    assert json.loads(links[0]["candidates"]) == [pair["jc_id"]]
    frozen = inventory(db)
    again = apif_reconcile.reconcile(db, "repeat")
    assert again["merged_teams"] == again["rekeyed_matches"] == 0
    assert inventory(db) == frozen


def add_copy(session, original, key, **changes):
    fields = {c.name: getattr(original, c.name) for c in Match.__table__.columns}
    fields.update(match_id=key, source=key.split(":")[0], counterpart_id=None)
    fields.update(changes)
    session.add(Match(**fields))


@pytest.mark.parametrize("kind", SAMPLE["conflicts"])
def test_other_conflicts_keep_original_rejection_and_database_bytes(make_database, provider, kind):
    db = make_database()
    pair = SAMPLE["pairs"][0]
    engine = get_engine(db)
    with session_scope(engine) as session:
        jc = session.get(Match, pair["jc_id"])
        api = session.get(Match, f"{provider}:{pair['api_id']}")
        if kind == "reverse":
            jc.home_team_id, jc.away_team_id = jc.away_team_id, jc.home_team_id
        elif kind == "multiple_jc":
            add_copy(session, jc, "jc:second")
        elif kind in {"jc_linked", "jc_linked_to_api"}:
            jc.counterpart_id = api.match_id if kind == "jc_linked_to_api" else "fd:elsewhere"
        elif kind == "api_linked":
            api.counterpart_id = "fd:elsewhere"
        elif kind in {"api_claimed", "jc_claimed"}:
            add_copy(
                session,
                jc,
                "fd:claim",
                kickoff_utc=SAMPLE["historical_utc"],
                counterpart_id=api.match_id if kind == "api_claimed" else jc.match_id,
            )
        elif kind == "fd_conflict":
            add_copy(session, jc, "fd:conflict")
        elif kind == "multiple_api":
            add_copy(session, api, f"{provider}:second")
        elif kind == "other_opponent":
            jc.home_team_id = SAMPLE["teams"][1][0]
        elif kind == "temporary_opponent":
            session.get(Team, api.home_team_id).name = f"{provider}:team:99999"
        elif kind == "historical_conflict":
            # A linkable current game cannot make this league/identity partially mutate.
            add_copy(session, api, f"{provider}:history", kickoff_utc=SAMPLE["historical_utc"])
            add_copy(session, jc, "fd:history", kickoff_utc=SAMPLE["historical_utc"])
    engine.dispose()
    before = db.read_bytes()
    report = apif_reconcile.reconcile(db, kind)
    assert report["merged_teams"] == report["rekeyed_matches"] == 0
    assert report["skipped_conflicts"] == 1
    assert db.read_bytes() == before
    assert audit(db, f"{provider}_identity_merges")[0]["reason"] == CONFLICT
    assert not (db.parent / "state/audit/jc_relink.csv").exists()


def test_background_three_pairs_pass_unchanged_pooled_guard_after_merge(make_database, provider):
    db = make_database(SAMPLE["pairs"])
    scope = sorted({p["league"] for p in SAMPLE["pairs"]})
    engine = get_engine(db)
    with session_scope(engine) as session, pytest.raises(ValueError, match="重复|重叠"):
        read_history(session, source=f"jc,{provider}", training_leagues=scope)
    assert apif_reconcile.reconcile(db, "three-pairs")["rekeyed_matches"] == 3
    with session_scope(engine) as session:
        frame = read_history(session, source=f"jc,{provider}", training_leagues=scope)
    engine.dispose()
    assert set(frame.match_id) == {p["jc_id"] for p in SAMPLE["pairs"]}
    assert frame.attrs["linked_rows_excluded"] == sorted(
        f"{provider}:{p['api_id']}" for p in SAMPLE["pairs"]
    )
    assert (
        list(zip(frame.home_goals, frame.away_goals, strict=True))
        == [tuple(SAMPLE["jc_score"])] * 3
    )
    assert len(audit(db, "jc_relink")) == 3


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("existing_audits", [False, True])
def test_link_and_identity_audits_rollback_together(
    make_database, provider, monkeypatch, failure, existing_audits
):
    db = make_database()
    if existing_audits:
        for name, fields in (
            (f"{provider}_identity_merges", apif_reconcile.FIELDS),
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
            raise OSError("synthetic audit write failure")

        monkeypatch.setattr(jc_counterpart, "_write_audit", fail)
        with pytest.raises(OSError, match="synthetic"):
            apif_reconcile.reconcile(db, "failed")
    else:

        def short_timeout(path):
            engine = get_engine(path)

            @event.listens_for(engine, "connect")
            def timeout(connection, _record):
                connection.execute("PRAGMA busy_timeout=1")

            return engine

        monkeypatch.setattr(apif_reconcile, "get_engine", short_timeout)
        with sqlite3.connect(db) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM teams").fetchall()
            with pytest.raises(OperationalError, match="database is locked"):
                apif_reconcile.reconcile(db, "failed")
            reader.rollback()
    assert inventory(db) == before
