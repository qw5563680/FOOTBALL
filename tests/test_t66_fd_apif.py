"""Real failed-run FD rows and unchanged archived APIF evidence; all replay is offline."""

import csv
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from zucai.cloud_feeds import check_db, export_feeds, restore_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.league_registry import provider_config
from zucai.models import Match, Result, Team
from zucai.predict.backtest import read_history
from zucai.predict.pooled import linked_representatives
from zucai.sources.api_common import ApiGame
from zucai.sources.apif_ingest import import_apif_games
from zucai.sources.fd_ingest import fetch_fd, import_fd_games
from zucai.sources.fdorg_ingest import import_fdorg_games
from zucai.sources.football_data import FootballDataCoUk

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data/fixtures/t66"
SEED = json.loads((FIXTURES / "fd-apif-replay-seed.json").read_text())
FD_ALIASES = json.loads((ROOT / "data/team_aliases/football_data_co_uk.json").read_text())
APIF_ALIASES = json.loads((ROOT / "data/team_aliases/api_football.json").read_text())
LEAGUES = provider_config("fd")
SEEN = datetime(2026, 11, 14, 3, tzinfo=UTC)


@pytest.fixture(params=SEED["cases"], ids=lambda case: case["country"])
def case(request):
    return request.param


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "data/zucai.db")
    init_db(engine)
    with engine.begin() as connection:
        for table, section in SEED["tables"].items():
            columns = ",".join(section["columns"])
            marks = ",".join("?" for _ in section["columns"])
            connection.exec_driver_sql(
                f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                [tuple(row) for row in section["rows"]],
            )
    yield engine
    engine.dispose()


def fd_game(case):
    source = FootballDataCoUk(leagues=LEAGUES, div_code=case["country"])
    try:
        return source.parse((FIXTURES / case["fixture"]).read_bytes())[0]
    finally:
        source.session.close()


def ingest_fd(session, case, games=None):
    return import_fd_games(
        session,
        games if games is not None else [fd_game(case)],
        seen=SEEN,
        assume=timedelta(hours=3),
        aliases=FD_ALIASES,
        leagues=LEAGUES,
    )


def evidence(session, key):
    return (
        tuple(
            session.execute(
                select(*(c for c in Match.__table__.columns if c.name != "counterpart_id")).where(
                    Match.match_id == key
                )
            ).one()
        ),
        tuple(session.execute(select(Result.__table__).where(Result.match_id == key)).one()),
        [(t.id, t.name, list(t.aliases)) for t in session.scalars(select(Team).order_by(Team.id))],
    )


def apif_game(case):
    game = fd_game(case)
    return ApiGame(
        "apif",
        int(case["apif_id"].split(":")[1]),
        "244" if case["country"] == "FIN" else "113",
        LEAGUES[case["country"]]["league"],
        game.kickoff_utc,
        "FT",
        game.home_raw,
        game.away_raw,
        2077 if case["country"] == "FIN" else 370,
        1165 if case["country"] == "FIN" else 766,
        (6, 1),  # Later synthetic disagreement must not overwrite either stored result.
        None,
        "regular",
    )


def clone(session, original, key, **changes):
    values = {c.name: getattr(original, c.name) for c in Match.__table__.columns}
    values.update(match_id=key, source=key.split(":")[0], status="scheduled")
    values.update(changes)
    item = Match(**values)
    session.add(item)
    session.flush()
    return item


def test_real_late_fd_links_apif_without_rewriting_identity_or_result(db, case):
    with session_scope(db) as session:
        before = evidence(session, case["apif_id"])
        result = ingest_fd(session, case)
        assert result["inserted"] == 1 and result["suspected_duplicates"] == 0
        assert session.get(Match, case["apif_id"]).counterpart_id == case["fd_id"]
        assert result["linked_apif"] == 1
        link = result["apif_links"][0]
        assert link["apif_id"] == case["apif_id"]
        assert link["basis"] == (
            "exact_team_ids" if case["country"] == "FIN" else "one_opaque_apif"
        )
        assert link["fd_away_bound"] == (case["country"] == "FIN")
        assert link["apif_away_team_id"] == (239 if case["country"] == "FIN" else 984)
        assert evidence(session, case["apif_id"]) == before
        history = read_history(
            session,
            "欧战",
            "jc,fd,fdorg,apif",
            training_leagues=[LEAGUES[case["country"]]["league"]],
        )
        assert list(history.match_id) == [case["fd_id"]]
        assert history.attrs["linked_rows_excluded"] == [case["apif_id"]]


def test_linked_fd_and_apif_reimports_are_idempotent(db, case):
    with session_scope(db) as session:
        ingest_fd(session, case)
        before = evidence(session, case["apif_id"])
        for _ in range(2):
            report = import_apif_games(
                session, [apif_game(case)], seen=SEEN, assume=None, aliases=APIF_ALIASES
            )
            assert report["linked_fd"] == 1 and report["suspected_duplicates"] == 0
            report = ingest_fd(session, case)
            assert report["unchanged"] == 1 and report["linked_apif"] == 0
        assert evidence(session, case["apif_id"]) == before


@pytest.mark.parametrize("hours", [-3, 3])
def test_link_window_includes_exactly_three_hours(db, case, hours):
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        api.kickoff_utc = fd_game(case).kickoff_utc + timedelta(hours=hours)
        assert ingest_fd(session, case)["linked_apif"] == 1


def test_just_outside_link_window_does_not_create_a_relation(db, case):
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        api.kickoff_utc = fd_game(case).kickoff_utc + timedelta(hours=3, microseconds=1)
        report = ingest_fd(session, case)
        assert report["inserted"] == 1 and report["linked_apif"] == 0
        assert api.counterpart_id is None


def test_one_opaque_home_side_uses_the_bound_away_side(db):
    case = SEED["cases"][1]
    game = fd_game(case)
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        api.home_team_id, api.away_team_id = api.away_team_id, api.home_team_id
        game = replace(game, home_raw=game.away_raw, away_raw=game.home_raw)
        report = ingest_fd(session, case, [game])
        assert report["linked_apif"] == 1
        assert api.counterpart_id == "fd:SWE:20261103:halmstad:sirius"
        assert api.home_team_id == 984 and api.away_team_id == 442


def test_a_new_partial_apif_observation_without_the_stored_pointer_stays_suspected(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        ingest_fd(session, case)
        session.get(Match, case["apif_id"]).counterpart_id = None
        before = evidence(session, case["apif_id"])
        report = import_apif_games(
            session, [apif_game(case)], seen=SEEN, assume=None, aliases=APIF_ALIASES
        )
        assert report["suspected_duplicates"] == 1 and report["linked_fd"] == 0
        assert evidence(session, case["apif_id"]) == before


def test_different_scores_never_establish_identity_or_overwrite_original_results(db, case):
    with session_scope(db) as session:
        before = evidence(session, case["apif_id"])
        report = ingest_fd(session, case, [replace(fd_game(case), full=(1, 1))])
        assert report["linked_apif"] == 1
        assert evidence(session, case["apif_id"]) == before
        history = read_history(
            session, "欧战", "fd,apif", training_leagues=[LEAGUES[case["country"]]["league"]]
        )
        assert list(history.match_id) == [case["fd_id"]]
        assert list(zip(history.home_goals, history.away_goals, strict=True)) == [(1, 1)]
        assert session.get(Result, case["apif_id"]).home_goals == 2


def test_fd_fdorg_apif_can_share_one_verified_fixture_and_reimport(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        original = session.get(Match, case["apif_id"])
        fdorg = clone(session, original, "fdorg:100", result_provider="fdorg")
        assert ingest_fd(session, case)["inserted"] == 1
        assert fdorg.counterpart_id == case["fd_id"]
        assert session.get(Match, case["apif_id"]).counterpart_id == case["fd_id"]
        before = evidence(session, case["apif_id"])
        for _ in range(2):
            assert ingest_fd(session, case)["unchanged"] == 1
            assert (
                import_apif_games(
                    session, [apif_game(case)], seen=SEEN, assume=None, aliases=APIF_ALIASES
                )["linked_fd"]
                == 1
            )
            game = replace(
                apif_game(case), provider="fdorg", provider_match_id=100, status="FINISHED"
            )
            report = import_fdorg_games(
                session, [game], seen=SEEN, assume=None, aliases=APIF_ALIASES
            )
            assert report["linked_fd"] == 1, report
        assert evidence(session, case["apif_id"]) == before


@pytest.mark.parametrize(
    "conflict",
    ["reverse", "two_apif", "other_fd", "api_pointer", "fd_claimed", "api_claimed"],
)
def test_ambiguous_or_claimed_late_fd_does_not_choose_a_match(db, case, conflict):
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        if conflict == "reverse":
            api.home_team_id, api.away_team_id = api.away_team_id, api.home_team_id
        elif conflict == "two_apif":
            clone(session, api, "apif:777")
        elif conflict == "other_fd":
            clone(
                session,
                api,
                "fd:other",
                home_team_id=case["fd_home_team_id"],
                away_team_id=case["fd_away_team_id"],
            )
        elif conflict == "api_pointer":
            api.counterpart_id = "fd:elsewhere"
        else:
            clone(
                session,
                api,
                "jc:claim",
                kickoff_utc=fd_game(case).kickoff_utc - timedelta(days=10),
                counterpart_id=case["fd_id"] if conflict == "fd_claimed" else api.match_id,
            )
        session.flush()
        before = evidence(session, api.match_id)
        result = ingest_fd(session, case)
        assert result["suspected_duplicates"] == 1
        assert session.get(Match, case["fd_id"]) is None
        assert evidence(session, api.match_id) == before


def test_unbound_fd_opponent_remains_admitted_without_guessing_canonical_apif_identity(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        api.away_team_id = 409
        session.flush()
        before = evidence(session, api.match_id)
        report = ingest_fd(session, case)
        assert report["inserted"] == 1 and report["suspected_duplicates"] == 0
        assert report["linked_apif"] == 0 and api.counterpart_id is None
        assert session.get(Match, case["fd_id"]).away_team_id == 504
        assert evidence(session, api.match_id) == before


def test_bound_fd_opponent_disagreeing_with_known_apif_team_is_rejected(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        api.away_team_id = 442
        session.flush()
        before = evidence(session, api.match_id)
        report = ingest_fd(session, case)
        assert report["suspected_duplicates"] == 1 and report["inserted"] == 0
        assert api.counterpart_id is None
        assert evidence(session, api.match_id) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_incoming_fd_batch_conflict_cannot_be_resolved_by_row_order(db, case, reverse):
    game = fd_game(case)
    other = replace(game, away_raw="Other unbound club", row_number=3)
    games = [game, other] if not reverse else [other, game]
    with session_scope(db) as session:
        result = ingest_fd(session, case, games)
        # FIN's second raw opponent is unbound, so its old admission is retained.
        # It must still veto linking the first, fully identified FD row.
        assert result["suspected_duplicates"] == (1 if case["country"] == "FIN" else 2)
        assert result["inserted"] == (1 if case["country"] == "FIN" else 0)
        assert session.get(Match, case["fd_id"]) is None
        assert session.get(Match, case["apif_id"]).counterpart_id is None


def test_two_unknown_apif_sides_remain_unlinked_and_training_refuses_overlap(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        opaque = Team(name="apif:team:999")
        session.add(opaque)
        session.flush()
        session.get(Match, case["apif_id"]).home_team_id = opaque.id
        assert ingest_fd(session, case)["inserted"] == 1
        assert session.get(Match, case["apif_id"]).counterpart_id is None
        with pytest.raises(ValueError, match="跨来源重复"):
            read_history(session, "欧战", "fd,apif", training_leagues=["瑞超"])


def test_export_restore_retains_partial_link_and_rejects_identity_tampering(db, tmp_path):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        ingest_fd(session, case)
    database = Path(db.url.database)
    archive = tmp_path / "archive"
    export_feeds(database, archive, SEEN.date(), "linked")
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, restored)
    with sqlite3.connect(restored) as connection:
        assert connection.execute(
            "SELECT counterpart_id FROM matches WHERE match_id=?", (case["apif_id"],)
        ).fetchone() == (case["fd_id"],)
        connection.execute(
            "UPDATE matches SET home_team_id=409 WHERE match_id=?", (case["apif_id"],)
        )
        with pytest.raises(ValueError, match="apif|APIF"):
            check_db(connection)


@pytest.mark.parametrize(
    "tamper", ["binding", "opaque_name", "multiple_api", "multiple_fd", "pointer", "kickoff"]
)
def test_export_rechecks_partial_link_claims_and_evidence(db, tmp_path, tamper):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        ingest_fd(session, case)
        api, fd = session.get(Match, case["apif_id"]), session.get(Match, case["fd_id"])
        if tamper == "binding":
            session.get(Team, 442).aliases = []
        elif tamper == "opaque_name":
            session.get(Team, 984).name = "apif:team:unknown"
        elif tamper == "multiple_api":
            clone(session, api, "apif:777")
        elif tamper == "multiple_fd":
            clone(session, fd, "fd:other")
        elif tamper == "pointer":
            fd.counterpart_id = "jc:elsewhere"
        else:
            api.kickoff_utc = fd_game(case).kickoff_utc + timedelta(hours=4)
    with pytest.raises(ValueError, match="apif|APIF"):
        export_feeds(Path(db.url.database), tmp_path / "invalid", SEEN.date(), "invalid")


def test_restore_rejects_tampered_link_even_with_a_recomputed_file_hash(db, tmp_path):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        ingest_fd(session, case)
    archive = tmp_path / "archive"
    export_feeds(Path(db.url.database), archive, SEEN.date(), "before")
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    path = archive / "state/matches.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row[0] == case["apif_id"]:
            row[3] = 409
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest["files"]["state/matches.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    destination = tmp_path / "restored.db"
    with pytest.raises(ValueError, match="apif|APIF"):
        restore_feeds(archive, destination)
    assert not destination.exists()


def test_file_replay_persists_new_link_audit_once_and_exports_it(db, case, tmp_path):
    database = Path(db.url.database)
    audit = database.parent / "state/audit"
    kwargs = dict(
        country=case["country"],
        from_season=2026,
        to_season=2026,
        assume=timedelta(hours=3),
        db=database,
        raw_file=FIXTURES / case["fixture"],
        audit_dir=audit,
    )
    assert fetch_fd(**kwargs)["linked_apif"] == 1
    path = audit / "fd_apif_links.csv"
    before = path.read_bytes()
    assert fetch_fd(**kwargs)["linked_apif"] == 0
    assert path.read_bytes() == before
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]["apif_id"] == case["apif_id"]
    archive = tmp_path / "archive"
    manifest = export_feeds(database, archive, SEEN.date(), "audit")
    assert "state/audit/fd_apif_links.csv" in manifest["files"]
    assert (archive / "state/audit/fd_apif_links.csv").read_bytes() == before


def test_known_apif_binding_has_one_owner_across_all_teams_before_admission(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        session.add(Team(name="Competing provider identity", aliases=["apif:芬超:team:2077"]))
        report = ingest_fd(session, case)
        assert report["suspected_duplicates"] == 1
        assert report["inserted"] == report["linked_apif"] == 0
        assert session.get(Match, case["fd_id"]) is None
        assert session.get(Match, case["apif_id"]).counterpart_id is None


def test_export_rejects_a_second_owner_of_a_linked_apif_binding(db, tmp_path):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        session.add(Team(name="Competing provider identity", aliases=["apif:芬超:team:2077"]))
    with pytest.raises(ValueError, match="apif"):
        export_feeds(Path(db.url.database), tmp_path / "invalid", SEEN.date(), "invalid-owner")


def test_training_keeps_a_valid_link_with_a_scheduled_fdorg_anchor(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        sibling = clone(session, api, "fdorg:888888", result_provider="fdorg")
        assert ingest_fd(session, case)["linked_apif"] == 1
        session.get(Match, case["fd_id"]).counterpart_id = sibling.match_id
    with sqlite3.connect(db.url.database) as connection:
        check_db(connection)
    with session_scope(db) as session:
        history = read_history(session, "欧战", "jc,fd,fdorg,apif", training_leagues=["芬超"])
        assert list(history.match_id) == [case["fd_id"]]


def test_training_does_not_hide_a_scheduled_apif_competitor(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        clone(session, session.get(Match, case["apif_id"]), "apif:777777")
    with sqlite3.connect(db.url.database) as connection:
        with pytest.raises(ValueError, match="apif"):
            check_db(connection)
    with session_scope(db) as session:
        with pytest.raises(ValueError, match="apif"):
            read_history(session, "欧战", "jc,fd,fdorg,apif", training_leagues=["瑞超"])


@pytest.mark.parametrize(
    "owners,league,linked",
    [
        ([409], "瑞超", False),
        ([504], "瑞超", True),
        ([409, 504], "瑞超", False),
        ([409], "芬超", True),
    ],
)
def test_opaque_side_must_not_contradict_an_existing_league_binding(db, owners, league, linked):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        for owner in owners:
            team = session.get(Team, owner)
            team.aliases.append(f"apif:{league}:team:766")
        session.flush()
        report = ingest_fd(session, case)
        assert report["linked_apif"] == int(linked)
        assert report["suspected_duplicates"] == int(not linked)
        api = session.get(Match, case["apif_id"])
        assert api.away_team_id == 984
        assert api.counterpart_id == (case["fd_id"] if linked else None)


def test_export_rechecks_a_linked_opaque_sides_current_binding(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        team = session.get(Team, 409)
        team.aliases.append("apif:瑞超:team:766")
    with sqlite3.connect(db.url.database) as connection:
        with pytest.raises(ValueError, match="apif"):
            check_db(connection)


def test_a_filtered_frame_alone_cannot_authorize_the_new_partial_link():
    frame = pd.DataFrame(
        [
            dict(
                match_id="apif:1494327",
                source="apif",
                league="瑞超",
                home="天狼星",
                away="apif:team:766",
                kickoff_utc=pd.Timestamp("2026-11-03T18:00Z"),
                counterpart_id="fd:SWE:20261103:sirius:halmstad",
            ),
            dict(
                match_id="fd:SWE:20261103:sirius:halmstad",
                source="fd",
                league="瑞超",
                home="天狼星",
                away="Halmstad",
                kickoff_utc=pd.Timestamp("2026-11-03T18:00Z"),
                counterpart_id=None,
            ),
        ]
    )
    with pytest.raises(ValueError, match="完整数据库"):
        linked_representatives(frame)
