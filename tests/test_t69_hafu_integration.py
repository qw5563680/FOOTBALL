"""T69 integration: freeze half-full-time probabilities without touching WDL or models."""

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Result, Team
from zucai.predict.hafu_share import share_from_rows
from zucai.report.batch import predict_batch

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 11, 2, 18, tzinfo=UTC)
DAY = date(2026, 11, 3)


def _add_match(session, teams, index):
    kickoff = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(hours=3 * index)
    match = Match(
        match_id=f"fd:{index:06d}",
        source="fd",
        league="英超",
        home_team_id=teams["A"].id,
        away_team_id=teams["B"].id,
        kickoff_utc=kickoff,
        status="finished",
    )
    session.add(match)
    session.flush()
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
        for index in range(640):
            _add_match(session, teams, index)
        # One finished row whose half scores are missing: pandas then promotes
        # the whole column to float, which must not break the other rows.
        missing_half = Match(
            match_id="fd:888888",
            source="fd",
            league="英超",
            home_team_id=teams["A"].id,
            away_team_id=teams["B"].id,
            kickoff_utc=datetime(2026, 8, 20, 12, tzinfo=UTC),
            status="finished",
        )
        session.add(missing_half)
        session.flush()
        session.add(
            Result(
                match_id=missing_half.match_id,
                home_goals=1,
                away_goals=0,
                ht_home_goals=None,
                ht_away_goals=None,
                available_utc=missing_half.kickoff_utc + timedelta(hours=2),
                available_source="fixture",
            )
        )
        # One row whose result becomes known only after the cutoff: it must not
        # influence the share no matter how its half scores change afterwards.
        late = Match(
            match_id="fd:999999",
            source="fd",
            league="英超",
            home_team_id=teams["A"].id,
            away_team_id=teams["C"].id,
            kickoff_utc=NOW - timedelta(hours=12),
            status="finished",
        )
        session.add(late)
        session.flush()
        session.add(
            Result(
                match_id=late.match_id,
                home_goals=2,
                away_goals=1,
                ht_home_goals=1,
                ht_away_goals=0,
                available_utc=NOW + timedelta(hours=3),
                available_source="fixture",
            )
        )
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
    training = train(
        db=database,
        league="英超",
        source="fd",
        since=NOW - timedelta(days=56),
        as_of=NOW,
        output=model_path,
        params_file=params,
    )
    return database, model_path, params, training["half_share_training"]


def _share(session):
    """The share from exactly the rows the fit consumes, recomputed independently."""
    from zucai.predict.backtest import read_history
    from zucai.predict.pooled import select_training_history

    history = read_history(session, "英超", "fd")
    rows = select_training_history(history, NOW)
    return share_from_rows(rows.to_dict("records"))


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


def _hint(share):
    return dict(tier="league", **share)


def test_freeze_includes_half_full_time_and_preserves_wdl_and_model_hash(
    hafu_environment, tmp_path
):
    database, model_path, params, training_share = hafu_environment
    engine = get_engine(database)
    with session_scope(engine) as session:
        assert _share(session) == training_share
        hint = _hint(training_share)
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
    assert half["share"] == training_share["share"]
    assert half["share_tier"] == "league"
    assert half["share_ht_matches"] == 640
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
    database, model_path, params, training_share = hafu_environment
    engine = get_engine(database)
    hint = _hint(training_share)
    with session_scope(engine) as session:
        batch = _batch(session, model_path, params, hint)
        expected = batch["predictions"][0]["half_full_time"]["probabilities"]
    with session_scope(engine) as session:
        from sqlalchemy import delete

        session.execute(delete(Odds))
    # Re-estimate the share on the odds-free database, then freeze again.
    with session_scope(engine) as session:
        assert _share(session) == training_share
        batch = _batch(session, model_path, params, _hint(_share(session)))
        actual = batch["predictions"][0]["half_full_time"]["probabilities"]
    assert actual == expected


def test_post_cutoff_result_changes_share_and_probabilities_nowhere(hafu_environment):
    database, model_path, params, training_share = hafu_environment
    engine = get_engine(database)
    with session_scope(engine) as session:
        before = _batch(session, model_path, params, _hint(_share(session)))
        session.get(Result, "fd:999999").ht_home_goals = 4
        session.get(Result, "fd:999999").ht_away_goals = 4
        session.get(Result, "fd:999999").home_goals = 5
    # The share is re-estimated after the post-cutoff change: it must be unchanged.
    with session_scope(engine) as session:
        assert _share(session) == training_share
        after = _batch(session, model_path, params, _hint(_share(session)))
    assert before["predictions"][0]["half_full_time"] == after["predictions"][0]["half_full_time"]


def test_replay_with_fixed_cutoff_is_identical(hafu_environment):
    database, model_path, params, training_share = hafu_environment
    engine = get_engine(database)
    hint = _hint(training_share)
    with session_scope(engine) as session:
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


def test_report_csv_and_digest_carry_half_full_time(hafu_environment, tmp_path):
    from zucai.digest_views import forecast_sections
    from zucai.report.render import write_report

    database, model_path, params, training_share = hafu_environment
    engine = get_engine(database)
    with session_scope(engine) as session:
        batch = _batch(session, model_path, params, _hint(training_share))
    directory = tmp_path / "freeze"
    path = write_report(batch, directory)
    csv_text = (directory / "report.csv").read_bytes().decode("utf-8-sig")
    assert "hafu_hh" in csv_text
    assert "hafu_aa" in csv_text
    assert "hafu_share_tier" in csv_text
    markdown = (directory / "report.md").read_text(encoding="utf-8")
    assert "胜/胜" in markdown and "半场进球占比" in markdown
    digest_lines = forecast_sections([(DAY.isoformat(), str(path), batch)], DAY, "repo")
    digest_text = "\n".join(digest_lines)
    assert "半全场（实验）" in digest_text
    assert "胜/胜" in digest_text
    assert "占比" in digest_text


def test_backtest_reports_nine_class_half_full_time(hafu_environment):
    from zucai.predict.backtest import evaluate, read_history

    database, _model_path, _params, training_share = hafu_environment
    engine = get_engine(database)
    with session_scope(engine) as session:
        history = read_history(session, "英超", "fd")
    result = evaluate(
        history,
        {},
        since=datetime(2026, 6, 1, tzinfo=UTC),
        as_of=NOW,
        half_life_days=30,
        fold="weekly",
        hafu_market={},
    )
    half = result["half_full_time"]
    assert half["model"]["n"] > 0
    assert half["model"]["log_loss"] is not None and math.isfinite(half["model"]["log_loss"])
    assert half["model"]["brier"] is not None and math.isfinite(half["model"]["brier"])
    # Fixed-share baseline is scored on exactly the same paired matches.
    assert half["fixed_half_share"]["n"] == half["model"]["n"]
    # One seeded row lacks half scores; the others still score (float column).
    assert half["excluded"].get("no_half_score", 0) == 1
    # Early folds under the 300-match threshold are reported, never silently assumed.
    assert half["excluded"].get("fold_share_insufficient", 0) > 0
    # Per-outcome calibration table lists all nine outcomes.
    assert [row["outcome"] for row in half["model"]["outcomes"]] == list(
        h + f for h in "hda" for f in "hda"
    )
    # No hafu odds were seeded: the odds benchmark is empty by construction.
    assert half["market_benchmark"]["n"] == 0
    assert half["market_paired_model"]["n"] == 0
    # Fold metadata discloses the share tier and sample for scored folds.
    assert any(f.get("half_share") is not None for f in result["folds"])
    for fold in result["folds"]:
        if fold.get("half_share"):
            assert fold["half_share"]["tier"] in {"league", "pool"}
            assert fold["half_share"]["ht_matches"] >= 300
    # The fit-rows share matches the one the daily path reports for the same cutoff.
    scored_fold = next(f for f in result["folds"] if f.get("half_share"))
    assert scored_fold["half_share"]["share"] == training_share["share"]
