"""Strict prospective candidates on synthetic checked-in technical observations."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, MatchStat, Result, Team
from zucai.predict.base import utc
from zucai.predict.experiments.parallel import compute, raw_json

FIX = Path(__file__).parents[1] / "data/fixtures/t34a"


@pytest.fixture
def strict_database(tmp_path):
    doc = json.loads((FIX / "prospective.json").read_bytes())
    path = tmp_path / "strict.db"
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(id=i + 1, name=name) for i, name in enumerate(doc["teams"])])
        session.flush()
        for row in doc["rows"]:
            kickoff = utc(row["kickoff_utc"])
            session.add(
                Match(
                    match_id=row["match_id"],
                    source="fd",
                    league=doc["league"],
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc=kickoff,
                    status="finished",
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=row["match_id"],
                    home_goals=row["home_goals"],
                    away_goals=row["away_goals"],
                    available_utc=kickoff + timedelta(hours=3),
                    available_source="captured",
                )
            )
            for side, team in (("home", 1), ("away", 2)):
                value = row[side + "_stat"]
                session.add(
                    MatchStat(
                        provider="fd",
                        provider_fixture_id=row["match_id"],
                        endpoint="csv",
                        record_key=side + ":shots_on_target",
                        match_id=row["match_id"],
                        team_id=team,
                        side=side,
                        period_basis="90",
                        eligible_90=True,
                        extra_time_included=False,
                        stat_key="shots_on_target",
                        raw_value=str(value),
                        value=value,
                        unit="count",
                        captured_utc=kickoff + timedelta(hours=4),
                        available_utc=kickoff + timedelta(hours=4),
                        availability_basis="captured",
                        raw_sha256="a" * 64,
                        raw_location="fixture://t34a/prospective.json",
                        parser_version="fixture",
                        version=1,
                    )
                )
        target = dict(
            doc["target"],
            source="jc",
            league=doc["league"],
            home=doc["teams"][0],
            away=doc["teams"][1],
        )
        session.add(
            Match(
                **{k: target[k] for k in ("match_id", "source", "league", "kickoff_utc")},
                home_team_id=1,
                away_team_id=2,
                status="scheduled",
            )
        )
    yield engine, path, doc, target
    engine.dispose()


def test_strict_fit_inference_exact_ids_and_frozen_evidence(strict_database):
    engine, _, doc, target = strict_database
    with session_scope(engine) as session:
        rows, fits = compute(session, [target], cutoff=doc["cutoff"])
        again, repeated_fits = compute(session, [target], cutoff=doc["cutoff"])
    assert raw_json(rows) == raw_json(again) and raw_json(fits) == raw_json(repeated_fits)
    sot, xg = rows
    assert sot["status"] == "predicted"
    assert sum(sot["model_probabilities"].values()) == pytest.approx(1)
    assert sot["p2_ready"] is False and sot["bet_allowed"] is False
    assert sot["top_score"]["probability"] > 0
    assert len(sot["coefficients_sha256"]) == 64
    assert sot["features"]["feature_cutoff"] == doc["cutoff"]
    assert all(cell["n"] == cell["N"] == 10 for cell in sot["features"]["cells"].values())
    assert all(
        e["availability_basis"] == "captured"
        for cell in sot["features"]["cells"].values()
        for e in cell["evidence"]
    )
    assert all(
        utc(e["available_utc"]) < utc(item["feature_cutoff"])
        for item in fits[0]["training"]
        for cell in item["features"]["cells"].values()
        for e in cell["evidence"]
    )
    assert xg["status"] == "missing" and xg["model_probabilities"] is None
    assert xg["missing_details"] and xg["fallback_reason"] == "strict_features_missing"
    assert "market" not in raw_json(fits).decode()


def test_backfilled_and_future_observations_do_not_fill_missing(strict_database):
    engine, _, doc, target = strict_database
    with session_scope(engine) as session:
        last = doc["rows"][-1]["match_id"]
        for row in session.scalars(select(MatchStat).where(MatchStat.match_id == last)):
            row.availability_basis = "backfilled"
            row.available_utc = None
        session.flush()
        rows, fits = compute(session, [target], cutoff=doc["cutoff"])
        assert rows[0]["model_probabilities"] is None
        assert any("backfilled" in reason for reason in rows[0]["missing_details"])
        for row in session.scalars(select(MatchStat).where(MatchStat.match_id == last)):
            row.availability_basis = "captured"
            row.available_utc = row.captured_utc = target["kickoff_utc"]
        session.flush()
        rows, _ = compute(session, [target], cutoff=doc["cutoff"])
        assert rows[0]["model_probabilities"] is None
        assert any("not_known_before_cutoff" in reason for reason in rows[0]["missing_details"])


def test_fit_failure_is_explicit_null(strict_database, monkeypatch):
    from zucai.predict.experiments.intensity import StatsIntensity

    engine, _, doc, target = strict_database

    def fail(*args, **kwargs):
        raise ArithmeticError("offline fit failure")

    monkeypatch.setattr(StatsIntensity, "fit", fail)
    with session_scope(engine) as session:
        rows, _ = compute(session, [target], cutoff=doc["cutoff"])
    assert rows[0]["model_probabilities"] is None
    assert rows[0]["fallback_reason"] == "fit_failed:ArithmeticError"


def test_aligned_jc_history_with_apif_values_is_used_and_counterparts_count_once(strict_database):
    from zucai.predict.experiments.history import unified_history

    engine, _, doc, target = strict_database
    with session_scope(engine) as session:
        for number, game in enumerate(doc["rows"]):
            match = session.get(Match, game["match_id"])
            jc_id = f"jc:aligned-{number}"
            match.counterpart_id = jc_id
            session.add(
                Match(
                    match_id=jc_id,
                    source="jc",
                    league=match.league,
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc=match.kickoff_utc,
                    status="finished",
                    counterpart_id=match.match_id,
                )
            )
            session.flush()
            session.add(
                Result(
                    match_id=jc_id,
                    home_goals=game["home_goals"],
                    away_goals=game["away_goals"],
                    available_utc=utc(game["kickoff_utc"]) + timedelta(hours=3),
                    available_source="captured",
                )
            )
            session.add(
                ApifFixtureIndex(
                    provider_fixture_id=str(1000 + number),
                    match_id=jc_id,
                    league="英超",
                    provider_league_id=39,
                    season=2023,
                    kickoff_utc=match.kickoff_utc,
                    home_provider_id=1,
                    away_provider_id=2,
                    home_team_id=1,
                    away_team_id=2,
                    captured_utc=match.kickoff_utc,
                    raw_sha256="a" * 64,
                    raw_location="fixture://t34a",
                    parser_version="fixture",
                    evidence={},
                )
            )
            for row in session.scalars(
                select(MatchStat).where(MatchStat.match_id == game["match_id"])
            ):
                row.provider = "apif"
                row.match_id = jc_id
                row.provider_fixture_id = str(1000 + number)
                row.provider_team_id = row.team_id
                row.endpoint = "statistics"
                row.stat_key = "expected_goals"
                row.record_key = row.side + ":expected_goals"
        session.flush()
        builder, games = unified_history(session, provider="apif")
        assert len(games) == len(doc["rows"])
        assert all(len(g["history_match_ids"]) == 2 for g in games)
        rows, fits = compute(session, [target], cutoff=doc["cutoff"])
    xg = next(r for r in rows if r["model_name"] == "dc-xg-v1")
    assert xg["status"] == "predicted"
    assert xg["training_rows"] == len(doc["rows"]) - 10
    assert all(cell["n"] == cell["N"] == 10 for cell in xg["features"]["cells"].values())
