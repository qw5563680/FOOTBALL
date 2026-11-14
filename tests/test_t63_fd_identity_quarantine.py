"""Real saved MLS rows; conservative exclusion never invents a team binding."""

import copy
import csv
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, ResultSourceLink, Team
from zucai.predict.backtest import read_history
from zucai.predict.pooled import select_training_history
from zucai.source_result_reconcile import reconcile_source_results
from zucai.source_results_audit import audit_source_results

SEED = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t63/fd-unmapped-overlap.json").read_text()
)
CHECKED = pd.Timestamp("2026-11-14T06:29:00Z")
CUTOFF = pd.Timestamp("2026-11-14T06:30:00Z")


@pytest.fixture
def db(tmp_path):
    engine = get_engine(tmp_path / "saved-mls.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**copy.deepcopy(row)) for row in SEED["teams"])
        session.flush()
        session.add_all(Match(**row) for row in SEED["matches"])
        session.flush()
        session.add_all(Result(**row) for row in SEED["results"])
    reconcile_source_results(engine.url.database, CHECKED.to_pydatetime())
    yield engine
    engine.dispose()


def history(session):
    return read_history(session, "美职", "fd,jc,apif")


def test_real_unmapped_fd_overlap_keeps_old_rows_without_claiming_a_match(db):
    with session_scope(db) as session:
        before = {
            table.__tablename__: [
                dict(r) for r in session.execute(select(table.__table__)).mappings()
            ]
            for table in (Team, Match, Result)
        }
        frame = history(session)
        selected = select_training_history(frame, CUTOFF)
        assert set(selected.match_id) == {
            r["match_id"] for r in SEED["matches"] if r["source"] == "fd"
        }
        held = selected.attrs["unverified_fd_identity_overlaps"]
        assert {r["match_id"] for r in held} == {"apif:1490585", "apif:1490596"}
        assert {r["reason"] for r in held} == {"unverified_fd_identity_overlap"}
        assert not list(session.scalars(select(ResultSourceLink)))
        for table in (Team, Match, Result):
            assert before[table.__tablename__] == [
                dict(r) for r in session.execute(select(table.__table__)).mappings()
            ]
        assert len(select_training_history(read_history(session, "美职", "fd,jc"), CUTOFF)) == 2


def test_future_fd_result_does_not_remove_the_current_paid_observation(db):
    with session_scope(db) as session:
        for result in session.scalars(select(Result).where(Result.match_id.like("fd:%"))):
            result.available_utc = CUTOFF.isoformat()
            result.available_source = "captured"
        frame = history(session)
        assert set(select_training_history(frame, CUTOFF).source) == {"apif"}
        assert set(select_training_history(frame, CUTOFF + pd.Timedelta(seconds=1)).source) == {
            "fd"
        }


@pytest.mark.parametrize(
    "case",
    [
        "bound_other_team",
        "malformed_binding",
        "multiple_bindings",
        "reverse",
        "scheduled_competitor",
        "remote_claim",
    ],
)
def test_unresolved_conflicts_still_fail_closed(db, case):
    with session_scope(db) as session:
        old = session.get(Match, "fd:USA:20261031:fc-cincinnati:philadelphia-union")
        if case in {"bound_other_team", "malformed_binding", "multiple_bindings"}:
            team = session.get(Team, old.away_team_id)
            team.aliases = {
                "bound_other_team": ["apif:美职:team:99999"],
                "malformed_binding": ["apif:美职:team:bad"],
                "multiple_bindings": ["apif:美职:team:99999", "apif:美职:team:88888"],
            }[case]
        elif case == "reverse":
            old.home_team_id, old.away_team_id = old.away_team_id, old.home_team_id
        else:
            fields = {c.name: getattr(old, c.name) for c in old.__table__.columns}
            fields.update(match_id="fd:synthetic-competitor", status="scheduled")
            if case == "remote_claim":
                fields.update(
                    league="英超", counterpart_id=old.match_id, kickoff_utc="2026-10-20T00:00:00Z"
                )
            session.add(Match(**fields))
        session.flush()
        with pytest.raises(ValueError, match="重叠|重复"):
            select_training_history(history(session), CUTOFF)


def test_a_bare_frame_cannot_authorize_the_exclusion(db):
    with session_scope(db) as session:
        frame = history(session)
        frame.attrs.pop("pending_fd_identity_overlaps", None)
        with pytest.raises(ValueError, match="重叠|重复"):
            select_training_history(frame, CUTOFF)


def test_unmapped_pairs_are_visible_in_actionable_audit(db, tmp_path):
    out = tmp_path / "audit.csv"
    report = audit_source_results(db.url.database, out)
    rows = list(csv.DictReader(out.open()))
    assert report["unverified"] == 2
    assert {row["apif_match_id"] for row in rows} == {"apif:1490585", "apif:1490596"}
    assert all(row["reason"] == "unverified_fd_identity_overlap" for row in rows)
    assert all(row["link_basis"] == "shared_team_id_candidate" for row in rows)
