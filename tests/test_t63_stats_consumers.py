"""Synthetic retained rows exercise proof clocks at each feature cutoff."""

from datetime import timedelta

import pytest

from test_t63_stats_retention import add_index, add_match, proof
from test_t63_stats_retention import engine as engine
from zucai.db import session_scope
from zucai.features.injuries import InjuryFeatures
from zucai.features.stats import StatsFeatures
from zucai.models import Match, Odds
from zucai.predict.experiments.command import attach_market
from zucai.predict.experiments.history import unified_history
from zucai.sources.apif_stats import instant


def retained(session, verified="2026-11-11T12:00:00Z", *, status="finished"):
    paid = add_match(session, "apif", kickoff="2026-11-10T10:30:00Z")
    paid.status = status
    session.get(Match, "jc:1").status = status
    proof(session, "apif:1", "jc:1", verified=instant(verified))
    return paid


def test_aligned_history_uses_model_cutoff_and_rechecks_each_feature_cutoff(engine):
    verified = instant("2026-11-11T12:00:00Z")
    with session_scope(engine) as session:
        retained(session)
        add_match(session, "jc", key="jc:target", kickoff="2026-11-12T10:00:00Z")
        builder, games = unified_history(
            session, provider="apif", as_of=verified + timedelta(seconds=1)
        )
        assert [game["history_match_ids"] for game in games] == [["apif:1", "jc:1"]]
        assert games[0]["match_id"] == "jc:1"
        assert games[0]["earliest_kickoff_utc"] == instant("2026-11-10T10:00Z")
        assert games[0]["kickoff_utc"] == instant("2026-11-10T10:30Z")
        target = builder.matches["jc:target"]
    for cutoff, count in [
        (verified, 2),
        (verified + timedelta(seconds=1), 1),
        (verified - timedelta(seconds=1), 2),
    ]:
        row = builder.build(
            target,
            as_of=cutoff,
            history_source="aligned",
            research=False,
            stat_keys=("shots",),
            windows=(5,),
        )
        assert row["cells"]["home_all_5_shots_for"]["sample_n"] == count


@pytest.mark.parametrize("case", ["future", "stale", "scheduled"])
def test_history_does_not_admit_future_stale_or_unfinished_rows(engine, case):
    with session_scope(engine) as session:
        paid = retained(session)
        if case == "stale":
            paid.kickoff_utc = "2026-11-10T10:31:00Z"
        if case == "scheduled":
            paid.status = "scheduled"
        session.flush()
        cutoff = "2026-11-11T12:00:00Z" if case == "future" else "2026-11-12T12:00:00Z"
        _, games = unified_history(session, provider="apif", as_of=cutoff)
        assert len(games) == (1 if case == "scheduled" else 2)
        assert all(game["status"] == "finished" for game in games)


def test_proof_does_not_hide_a_competing_row_from_the_same_source(engine):
    with session_scope(engine) as session:
        retained(session)
        other = add_match(session, "apif", key="apif:2", kickoff="2026-11-10T10:30:00Z")
        other.status = "finished"
        session.flush()
        _, games = unified_history(session, provider="apif", as_of="2026-11-12T00:00Z")
        assert len(games) == 2
        assert next(game for game in games if game["match_id"] == "jc:1")["history_match_ids"] == [
            "jc:1"
        ]


@pytest.mark.parametrize("mode", ["strict", "backfill_replay"])
def test_injury_index_proof_is_strictly_before_each_target_cutoff(engine, mode):
    with session_scope(engine) as session:
        retained(session, "2026-11-10T09:00Z", status="scheduled")
        add_index(session, "jc:1")
        builder = InjuryFeatures(session)
    target = builder.matches["apif:1"]
    for cutoff, reason in [
        ("2026-11-10T09:00Z", "fixture_identity_missing"),
        ("2026-11-10T09:01Z", "capture_receipt_missing"),
        ("2026-11-10T08:59Z", "fixture_identity_missing"),
    ]:
        row = builder.build(target, as_of=cutoff, mode=mode)
        assert row["home"]["missing_n"]["missing_reason"] == reason


def test_proof_after_earliest_kickoff_cannot_enable_injury_inputs(engine):
    with session_scope(engine) as session:
        retained(session, "2026-11-10T10:10Z", status="scheduled")
        add_index(session, "jc:1")
        builder = InjuryFeatures(session)
    row = builder.build(builder.matches["apif:1"], as_of="2026-11-10T11:00Z")
    assert row["home"]["missing_n"]["missing_reason"] == "fixture_identity_missing"


def test_multiple_indices_in_a_proven_group_remain_ambiguous_for_injuries(engine):
    with session_scope(engine) as session:
        retained(session, "2026-11-10T09:00Z", status="scheduled")
        add_index(session, "jc:1")
        add_index(session, "apif:1", "2")
        builder = InjuryFeatures(session)
    row = builder.build(builder.matches["apif:1"], as_of="2026-11-10T09:01Z")
    assert row["home"]["missing_n"]["missing_reason"] == "fixture_identity_ambiguous"


@pytest.mark.parametrize(
    "cutoff,available", [("2026-11-10T09:00Z", False), ("2026-11-10T09:01Z", True)]
)
def test_post_fit_market_mapping_uses_only_proofs_known_before_row_cutoff(
    engine, cutoff, available
):
    with session_scope(engine) as session:
        retained(session, "2026-11-10T09:00Z")
        for selection in "hda":
            session.add(
                Odds(
                    match_id="jc:1",
                    pool="had",
                    line=0,
                    selection=selection,
                    price=3,
                    snapshot_utc="2026-11-10T08:00Z",
                )
            )
        session.flush()
        features = StatsFeatures(session, provider="apif")
        rows = [dict(match_id="apif:1", actual=0, feature_cutoff=cutoff, variants={})]
        attach_market(session, rows, features)
        assert (rows[0]["variants"]["market"] is not None) is available


@pytest.mark.parametrize("reverse", [False, True])
def test_same_fixture_market_rows_keep_their_own_proof_cutoffs(engine, reverse):
    with session_scope(engine) as session:
        retained(session, "2026-11-10T09:00Z")
        for selection in "hda":
            session.add(
                Odds(
                    match_id="jc:1",
                    pool="had",
                    line=0,
                    selection=selection,
                    price=3,
                    snapshot_utc="2026-11-10T08:00Z",
                )
            )
        session.flush()
        rows = [
            dict(match_id="apif:1", actual=0, feature_cutoff=cutoff, variants={})
            for cutoff in ("2026-11-10T08:59Z", "2026-11-10T09:01Z")
        ]
        if reverse:
            rows.reverse()
        attach_market(session, rows, StatsFeatures(session, provider="apif"))
        assert {row["feature_cutoff"]: row["variants"]["market"] is not None for row in rows} == {
            "2026-11-10T08:59Z": False,
            "2026-11-10T09:01Z": True,
        }
