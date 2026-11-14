"""Actual T66 saved rows remain distinct from T63's complete, dated identity proofs."""

from datetime import timedelta

import pytest

from test_t66_fd_apif import SEED, SEEN, clone, evidence, ingest_fd
from test_t66_fd_apif import db as db
from zucai.db import session_scope
from zucai.models import Match, Result, ResultSourceIdentity, ResultSourceLink
from zucai.predict.backtest import read_history
from zucai.predict.pooled import select_training_history
from zucai.source_result_reconcile import reconcile_source_results


def test_partial_fd_pointer_never_mints_a_complete_identity_proof(db):
    case = SEED["cases"][1]
    with session_scope(db) as session:
        before = evidence(session, case["apif_id"])
        assert ingest_fd(session, case)["linked_apif"] == 1
    report = reconcile_source_results(db.url.database, SEEN + timedelta(minutes=1))
    assert report["reasons"]["unverified_provider_identity"] >= 1
    with session_scope(db) as session:
        assert evidence(session, case["apif_id"]) == before
        assert session.get(Match, case["apif_id"]).counterpart_id == case["fd_id"]
        assert (
            not session.query(ResultSourceLink).filter_by(provider_match_id=case["apif_id"]).all()
        )
        assert not session.query(ResultSourceIdentity).filter_by(match_id=case["apif_id"]).all()
        pool = read_history(session, "欧战", "jc,fd,fdorg,apif", training_leagues=["瑞超"])
        assert list(pool.match_id) == [case["fd_id"]]
        domestic = read_history(session, "瑞超", "jc,fd,apif")
        selected = select_training_history(domestic, SEEN + timedelta(minutes=2))
        assert list(selected.match_id) == [case["fd_id"]]


def test_fd_pointer_and_timed_proof_keep_each_cutoff_and_full_graph_guard(db):
    case = SEED["cases"][0]
    with session_scope(db) as session:
        assert ingest_fd(session, case)["linked_apif"] == 1
        session.get(Result, case["fd_id"]).available_utc = SEEN + timedelta(minutes=3)
    reconcile_source_results(db.url.database, SEEN + timedelta(minutes=1))
    with session_scope(db) as session:
        frame = read_history(session, "芬超", "jc,fd,apif")
        assert {
            seconds: list(
                select_training_history(frame, SEEN + timedelta(seconds=seconds)).match_id
            )
            for seconds in (59, 60, 61, 180, 181)
        } == {59: [], 60: [], 61: [case["apif_id"]], 180: [case["apif_id"]], 181: [case["fd_id"]]}
        clone(session, session.get(Match, case["apif_id"]), "apif:777777")
    with session_scope(db) as session, pytest.raises(ValueError, match="apif"):
        read_history(session, "芬超", "jc,fd,apif")
