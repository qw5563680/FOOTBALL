"""Attack/defence strengths shown beside each forecast, and the 10,000-run simulation of
how many picks come in: both only restate the frozen model, they never change it."""

from datetime import timedelta

import numpy as np
import pytest

from test_prediction import CUTOFF, games  # noqa: F401
from test_prediction_batch import CUTOFF as BATCH_CUTOFF
from test_prediction_batch import setup_batch  # noqa: F401
from zucai.digest_views import strength_lines
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes
from zucai.report.sfc import hit_line, pick_nine_lines
from zucai.simulate import (
    RUNS,
    exact_hit_distribution,
    hit_summary,
    outcome_frequencies,
    sample_scores,
    seed_from,
    simulate_hits,
)

TARGET = {"home": "A", "away": "B", "kickoff_utc": CUTOFF + timedelta(hours=1)}


@pytest.fixture
def model(games):  # noqa: F811
    return DixonColes().fit(games, as_of=CUTOFF)


def test_strengths_are_the_forecasts_own_parameters(model):
    values = model.team_strengths("A", "B")
    grid = model.engine.predict("A", "B", max_goals=15, normalize=False)
    # Expected goals are the fixture's own scoring rates.
    assert values["home_expected_goals"] == pytest.approx(grid.home_goal_expectation, abs=1e-9)
    assert values["away_expected_goals"] == pytest.approx(grid.away_goal_expectation, abs=1e-9)
    assert not values["home_default_strength"] and not values["away_default_strength"]
    assert values["fitted_teams"] == 4
    params = model.engine.get_params()
    ranks = sorted("ABCD", key=lambda team: params[f"attack_{team}"])
    indices = {team: model.team_strengths(team, "A" if team != "A" else "B") for team in "ABCD"}
    assert sorted("ABCD", key=lambda team: indices[team]["home_attack_index"]) == ranks
    # 100 is the average of the fitted teams (geometric mean of the indices).
    for key in ("home_attack_index", "home_defence_index"):
        logs = [np.log(indices[team][key] / 100) for team in "ABCD"]
        assert abs(float(np.mean(logs))) < 1e-9
    # A higher defence index means fewer goals conceded: the parameter is inverted.
    weakest = max("ABCD", key=lambda team: params[f"defence_{team}"])
    assert min("ABCD", key=lambda team: indices[team]["home_defence_index"]) == weakest


def test_a_side_the_fit_has_not_seen_is_marked_default_strength(model):
    values = model.team_strengths("new", "B")
    assert values["home_default_strength"] is True and values["away_default_strength"] is False
    prior = model.prior_parameters
    params = model.engine.get_params()
    mean = float(np.mean([params[f"attack_{team}"] for team in "ABCD"]))
    assert values["home_attack_index"] == pytest.approx(100 * np.exp(prior["attack"] - mean))


def test_strengths_do_not_change_the_forecast(model):
    before = model.predict_score_matrix(TARGET)
    model.team_strengths("A", "B")
    np.testing.assert_array_equal(model.predict_score_matrix(TARGET), before)


def test_simulated_single_match_agrees_with_the_exact_probabilities(model):
    matrix = model.predict_score_matrix(TARGET)
    exact = had(matrix)
    sampled = outcome_frequencies(matrix, RUNS, seed_from("consistency"))
    assert all(abs(sampled[key] - exact[key]) < 0.015 for key in "hda")
    home, away = sample_scores(matrix, 100, 7)
    assert home.shape == away.shape == (100,) and home.max() < 10 and away.max() < 10


def test_simulation_is_reproducible_and_matches_the_exact_hit_distribution():
    matches = [
        {"h": 0.6, "d": 0.25, "a": 0.15},
        {"h": 0.2, "d": 0.3, "a": 0.5},
        {"h": 0.4, "d": 0.35, "a": 0.25},
    ]
    first = hit_summary(matches, "26166", "sha", "任九")
    assert first == hit_summary(matches, "26166", "sha", "任九")
    assert first["seed"] != hit_summary(matches, "26167", "sha", "任九")["seed"]
    assert first["runs"] == 10_000 and first["matches"] == 3
    exact = exact_hit_distribution([0.6, 0.5, 0.4])
    assert first["exact"] == pytest.approx(exact) and sum(exact) == pytest.approx(1)
    assert exact[3] == pytest.approx(0.6 * 0.5 * 0.4) and exact[0] == pytest.approx(0.4 * 0.5 * 0.6)
    assert max(abs(a - b) for a, b in zip(first["simulated"], exact, strict=True)) < 0.015
    assert sum(first["simulated"]) == pytest.approx(1)
    assert simulate_hits([], 10, 1) == [1.0]


@pytest.mark.parametrize(
    "bad",
    [
        {"h": 0.5, "d": 0.5, "a": 0.5},
        {"h": -0.1, "d": 0.6, "a": 0.5},
        {"h": float("nan"), "d": 0, "a": 1},
    ],
)
def test_invalid_probabilities_are_refused(bad):
    with pytest.raises(ValueError):
        simulate_hits([bad], 10, 1)
    with pytest.raises(ValueError):
        exact_hit_distribution([1.2])


def _report(count=14):
    rows = [
        dict(
            seq=n,
            home="H",
            away="A",
            kickoff_utc="2026-11-08T12:00:00Z",
            probabilities={"h": 0.15, "d": 0.85 - n / 50, "a": n / 50},
        )
        for n in range(1, count + 1)
    ]
    return dict(
        rows=rows,
        issue_no="26166",
        forecast_sha256="a" * 64,
        sale_state="published_before_deadline",
        mode="prematch",
        published_utc="2026-11-08T10:00:00Z",
    )


def test_pick_nine_adds_one_simulated_line_that_agrees_with_the_product():
    report = _report()
    lines = pick_nine_lines(report)
    simulated = [line for line in lines if line.startswith("模拟 10,000 次")]
    assert len(simulated) == 1 and "九场全中" in simulated[0] and "错 3 场及以上" in simulated[0]
    exact = float(np.prod([0.85 - n / 50 for n in range(1, 10)]))
    shown = float(simulated[0].split("九场全中 ")[1].split("%")[0]) / 100
    assert abs(shown - exact) < 0.015
    assert pick_nine_lines(report) == lines


def test_hit_line_names_every_bucket_and_sums_to_one():
    report = _report(4)
    line = hit_line(report["rows"], report, "14场单选", "全中")
    shares = [float(part.split("%")[0].split(" ")[-1]) for part in line.split("：")[1].split("，")]
    assert len(shares) == 4 and sum(shares) == pytest.approx(100, abs=0.05)


def test_digest_strength_table_only_when_the_forecast_carries_strengths():
    row = dict(
        match_id="jc:1",
        status="predicted",
        league="德甲",
        home="拜仁",
        away="科隆",
        kickoff_utc="2026-11-20T19:30:00Z",
        jc_num="周五006",
        team_strengths=dict(
            home_attack_index=182.4,
            home_defence_index=141.2,
            away_attack_index=88.0,
            away_defence_index=79.6,
            home_expected_goals=2.93,
            away_expected_goals=0.71,
            home_default_strength=False,
            away_default_strength=True,
        ),
    )
    lines = strength_lines([row])
    text = "\n".join(lines)
    assert "#### 进攻 / 防守强度（球队模型）" in text
    assert "| 德甲 / 拜仁 VS 科隆 | 182 / 141 | 88 / 80（默认实力） | 2.93 : 0.71 |" in text
    assert "100 是该模型里所有球队的平均水平" in text
    # Older frozen forecasts carry no strengths: nothing is added, bytes stay as they were.
    assert strength_lines([{**row, "team_strengths": None}]) == []
    assert strength_lines([{k: v for k, v in row.items() if k != "team_strengths"}]) == []
    assert strength_lines([{**row, "status": "excluded"}]) == []


def test_frozen_forecast_row_carries_the_strengths(setup_batch):  # noqa: F811
    from zucai.db import session_scope
    from zucai.report.batch import predict_batch

    engine, path, fitted, _ = setup_batch
    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[path],
            as_of=BATCH_CUTOFF,
            generated=BATCH_CUTOFF,
            match_ids=["jc:1", "jc:2"],
        )
    rows = {row["match_id"]: row for row in batch["predictions"]}
    assert rows["jc:1"]["status"] == "predicted", rows["jc:1"]
    assert rows["jc:1"]["team_strengths"] == fitted.team_strengths("A", "B")
    # A fixture that gets no forecast gets no strengths either.
    assert rows["jc:2"]["status"] == "excluded" and "team_strengths" not in rows["jc:2"]
