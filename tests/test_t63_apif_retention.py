"""Independent paid scores use the pre-existing, synthetic T10 capture."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.sources.api_common import ApiGame
from zucai.sources.apif_ingest import import_apif_games

SAMPLE = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t10/synthetic_ingest.json").read_text()
)
SEEN = datetime(2026, 10, 12, 10, tzinfo=UTC)


def captured_game():
    values = SAMPLE["fixture"].copy()
    values["kickoff_utc"] = datetime.fromisoformat(values["kickoff_utc"])
    for field in ("full", "half"):
        values[field] = tuple(values[field])
    return ApiGame(**values)


@pytest.fixture
def database(tmp_path):
    engine = get_engine(tmp_path / "scores.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(name="北港"), Team(name="东岸")])
    yield engine
    engine.dispose()


def add_old(session, source, *, kickoff=None, score=(0, 2)):
    teams = {team.name: team for team in session.scalars(select(Team))}
    when = kickoff or captured_game().kickoff_utc
    match = Match(
        match_id=f"{source}:retained",
        source=source,
        league="日职",
        home_team_id=teams["北港"].id,
        away_team_id=teams["东岸"].id,
        kickoff_utc=when,
        status="finished",
        result_checked_utc=SEEN,
    )
    session.add(match)
    session.flush()
    session.add(
        Result(
            match_id=match.match_id,
            home_goals=score[0],
            away_goals=score[1],
            available_utc=when + timedelta(hours=3),
            available_source="assumed",
        )
    )
    session.flush()
    return match


def ingest(session, seen=SEEN):
    return import_apif_games(
        session,
        [captured_game()],
        seen=seen,
        assume=timedelta(hours=3),
        aliases=SAMPLE["aliases"],
        retain_results=True,
        imported_utc=seen,
    )


@pytest.mark.parametrize("source", ["fd", "jc", "fdorg"])
def test_new_paid_result_is_retained_without_overwriting_the_old_score_or_clock(database, source):
    with session_scope(database) as session:
        incumbent = add_old(session, source)
        old = session.get(Result, incumbent.match_id)
        before = (
            old.home_goals,
            old.away_goals,
            old.available_utc,
            old.available_source,
            incumbent.counterpart_id,
            incumbent.result_checked_utc,
        )
        result = ingest(session)
        paid = session.get(Result, "apif:91001")
        assert result["inserted"] == 1
        assert (paid.home_goals, paid.away_goals) == (2, 1)
        assert paid.available_source == "captured"
        assert datetime.fromisoformat(paid.available_utc) == SEEN
        assert (
            old.home_goals,
            old.away_goals,
            old.available_utc,
            old.available_source,
            incumbent.counterpart_id,
            incumbent.result_checked_utc,
        ) == before
        assert ingest(session, SEEN + timedelta(hours=1))["unchanged"] == 1
        assert datetime.fromisoformat(paid.available_utc) == SEEN


def test_all_four_sources_link_without_overwriting_legacy_counterparts(database):
    from zucai.result_source_links import load_result_links

    with session_scope(database) as session:
        fd = add_old(session, "fd")
        fdorg = add_old(session, "fdorg")
        jc = add_old(session, "jc")
        fdorg.counterpart_id = fd.match_id
        jc.counterpart_id = fd.match_id
        assert ingest(session)["inserted"] == 1
        assert {(left, right) for left, right, _ in load_result_links(session)} == {
            ("apif:91001", match.match_id) for match in (fd, fdorg, jc)
        }
        assert fdorg.counterpart_id == fd.match_id and jc.counterpart_id == fd.match_id


def test_later_fd_capture_can_link_preexisting_paid_result_without_rewriting_its_time(database):
    from zucai.result_source_links import load_result_links

    with session_scope(database) as session:
        assert ingest(session)["inserted"] == 1
        add_old(session, "fd")
        assert ingest(session, SEEN + timedelta(hours=1))["unchanged"] == 1
        assert len(load_result_links(session)) == 1
        assert datetime.fromisoformat(session.get(Result, "apif:91001").available_utc) == SEEN


def test_ambiguous_lower_priority_match_cannot_become_a_retained_result_link(database):
    with session_scope(database) as session:
        add_old(session, "jc")
        fd = add_old(session, "fd")
        session.add(
            Match(
                match_id="fd:second",
                source="fd",
                league=fd.league,
                home_team_id=fd.home_team_id,
                away_team_id=fd.away_team_id,
                kickoff_utc=fd.kickoff_utc,
                status="scheduled",
            )
        )
        result = ingest(session)
        assert result["suspected_duplicates"] == 1
        assert session.get(Result, "apif:91001") is None


def test_missing_historical_identity_is_excluded_before_overlap_precheck(database):
    from sqlalchemy import delete

    from zucai.models import ResultSourceIdentity
    from zucai.predict.backtest import read_history
    from zucai.predict.pooled import select_training_history
    from zucai.result_source_identity import record_result_identity

    with session_scope(database) as session:
        ingest(session)
        add_old(session, "fd")
        # A legacy normalized snapshot has current bindings but no dated proof.
        session.execute(delete(ResultSourceIdentity))
        frame = read_history(session, "日职", "fd,apif")
        selected = select_training_history(frame, "2026-11-14T00:00Z")
        assert set(selected.match_id) == {"fd:retained"}
        record_result_identity(session, session.get(Match, "apif:91001"), SEEN)
        with pytest.raises(ValueError, match="重复|重叠"):
            read_history(session, "日职", "fd,apif")


def test_daily_late_official_link_is_timestamped_without_any_result_mutation(database):
    from zucai.result_source_links import load_result_links
    from zucai.source_result_reconcile import reconcile_source_results

    with session_scope(database) as session:
        ingest(session)
        jc = add_old(session, "jc")
        stored = session.get(Result, jc.match_id)
        before = (stored.home_goals, stored.away_goals, stored.available_utc)
    when = SEEN + timedelta(hours=2)
    report = reconcile_source_results(database.url.database, when)
    assert report["linked"] == 1
    with session_scope(database) as session:
        assert datetime.fromisoformat(load_result_links(session)[0][2]) == when
        stored = session.get(Result, "jc:retained")
        assert (stored.home_goals, stored.away_goals, stored.available_utc) == before
    assert reconcile_source_results(database.url.database, when + timedelta(hours=1))["linked"] == 0


def test_feed_archive_roundtrip_preserves_proofs_and_old_restore_cannot_keep_late_state(
    database, tmp_path
):
    import sqlite3
    from datetime import date

    from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
    from zucai.result_source_identity import RESULT_IDENTITY_FILE
    from zucai.result_source_links import RESULT_LINK_FILE

    old, new = tmp_path / "old", tmp_path / "new"
    source = Path(database.url.database)
    export_feeds(source, old, date(2026, 10, 12), "synthetic-before-proof")
    with session_scope(database) as session:
        add_old(session, "fd")
        ingest(session)
    export_feeds(source, new, date(2026, 10, 12), "synthetic-with-proof")
    info = archive_info(new)
    assert {RESULT_LINK_FILE, RESULT_IDENTITY_FILE} <= set(info["files"])
    destination = tmp_path / "reuse" / "feeds.db"
    restore_feeds(new, destination)
    with sqlite3.connect(destination) as connection:
        assert connection.execute("select count(*) from result_source_links").fetchone()[0] == 1
        assert (
            connection.execute("select count(*) from result_source_identities").fetchone()[0] == 1
        )
    # A reused working directory may also retain exports from a later run.
    for name in (RESULT_LINK_FILE, RESULT_IDENTITY_FILE):
        stale = destination.parent / name
        stale.parent.mkdir(exist_ok=True)
        stale.write_bytes((new / name).read_bytes())
    # Files left in state/ are never authoritative over the restored DB.
    stale_link = destination.parent / RESULT_LINK_FILE
    stale_link.write_text("not the database export\n")
    roundtrip = tmp_path / "roundtrip"
    export_feeds(destination, roundtrip, date(2026, 10, 12), "synthetic-db-authority")
    assert (roundtrip / RESULT_LINK_FILE).read_bytes() == (new / RESULT_LINK_FILE).read_bytes()
    assert (roundtrip / RESULT_IDENTITY_FILE).read_bytes() == (
        new / RESULT_IDENTITY_FILE
    ).read_bytes()
    restore_feeds(roundtrip, tmp_path / "roundtrip-restored.db")
    restore_feeds(old, destination)
    with sqlite3.connect(destination) as connection:
        assert connection.execute("select count(*) from result_source_links").fetchone()[0] == 0
        assert (
            connection.execute("select count(*) from result_source_identities").fetchone()[0] == 0
        )
    assert not (destination.parent / RESULT_LINK_FILE).exists()
    assert not (destination.parent / RESULT_IDENTITY_FILE).exists()
