"""Codex internal review probes; not Claude approval and not product changes."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from test_t66_fd_apif import APIF_ALIASES, SEED, SEEN, apif_game, clone, ingest_fd
from test_t66_fd_apif import db as db
from zucai.cloud_feeds import check_db, export_feeds
from zucai.db import session_scope
from zucai.models import Match, Team
from zucai.predict.backtest import read_history
from zucai.sources.apif_ingest import import_apif_games


def test_existing_apif_import_already_rejects_competing_binding_owner(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        session.add(Team(name="Competing APIF identity", aliases=["apif:芬超:team:2077"]))
        session.flush()
        report = import_apif_games(
            session, [apif_game(case)], seen=SEEN, assume=None, aliases=APIF_ALIASES
        )
        assert report["rejected_rows"] == 1
        assert "多个既有身份" in report["rejected"][0]["reason"]


def test_fd_admission_rejects_competing_owner_of_known_apif_binding(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        session.add(Team(name="Competing APIF identity", aliases=["apif:芬超:team:2077"]))
        session.flush()
        report = ingest_fd(session, case)
        assert report["suspected_duplicates"] == 1, report
        assert report["inserted"] == report["linked_apif"] == 0
        assert session.get(Match, case["fd_id"]) is None
        assert session.get(Match, case["apif_id"]).counterpart_id is None


def test_export_rejects_competing_owner_of_linked_apif_binding(db, tmp_path):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        session.add(Team(name="Competing APIF identity", aliases=["apif:芬超:team:2077"]))
    with pytest.raises(ValueError, match="apif|APIF|来源|绑定"):
        export_feeds(
            Path(db.url.database), tmp_path / "archive", date(2026, 11, 14), "invalid-alias-owner"
        )


def test_training_accepts_export_valid_fdorg_anchor_omitted_by_finished_filter(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        sibling = clone(session, api, "fdorg:888888", result_provider="fdorg")
        assert ingest_fd(session, case)["linked_apif"] == 1
        fd = session.get(Match, case["fd_id"])
        fd.counterpart_id = sibling.match_id
    with sqlite3.connect(db.url.database) as conn:
        check_db(conn)
    with session_scope(db) as session:
        history = read_history(session, "欧战", "jc,fd,fdorg,apif", training_leagues=["芬超"])
        assert list(history.match_id) == [case["fd_id"]]


def test_training_rejects_scheduled_competitor_hidden_by_finished_filter(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        clone(session, session.get(Match, case["apif_id"]), "apif:777777")
    with sqlite3.connect(db.url.database) as conn:
        with pytest.raises(ValueError, match="apif|APIF"):
            check_db(conn)
    with session_scope(db) as session:
        with pytest.raises(ValueError, match="apif|APIF|跨来源|候选"):
            read_history(session, "欧战", "jc,fd,fdorg,apif", training_leagues=["瑞超"])


@pytest.mark.parametrize(
    "owners,league,expected_link",
    [([409], "瑞超", False), ([504], "瑞超", True), ([409, 504], "瑞超", False), ([409], "芬超", True)],
)
def test_opaque_apif_side_respects_existing_league_binding(db, owners, league, expected_link):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        for owner in owners:
            team = session.get(Team, owner)
            team.aliases.append(f"apif:{league}:team:766")
        session.flush()
        for owner in owners:
            assert f"apif:{league}:team:766" in session.get(Team, owner).aliases
        report = ingest_fd(session, case)
        assert report["linked_apif"] == int(expected_link), report
        assert report["suspected_duplicates"] == int(not expected_link), report
        assert session.get(Match, case["apif_id"]).away_team_id == 984
        assert session.get(Match, case["apif_id"]).counterpart_id == (
            case["fd_id"] if expected_link else None
        )


def test_export_rejects_contrary_binding_for_linked_opaque_side(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        team = session.get(Team, 409)
        team.aliases.append("apif:瑞超:team:766")
    with sqlite3.connect(db.url.database) as conn:
        assert "apif:瑞超:team:766" in __import__("json").loads(
            conn.execute("SELECT aliases FROM teams WHERE id=409").fetchone()[0]
        )
        with pytest.raises(ValueError, match="apif|APIF|来源|绑定"):
            check_db(conn)
