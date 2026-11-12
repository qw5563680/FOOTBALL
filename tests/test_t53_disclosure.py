"""T53 uses frozen evidence to disclose prior reliance without changing probabilities."""

import csv
import io
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from test_prediction import CUTOFF
from test_prediction import games as games
from test_report_render import frozen_batch as frozen_batch
from zucai.predict.dixon_coles import DixonColes

METHOD = "weakest_quartile_half_shrinkage"


def fixture_row(**metadata):
    return {"home": "主队甲", "away": "客队乙", "metadata": metadata}


@pytest.mark.parametrize("home,away", [(False, False), (True, False), (False, True), (True, True)])
def test_explicit_sides_are_disclosed_without_mutating_the_prediction(home, away):
    from zucai.prediction_disclosure import prior_disclosure, prior_label, uses_unknown_team_prior

    row = fixture_row(
        unknown_team_prior=home or away,
        home_unknown_team=home,
        away_unknown_team=away,
        prior_method=METHOD if home or away else None,
    )
    before = deepcopy(row)
    detail = prior_disclosure(row)
    assert detail["used"] is (home or away)
    assert (detail["home"], detail["away"]) == (home, away)
    assert uses_unknown_team_prior(row) is (home or away)
    text = prior_label(row)
    if home or away:
        assert "默认实力" in text and METHOD in text
        assert ("主队甲" in text) is home
        assert ("客队乙" in text) is away
    else:
        assert text == ""
    assert row == before


@pytest.mark.parametrize("home,away", [(False, False), (True, False), (False, True), (True, True)])
def test_original_pooled_training_evidence_identifies_missing_sides(home, away):
    from zucai.prediction_disclosure import prior_disclosure

    row = fixture_row(
        unknown_team_prior=home or away,
        pooled=True,
        home_training_leagues={} if home else {"欧冠": 3},
        away_training_leagues={} if away else {"欧罗巴": 4},
        prior_method=METHOD if home or away else None,
    )
    detail = prior_disclosure(row)
    assert detail["used"] is (home or away)
    assert (detail["home"], detail["away"]) == (home, away)


@pytest.mark.parametrize(
    "metadata",
    [
        {"unknown_team_prior": True},
        {"unknown_team_prior": True, "home_unknown_team": False, "away_unknown_team": False},
        {"unknown_team_prior": True, "pooled": True, "home_training_leagues": {}},
        {
            "unknown_team_prior": True,
            "pooled": True,
            "home_training_leagues": {"欧冠": 0},
            "away_training_leagues": {},
        },
    ],
)
def test_incomplete_or_inconsistent_sides_cannot_bypass_affirmative_prior(metadata):
    from zucai.prediction_disclosure import prior_disclosure, prior_label, uses_unknown_team_prior

    row = fixture_row(**metadata)
    detail = prior_disclosure(row)
    assert detail["used"] is True
    assert detail["home"] is detail["away"] is None
    assert uses_unknown_team_prior(row)
    assert "缺失方未记录" in prior_label(row)
    assert "先验方法未记录" in prior_label(row)


def test_explicit_missing_side_cannot_be_hidden_by_false_aggregate_flag():
    from zucai.prediction_disclosure import prior_disclosure, uses_unknown_team_prior

    row = fixture_row(unknown_team_prior=False, home_unknown_team=False, away_unknown_team=True)
    assert uses_unknown_team_prior(row)
    assert prior_disclosure(row)["away"] is True


def test_propagated_false_cannot_override_original_true():
    from zucai.prediction_disclosure import prior_disclosure, uses_unknown_team_prior

    row = fixture_row(unknown_team_prior=True, prior_method=METHOD)
    row["prior_disclosure"] = {"used": False, "home": False, "away": False}
    assert uses_unknown_team_prior(row)
    assert prior_disclosure(row)["method"] == METHOD


def test_real_frozen_ac_milan_ferencvaros_identifies_away_side():
    from zucai.prediction_disclosure import prior_disclosure, prior_label

    fixture = Path(__file__).parents[1] / "data/fixtures/t53/forecast-three.json"
    batch = json.loads(fixture.read_bytes())
    row = next(r for r in batch["predictions"] if r["match_id"] == "jc:2042523")
    before = deepcopy(row)
    detail = prior_disclosure(row)
    assert detail["used"] is True
    assert detail["home"] is False
    assert detail["away"] is True
    assert detail["evidence"] == "pooled_training_leagues"
    assert "客队 费伦茨" in prior_label(row)
    assert row == before


@pytest.mark.parametrize("metadata", [None, {}, {"unknown_team_prior": "false"}])
def test_unrecorded_prior_is_not_certified_known_and_does_not_change_eligibility(metadata):
    from zucai.prediction_disclosure import prior_disclosure, prior_label, uses_unknown_team_prior

    row = {"metadata": metadata}
    assert prior_disclosure(row)["used"] is None
    assert prior_disclosure(row)["home"] is None
    assert prior_disclosure(row)["away"] is None
    assert not uses_unknown_team_prior(row)
    assert prior_label(row) == ""


def test_propagated_disclosure_retains_the_original_prior_evidence():
    from zucai.prediction_disclosure import prior_disclosure, prior_label

    original = fixture_row(
        unknown_team_prior=True,
        home_unknown_team=False,
        away_unknown_team=True,
        prior_method=METHOD,
    )
    detail = prior_disclosure(original)
    derived = {"home": original["home"], "away": original["away"], "prior_disclosure": detail}
    assert prior_disclosure(derived) == detail
    assert prior_label(derived) == prior_label(original)
    prior_disclosure(derived)["used"] = False
    assert detail["used"] is True


def test_real_model_adds_exact_sides_and_preserves_known_probability_bytes(games):
    model = DixonColes().fit(games, as_of=CUTOFF)
    params_before = deepcopy(model.engine.get_params())
    prior_before = deepcopy(model.prior_parameters)
    target = {"home": "A", "away": "B", "kickoff_utc": CUTOFF + timedelta(hours=1)}
    raw = model.engine.predict("A", "B", max_goals=15, normalize=False).grid[:10, :10]
    expected = raw / raw.sum()
    matrix, metadata = model.predict_with_metadata(target)
    assert matrix.tobytes() == expected.tobytes()
    assert metadata["home_unknown_team"] is False
    assert metadata["away_unknown_team"] is False
    for home, away in (("新主队", "B"), ("A", "新客队"), ("新主队", "新客队")):
        grid, metadata = model.predict_with_metadata({**target, "home": home, "away": away})
        assert metadata["home_unknown_team"] is (home not in model.teams)
        assert metadata["away_unknown_team"] is (away not in model.teams)
        assert metadata["unknown_team_prior"] is True
        assert metadata["prior_method"] == METHOD
        assert np.isclose(grid.sum(), 1)
    assert model.engine.get_params() == params_before
    assert model.prior_parameters == prior_before


def test_single_match_markdown_and_csv_disclose_side_and_keep_frozen_values(frozen_batch):
    from zucai.report.render import _csv_bytes, _markdown

    row = frozen_batch["predictions"][0]
    row["metadata"].update(
        unknown_team_prior=True,
        home_unknown_team=False,
        away_unknown_team=True,
        prior_method=METHOD,
    )
    before = json.dumps(frozen_batch, ensure_ascii=False, sort_keys=True)
    markdown = _markdown(frozen_batch)
    assert "默认实力" in markdown and METHOD.replace("_", r"\_") in markdown
    assert "缺失方未记录" not in markdown
    assert "60.00%" in markdown and r"客队\[乙\]" in markdown
    exported = next(csv.DictReader(io.StringIO(_csv_bytes(frozen_batch).decode("utf-8-sig"))))
    assert exported["home_unknown_team"] == "False"
    assert exported["away_unknown_team"] == "True"
    assert exported["prior_method"] == METHOD
    assert exported["model_h"] == "0.6"
    assert json.dumps(frozen_batch, ensure_ascii=False, sort_keys=True) == before
