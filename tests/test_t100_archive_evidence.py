from datetime import timedelta
from pathlib import Path
from runpy import run_path

import pandas as pd
import pytest

from test_t66_fd_apif import SEED, SEEN, clone, ingest_fd
from test_t66_fd_apif import db as db
from zucai.db import session_scope
from zucai.models import Match, Result
from zucai.predict.backtest import read_archive_evidence, read_history
from zucai.predict.pooled import select_training_history
from zucai.source_result_reconcile import reconcile_source_results
from zucai.sources.fd_counterpart import CounterpartConflict

score_session = run_path(Path(__file__).resolve().parents[1] / "examples/t100_rating_replay.py")[
    "score_session"
]


def _link_archive(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        session.get(Result, case["fd_id"]).available_utc = SEEN + timedelta(minutes=3)
    reconcile_source_results(db.url.database, SEEN + timedelta(minutes=1))
    return case


def _history(session, **kwargs):
    return read_history(
        session,
        source="jc,fd,apif",
        training_leagues=["芬超"],
        source_selection="cutoff",
        **kwargs,
    )


def test_archived_evidence_preserves_each_cutoffs_selected_results(db):
    case = _link_archive(db)
    with session_scope(db) as session:
        fd = session.get(Match, case["fd_id"])
        old_kickoff = SEEN - timedelta(days=2)
        clone(session, fd, "fd:bridge", kickoff_utc=old_kickoff, status="finished")
        clone(
            session,
            fd,
            "jc:bridge",
            counterpart_id="fd:bridge",
            kickoff_utc=old_kickoff,
            status="finished",
        )
        session.add_all(
            Result(
                match_id=key,
                home_goals=1,
                away_goals=0,
                available_utc=when,
                available_source="captured",
            )
            for key, when in (
                ("fd:bridge", SEEN - timedelta(hours=2)),
                ("jc:bridge", SEEN + timedelta(minutes=6)),
            )
        )
    with score_session(db.url.database, pd.Timestamp(SEEN)) as session:
        evidence = read_archive_evidence(session)

    for seconds, expected in (
        (61, [case["apif_id"], "fd:bridge"]),
        (181, [case["fd_id"], "fd:bridge"]),
        (361, [case["fd_id"], "jc:bridge"]),
    ):
        cutoff = pd.Timestamp(SEEN + timedelta(seconds=seconds))
        with score_session(db.url.database, cutoff) as session:
            normal = _history(session)
            cached = _history(session, archive_evidence=evidence)
        pd.testing.assert_frame_equal(cached, normal)
        assert cached.attrs == normal.attrs
        original = select_training_history(normal, cutoff)
        optimized = select_training_history(cached, cutoff)
        pd.testing.assert_frame_equal(optimized, original)
        assert optimized.attrs == original.attrs
        assert list(optimized.match_id) == expected


def test_cached_claim_error_only_applies_in_fold_with_claimed_apif_result(db):
    case = _link_archive(db)
    with session_scope(db) as session:
        api = session.get(Match, case["apif_id"])
        clone(session, api, "apif:777777")
        session.get(Result, api.match_id).available_utc = SEEN + timedelta(minutes=5)
    early = pd.Timestamp(SEEN + timedelta(minutes=4))
    late = pd.Timestamp(SEEN + timedelta(minutes=6))
    with score_session(db.url.database, early) as session:
        evidence = read_archive_evidence(session)
        early_normal = _history(session)
        early_cached = _history(session, archive_evidence=evidence)
        pd.testing.assert_frame_equal(early_cached, early_normal)
        assert early_cached.attrs == early_normal.attrs
    with score_session(db.url.database, late) as session:
        with pytest.raises(CounterpartConflict) as uncached:
            _history(session)
        with pytest.raises(CounterpartConflict) as cached:
            _history(session, archive_evidence=evidence)
    assert str(cached.value) == str(uncached.value)
    assert cached.value.candidates == uncached.value.candidates
