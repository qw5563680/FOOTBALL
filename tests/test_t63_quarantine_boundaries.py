"""Preserve conservative MLS exclusion boundaries found during independent review."""

import pandas as pd
import pytest
from sqlalchemy import text

from test_t63_fd_identity_quarantine import CHECKED, CUTOFF, history
from test_t63_fd_identity_quarantine import db as db
from zucai.db import session_scope
from zucai.models import Match, Result, Team
from zucai.predict.pooled import select_training_history

API = "apif:1490585"
FD = "fd:USA:20261031:fc-cincinnati:philadelphia-union"


@pytest.mark.parametrize("target", [API, FD])
@pytest.mark.parametrize(
    "kind", ["outgoing", "remote_incoming", "scheduled_neighbor", "bad_alias_element"]
)
def test_blockers_do_not_grant_quarantine(db, target, kind):
    with session_scope(db) as s:
        row = s.get(Match, target)
        if kind == "outgoing":
            row.counterpart_id = "jc:nonexistent"
        elif kind in ("remote_incoming", "scheduled_neighbor"):
            fields = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            fields.update(
                match_id="jc:remote", source="jc", status="scheduled", counterpart_id=None
            )
            if kind == "remote_incoming":
                fields.update(
                    league="英超", kickoff_utc="2025-01-01T00:00:00Z", counterpart_id=target
                )
            s.add(Match(**fields))
        else:
            s.get(Team, 509).aliases = [None]
        s.flush()
        frame = history(s)
        assert API not in {x["match_id"] for x in frame.attrs["pending_fd_identity_overlaps"]}
        with pytest.raises(ValueError, match="重叠|重复"):
            select_training_history(frame, CUTOFF)


def test_scheduled_fd_row_without_result_cannot_suppress_paid_result(db):
    with session_scope(db) as s:
        s.delete(s.get(Result, FD))
        s.get(Match, FD).status = "scheduled"
        s.flush()
        frame = history(s)
        selected = select_training_history(frame, CUTOFF)
        assert API in set(selected.match_id)
        assert FD not in set(selected.match_id)
        assert API not in {x["match_id"] for x in selected.attrs["unverified_fd_identity_overlaps"]}


def test_identity_equal_cutoff_does_not_create_spurious_exclusion(db):
    with session_scope(db) as s:
        frame = history(s)
        selected = select_training_history(frame, CHECKED)
        assert not selected.attrs["unverified_fd_identity_overlaps"]
        assert set(selected.source) == {"fd"}
        later = select_training_history(frame, CHECKED + pd.Timedelta(microseconds=1))
        assert {x["match_id"] for x in later.attrs["unverified_fd_identity_overlaps"]} == {
            "apif:1490585",
            "apif:1490596",
        }


@pytest.mark.parametrize("hours,expected_pending", [(3, True), (3 + 1 / 3600, False)])
def test_window_boundary_is_inclusive_only_at_three_hours(db, hours, expected_pending):
    with session_scope(db) as s:
        s.get(Match, FD).kickoff_utc = (
            pd.Timestamp("2026-10-31T18:00Z") + pd.Timedelta(hours=hours)
        ).isoformat()
        s.get(Result, FD).available_utc = "2026-11-01T00:00:00Z"
        s.flush()
        frame = history(s)
        assert (
            API in {x["match_id"] for x in frame.attrs["pending_fd_identity_overlaps"]}
        ) is expected_pending
        selected = select_training_history(frame, CUTOFF)
        assert (API not in set(selected.match_id)) is expected_pending


def test_invalid_alias_object_in_persisted_database_fails_closed(db):
    with session_scope(db) as s:
        s.execute(text("UPDATE teams SET aliases='{}' WHERE id=509"))
        s.flush()
        s.expire_all()
        with pytest.raises(ValueError, match="aliases"):
            history(s)
