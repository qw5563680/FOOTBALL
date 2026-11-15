"""T69 integration: freeze half-full-time probabilities without touching WDL or models."""

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, Team
from zucai.predict.hafu_share import estimate_share
from zucai.report.batch import predict_batch

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)
DAY = date(2026, 11, 3)


def _add_match(session, teams, index, finished, ht):
    kickoff = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(hours=3 * index)
    source = "fd"
    match = Match(
        match_id=f"fd:{index:06d}",
        source=source,
        league="英超",
        home_team_id=teams["A"].id,
        away_team_id=teams["B"].id,
        kickoff_utc=kickoff,
        status="finished" if finished else "scheduled",
    )
    session.add(match)
    session.flush()
    if finished:
        full = ((2, 1), (1, 0), (1, 1), (0, 2), (3, 1), (2, 2), (0, 1), (1, 2))[index % 8]
        half = (
            (1, 1),
            (1, 0),
            (0, 1),
            (0, 1),
            (2, 1),
            (1, 1),
            (0, 1),
            (1, 1),
        )[index % 8]
        session.add(
            Result(
                match_id=match.match_id,
                home_goals=full[0],
                away_goals=full[1],
                ht_home_goals=half[0],
                ht_away_goals=half[1],
                available_utc=kickoff + timedelta(hours=2),
                available_source="fixture",
            )
        )
        for selection, price in zip("hda", (2.4, 3.1, 2.9), strict=False):
            session.add(
                Odds(
                    match_id=match.match_id,
                    pool="had",
                    line=0,
                    selection=selection,
                    price=price,
                    snapshot_utc=kickoff - timedelta(hours=1),
                )
            )
    else:
        return match


@pytest.fixture
def hafu_environment(tmp_path):
    database = tmp_path / "seed.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in "ABCD"}
        session.add_all(teams.values())
        session.flush()
        for index in range(320):
            _add_match(session, teams, index, finished=True, ht=True)
        upcoming = Match(
            match_id="fd:999001",
            source="fd",
            league="英超",
            home_team_id=teams["A"].id,
            away_team_id=teams["C"].id,
            kickoff_utc=datetime(2026, 11, 3, 10, tzinfo=UTC),
            status="scheduled",
        )
        session.add(upcoming)
    engine.dispose()
    config = json.loads((ROOT / "data/model/params.json").read_bytes())
    config["leagues"] = {k: v for k, v in config["leagues"].items() if k == "英超"}
    params = tmp_path / "params.json"
    params.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    from zucai.predict.command import train

    model_path = tmp_path / "model.pkl"
    train(
        db=database,
        league="英超",
        source="fd",
        since=NOW - timedelta(days=56),
        as_of=NOW,
        output=model_path,
        params_file=params,
    )
    return database, model_path, params


def _share(session):
    from zucai.cloud_predict import _share_rows

    rows = _share_rows(session, ["英超"], NOW)
    return estimate_share(rows, pool_rows=rows, all_rows=rows)


def _batch(session, model_path, params, hint):
    return predict_batch(
        session,
        model_files=[model_path],
        as_of=NOW,
        generated=NOW,
        day=DAY,
        params_file=params,
        persist=False,
        half_share_hints={"英超": hint},
    )


def test_freeze_includes_half_full_time_and_preserves_wdl_and_model_hash(
    hafu_environment, tmp_path
):
    database, model_path, params = hafu_environment
    engine = get_engine(database)
    with session_scope(engine) as session:
        hint = _share(session)
        batch = _batch(session, model_path, params, hint)
        plain = _batch(session, model_path, params, None)
    predictions = {p["match_id"]: p for p in batch["predictions"]}
    plain_predictions = {p["match_id"]: p for p in plain["predictions"]}
    assert len(predictions) == 1
    prediction = predictions["fd:999001"]
    plain_prediction = plain_predictions["fd:999001"]
    assert prediction["status"] == "predicted"
    # WDL probabilities and the model file hash are byte-for-byte identical
    # whether or not the half-time derivation is attached.
    assert prediction["model_probabilities"] == plain_prediction["model_probabilities"]
    assert prediction["model_version"] == plain_prediction["model_version"]
    assert prediction["score_matrix"] == plain_prediction["score_matrix"]
    assert "half_full_time" not in plain_prediction
    half = prediction["half_full_time"]
    assert half["status"] == "experimental"
    assert set(half["probabilities"]) == {h + f for h in "hda" for f in "hda"}
    assert math.isclose(sum(half["probabilities"].values()), 1, rel_tol=0, abs_tol=1e-12)
    assert half["share"] == hint["share"]
    assert half["share_tier"] == "league"
    assert half["share_ht_matches"] == 320
    import numpy as np

    from zucai.predict.derive import had

    grid = np.asarray(prediction["score_matrix"])
    assert prediction["model_probabilities"] == had(grid)
    for full in "hda":
        merged = sum(v for k, v in half["probabilities"].items() if k[1] == full)
        assert math.isclose(
            merged, prediction["model_probabilities"][full], rel_tol=0, abs_tol=1e-12
        )


def test_deleting_odds_table_changes_no_half_full_time_probability(hafu_environment):
    database, model_path, params = hafu_environment
    engine = get_engine(database)
    with session_scope(engine) as session:
        hint = _share(session)
        batch = _batch(session, model_path, params, hint)
        expected = batch["predictions"][0]["half_full_time"]["probabilities"]
    with session_scope(engine) as session:
        from sqlalchemy import delete

        session.execute(delete(Odds))
    with session_scope(engine) as session:
        batch = _batch(session, model_path, params, hint)
        actual = batch["predictions"][0]["half_full_time"]["probabilities"]
    assert actual == expected


def test_replay_with_fixed_cutoff_is_identical(hafu_environment):
    database, model_path, params = hafu_environment
    engine = get_engine(database)
    with session_scope(engine) as session:
        hint = _share(session)
        first = _batch(session, model_path, params, hint)
    with session_scope(engine) as session:
        second = _batch(session, model_path, params, hint)
    for left, right in zip(first["predictions"], second["predictions"], strict=True):
        for key in (
            "model_version",
            "model_probabilities",
            "score_matrix",
            "half_full_time",
        ):
            assert left[key] == right[key], key
