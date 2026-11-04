import json
from pathlib import Path

import numpy as np
import pytest

from zucai.predict.base import utc


def observations():
    doc = json.loads((Path(__file__).parents[1] / "data/fixtures/t33/intensity.json").read_bytes())
    rows = []
    for i, values in enumerate(doc["rows"]):
        kickoff = utc("2023-01-01T12:00:00Z") + np.timedelta64(i, "D")
        rows.append(
            dict(
                match_id=f"fd:test{i}",
                kickoff_utc=kickoff,
                available_utc=kickoff + np.timedelta64(3, "h"),
                feature_cutoff=kickoff,
                inputs={
                    k: values[k] for k in ("home_for", "away_for", "home_against", "away_against")
                },
                home_goals=values["home_goals"],
                away_goals=values["away_goals"],
            )
        )
    return rows


def model(ablation=False):
    from zucai.predict.experiments.intensity import StatsIntensity

    return StatsIntensity(stat_key="shots_on_target", window=5, ablation=ablation)


def test_parameters_fitted_and_grid_is_finite_without_manual_probability_adjustment():
    fitted = model().fit(observations(), as_of="2024-01-01T00:00:00Z")
    assert fitted.coefficients["for"] > 0.1
    assert fitted.coefficients["against"] > 0.1
    matrix, meta = fitted.predict(observations()[0]["inputs"])
    assert matrix.shape == (10, 10) and matrix.min() >= 0 and matrix.sum() == pytest.approx(1)
    assert meta["model_name"] == "dc-sot-v1" and meta["model_type"] == "TEAM"
    assert meta["bet_allowed"] is False and meta["p2_ready"] is False
    assert len(meta["model_version"]) == 64
    calibrated, _ = fitted.predict(observations()[0]["inputs"], calibrated=True)
    assert calibrated.sum() == pytest.approx(1)


def test_ablation_removes_against_but_keeps_full_sample_mask():
    rows = observations()
    rows[0]["inputs"]["home_against"] = None
    full = model().fit(rows, as_of="2024-01-01T00:00:00Z")
    ablated = model(True).fit(rows, as_of="2024-01-01T00:00:00Z")
    assert full.training_ids == ablated.training_ids and len(full.training_ids) == 119
    left = rows[1]["inputs"]
    right = {**left, "home_against": 20, "away_against": 20}
    assert np.array_equal(ablated.predict(left)[0], ablated.predict(right)[0])
    assert not np.allclose(full.predict(left)[0], full.predict(right)[0])
    with pytest.raises(ValueError, match="missing"):
        ablated.predict(rows[0]["inputs"])


def test_future_rows_do_not_enter_fit_and_market_fields_are_rejected():
    rows = observations()
    future = {**rows[0], "match_id": "fd:future", "available_utc": utc("2025-01-01T00:00Z")}
    baseline = model().fit(rows, as_of="2024-01-01T00:00:00Z")
    repeated = model().fit([*rows, future], as_of="2024-01-01T00:00:00Z")
    assert repeated.coefficients == baseline.coefficients
    with pytest.raises(ValueError, match="白名单"):
        model().fit(
            [{**rows[0], "inputs": {**rows[0]["inputs"], "market_h": 0.9}}],
            as_of="2024-01-01T00:00:00Z",
        )
    wrong = {**rows[0], "feature_cutoff": utc("2023-06-01T12:00Z")}
    with pytest.raises(ValueError, match="特征截点"):
        model().fit([wrong, *rows[1:]], as_of="2024-01-01T00:00:00Z")
