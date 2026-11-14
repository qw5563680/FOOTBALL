"""Synthetic T10 retained results must remain compatible with the old European pool."""

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy import select

from test_t63_apif_retention import SAMPLE, SEEN, captured_game
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.backtest import read_history
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.pooled import select_training_history
from zucai.result_source_links import load_result_links
from zucai.sources.apif_ingest import import_apif_games


@pytest.fixture
def database(tmp_path):
    engine = get_engine(tmp_path / "pool.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(name="北港"), Team(name="东岸")])
    yield engine
    engine.dispose()


def _free(session, source, *, score=(2, 1), arrived=None, key=None, kickoff=None):
    teams = {team.name: team for team in session.scalars(select(Team))}
    when = kickoff or captured_game().kickoff_utc
    row = Match(
        match_id=key or f"{source}:retained",
        source=source,
        league="英超",
        home_team_id=teams["北港"].id,
        away_team_id=teams["东岸"].id,
        kickoff_utc=when,
        status="finished",
    )
    session.add(row)
    session.flush()
    session.add(
        Result(
            match_id=row.match_id,
            home_goals=score[0],
            away_goals=score[1],
            available_utc=arrived or when + timedelta(hours=3),
            available_source="captured",
        )
    )
    session.flush()
    return row


def _support(session):
    for index in range(6):
        _free(
            session,
            "fd",
            key=f"fd:earlier:{index}",
            score=(index % 3, (index + 1) % 2),
            kickoff=captured_game().kickoff_utc - timedelta(days=7 * (index + 1)),
        )


def _ingest(session, when=SEEN):
    game = replace(captured_game(), league="英超", competition_code="39")
    return import_apif_games(
        session,
        [game],
        seen=when,
        imported_utc=when,
        assume=None,
        aliases={"英超": SAMPLE["aliases"]["日职"]},
        retain_results=True,
    )


def _pool(session):
    return read_history(session, "欧战", "fd,jc,fdorg,apif", training_leagues=["英超"])


@pytest.mark.parametrize("source", ["fd", "jc"])
@pytest.mark.parametrize("score", [(2, 1), (0, 2)])
def test_retained_paid_result_does_not_break_european_primary(database, source, score, tmp_path):
    from zucai.source_results_audit import audit_source_results

    with session_scope(database) as session:
        _support(session)
        incumbent = _free(session, source, score=score)
        before = _pool(session)
        old = session.get(Result, incumbent.match_id)
        snapshot = old.home_goals, old.away_goals, old.available_utc, incumbent.counterpart_id
        assert _ingest(session)["inserted"] == 1
        assert len(load_result_links(session)) == 1
        assert incumbent.counterpart_id is None
        frame = _pool(session)
        for cutoff in (SEEN, SEEN + timedelta(minutes=1)):
            selected = select_training_history(frame, cutoff)
            assert set(selected.match_id) == set(before.match_id)
        current = DixonColes(pooled=True).fit(frame, as_of=SEEN + timedelta(minutes=1))
        baseline = DixonColes(pooled=True).fit(before, as_of=SEEN + timedelta(minutes=1))
        np.testing.assert_array_equal(current.engine._params, baseline.engine._params)
        assert (
            old.home_goals,
            old.away_goals,
            old.available_utc,
            incumbent.counterpart_id,
        ) == snapshot
        paid = session.get(Result, "apif:91001")
        assert (paid.home_goals, paid.away_goals) == (2, 1)
    audit = audit_source_results(database.url.database, tmp_path / "retained-audit.csv")
    assert audit["matched"] == 1
    assert audit["conflicts"] == int(score != (2, 1))


@pytest.mark.parametrize("source", ["fd", "jc"])
def test_paid_first_pool_waits_for_actual_relationship_clock(database, source):
    with session_scope(database) as session:
        _support(session)
        _ingest(session)
        _free(session, source, arrived=SEEN + timedelta(hours=1))
        _ingest(session, SEEN + timedelta(hours=2))
        frame = _pool(session)
        early = select_training_history(frame, SEEN + timedelta(minutes=30))
        assert "apif:91001" in set(early.match_id)
        assert f"{source}:retained" not in set(early.match_id)
        for cutoff in (SEEN + timedelta(minutes=90), SEEN + timedelta(hours=2)):
            with pytest.raises(ValueError, match="重复|重叠"):
                select_training_history(frame, cutoff)
        late = select_training_history(frame, SEEN + timedelta(hours=2, seconds=1))
        assert "apif:91001" not in set(late.match_id)
        assert f"{source}:retained" in set(late.match_id)


def test_new_link_can_reach_the_legacy_representative_through_a_removed_fd_anchor(database):
    from sqlalchemy import delete

    from zucai.models import ResultSourceLink

    with session_scope(database) as session:
        _support(session)
        fd = _free(session, "fd")
        jc = _free(session, "jc")
        jc.counterpart_id = fd.match_id
        before = _pool(session)
        assert fd.match_id not in set(before.match_id)
        _ingest(session)
        # Deliberately retain only the validated paid -> FD proof. The old
        # JC -> FD edge must reach the original representative without retiming it.
        session.execute(
            delete(ResultSourceLink).where(ResultSourceLink.counterpart_id == jc.match_id)
        )
        frame = _pool(session)
        assert set(select_training_history(frame, SEEN + timedelta(seconds=1)).match_id) == set(
            before.match_id
        )
        assert jc.counterpart_id == fd.match_id


def test_expired_link_snapshot_cannot_authorize_pool_deduplication(database):
    with session_scope(database) as session:
        _support(session)
        _free(session, "fd")
        _ingest(session)
        paid = session.get(Match, "apif:91001")
        paid.kickoff_utc = captured_game().kickoff_utc + timedelta(minutes=1)
        session.flush()
        assert not load_result_links(session)
        with pytest.raises(ValueError, match="重复|重叠"):
            _pool(session)
