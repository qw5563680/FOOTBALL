"""Pooled-model contracts use only a labeled, synthetic offline CSV."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from zucai.predict.dixon_coles import DixonColes, InsufficientHistory

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t21/model-history.csv"
CUTOFF = pd.Timestamp("2024-02-15T00:00:00Z")


@pytest.fixture
def history():
    return pd.read_csv(FIXTURE, comment="#", parse_dates=["kickoff_utc", "available_utc"])


def target(**changes):
    return {
        "home": "甲队",
        "away": "乙队",
        "league": "欧冠",
        "source": "jc",
        "kickoff_utc": CUTOFF + pd.Timedelta(days=1),
        **changes,
    }


@pytest.mark.parametrize("calibration", [False, True])
def test_single_league_pooled_is_exactly_the_domestic_fit(history, calibration):
    history["league"] = "英超"
    domestic = DixonColes(draw_calibration=calibration).fit(history, as_of=CUTOFF)
    pooled = DixonColes(pooled=True, draw_calibration=calibration).fit(history, as_of=CUTOFF)
    np.testing.assert_array_equal(domestic.engine._params, pooled.engine._params)
    np.testing.assert_array_equal(
        domestic.predict_score_matrix(target(league="英超")),
        pooled.predict_score_matrix(target(league="英超")),
    )
    assert pooled.prior_parameters == domestic.prior_parameters
    assert pooled.draw_offset == domestic.draw_offset
    assert pooled.calibration_training_rows == domestic.calibration_training_rows
    assert pooled.calibration_skipped_training_rows == domestic.calibration_skipped_training_rows
    assert domestic.league == "英超"
    assert domestic.pooled is False
    assert pooled.training_leagues == {"英超": 45}


def test_mixed_league_fit_has_common_home_parameter_and_eligible_support(history):
    history.attrs["training_leagues"] = ["欧协联", "欧冠", "英超", "德甲", "欧罗巴"]
    model = DixonColes(pooled=True).fit(history, as_of=CUTOFF)
    assert model.league == "欧战" and model.pooled is True
    assert model.training_leagues == {"德甲": 11, "欧冠": 11, "欧罗巴": 11, "英超": 12}
    assert model.training_league_scope == sorted(history.attrs["training_leagues"])
    assert model.prediction_leagues == ["欧冠", "欧罗巴", "欧协联"]
    assert model.team_training_leagues["甲队"] == {"德甲": 2, "欧冠": 5, "欧罗巴": 2, "英超": 6}
    assert sum(model.training_leagues.values()) == model.training_rows
    assert sum(sum(counts.values()) for counts in model.team_training_leagues.values()) == 90
    matrix, metadata = model.predict_with_metadata(target())
    assert matrix.sum() == pytest.approx(1)
    assert metadata["pooled"] is True
    assert metadata["training_leagues"] == model.training_leagues
    assert metadata["home_training_leagues"] == model.team_training_leagues["甲队"]
    assert metadata["away_training_leagues"] == model.team_training_leagues["乙队"]
    assert metadata["home_advantage"] == model.engine.get_params()["home_advantage"]
    assert [key for key in model.engine.get_params() if "home_advantage" in key] == [
        "home_advantage"
    ]
    for league in ["欧冠", "欧罗巴", "欧协联", "其他赛事"]:
        np.testing.assert_array_equal(matrix, model.predict_score_matrix(target(league=league)))
    model.prediction_leagues = ["欧冠"]
    np.testing.assert_array_equal(matrix, model.predict_score_matrix(target(league="其他赛事")))


def test_future_unavailable_and_invalid_availability_never_define_support(history):
    history.loc[0, "available_utc"] = CUTOFF
    history.loc[1, "available_utc"] = pd.NaT
    history.loc[2, "available_utc"] = history.loc[2, "kickoff_utc"]
    history.loc[3, "available_utc"] = history.loc[3, "kickoff_utc"] - pd.Timedelta(hours=1)
    eligible = history.loc[
        (history.kickoff_utc < CUTOFF)
        & (history.available_utc < CUTOFF)
        & (history.available_utc > history.kickoff_utc)
    ].copy()
    changed = history.copy()
    invisible = ~changed.index.isin(eligible.index)
    changed.loc[invisible, "league"] = "未来赛事"
    changed.loc[invisible, "home"] = "未来球队"
    changed.loc[invisible, "home_goals"] = 99
    model = DixonColes(pooled=True, draw_calibration=True).fit(changed, as_of=CUTOFF)
    reference = DixonColes(pooled=True, draw_calibration=True).fit(eligible, as_of=CUTOFF)
    assert model.training_rows == 41
    assert "未来赛事" not in model.training_leagues
    assert "未来赛事" not in model.training_league_scope
    assert "未来球队" not in model.teams
    assert "未来球队" not in model.team_training_leagues
    np.testing.assert_array_equal(model.engine._params, reference.engine._params)
    first, first_metadata = model.predict_with_metadata(target())
    second, second_metadata = reference.predict_with_metadata(target())
    np.testing.assert_array_equal(first, second)
    assert first_metadata == second_metadata


def test_opaque_provider_names_remain_separate_teams(history):
    model = DixonColes(pooled=True).fit(history, as_of=CUTOFF)
    assert model.teams == {"甲队", "乙队", "丙队", "丁队", "fdorg:team:1", "apif:team:1"}
    for name in ["fdorg:team:1", "apif:team:1"]:
        assert f"attack_{name}" in model.engine.get_params()
        assert f"defence_{name}" in model.engine.get_params()
        _, metadata = model.predict_with_metadata(target(home=name))
        assert metadata["unknown_team_prior"] is False
        assert metadata["home_training_leagues"] == model.team_training_leagues[name]
    assert model.team_training_leagues["fdorg:team:1"] != model.team_training_leagues["apif:team:1"]


def test_unknown_team_uses_same_weak_quartile_prior_and_ignores_market_inputs(history):
    model = DixonColes(pooled=True).fit(history, as_of=CUTOFF)
    matrix, metadata = model.predict_with_metadata(target(home="未见球队"))
    assert matrix.shape == (10, 10) and matrix.min() >= 0
    assert matrix.sum() == pytest.approx(1)
    assert metadata["unknown_team_prior"] is True
    assert metadata["prior_method"] == "weakest_quartile_half_shrinkage"
    assert metadata["home_training_leagues"] == {}
    assert metadata["away_training_leagues"] == model.team_training_leagues["乙队"]
    np.testing.assert_array_equal(
        matrix,
        model.predict_score_matrix(
            target(home="未见球队", market_home=0.999, closing=(0.99, 0.005, 0.005))
        ),
    )
    disabled = DixonColes(pooled=True, unknown_team_prior=False).fit(history, as_of=CUTOFF)
    with pytest.raises(InsufficientHistory):
        disabled.predict_score_matrix(target(home="未见球队"))
    np.testing.assert_array_equal(
        model.predict_score_matrix(target()), disabled.predict_score_matrix(target())
    )


def test_pooled_roundtrip_and_legacy_domestic_load(history, tmp_path):
    model = DixonColes(pooled=True).fit(history, as_of=CUTOFF)
    model.prediction_leagues = ["欧冠"]
    path = tmp_path / "pooled.pkl"
    model.save(path)
    restored = DixonColes.load(path)
    assert restored.pooled is True and restored.league == "欧战"
    assert restored.prediction_leagues == ["欧冠"]
    assert restored.training_leagues == model.training_leagues
    assert restored.training_league_scope == model.training_league_scope
    assert restored.team_training_leagues == model.team_training_leagues
    before, before_metadata = model.predict_with_metadata(target())
    after, after_metadata = restored.predict_with_metadata(target())
    np.testing.assert_array_equal(before, after)
    assert before_metadata == after_metadata
    history["league"] = "英超"
    domestic = DixonColes().fit(history, as_of=CUTOFF)
    domestic.__dict__.pop("pooled", None)
    domestic.save(path)
    legacy = DixonColes.load(path)
    assert legacy.pooled is False
    np.testing.assert_array_equal(
        legacy.predict_score_matrix(target(league="英超")),
        domestic.predict_score_matrix(target(league="英超")),
    )
    with pytest.raises(ValueError, match="联赛"):
        legacy.predict_score_matrix(target())


def test_default_rejects_mixed_leagues_and_pooled_keeps_source_cutoff_guards(history):
    with pytest.raises(ValueError, match="联赛"):
        DixonColes().fit(history, as_of=CUTOFF)
    model = DixonColes(pooled=True).fit(history, as_of=CUTOFF)
    with pytest.raises(ValueError, match="来源"):
        model.predict_score_matrix(target(source="unselected"))
    with pytest.raises(ValueError, match="截点"):
        model.predict_score_matrix(target(kickoff_utc=CUTOFF))
    history.attrs["sources"] = ["jc"]
    with pytest.raises(ValueError, match="来源"):
        DixonColes(pooled=True).fit(history, as_of=CUTOFF)


def test_pooled_keeps_cross_source_overlap_guard(history):
    duplicate = history.iloc[[0]].copy()
    duplicate["source"] = "jc"
    duplicate["match_id"] = "jc:synthetic-duplicate"
    mixed = pd.concat([history, duplicate], ignore_index=True)
    with pytest.raises(ValueError, match="重复|重叠"):
        DixonColes(pooled=True).fit(mixed, as_of=CUTOFF)
