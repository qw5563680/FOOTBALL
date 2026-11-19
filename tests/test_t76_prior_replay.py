"""T76: the small-sample prior replay compares all versions on the same fixtures and
cut-offs, never looks ahead, and fixes k on the selection seasons only."""

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.experiments.prior_replay import (
    CONFIG,
    choose_k,
    columns,
    load_config,
    prior_targets,
    replay_league,
    score,
    segment,
    shrunk,
    view_probabilities,
)

END = pd.Timestamp("2025-06-01", tz="UTC")
ENTRY = {
    "evaluation_windows": [
        {"validation_season": "2425", "half_life_days": 365.0, "draw_calibration": False}
    ]
}


def _config(**changes):
    config = dict(
        version=1,
        pairs=[["甲级", "乙级"]],
        selection_seasons=["2425"],
        validation_seasons=["2526"],
        reported_separately=[],
        k_candidates=[3, 6],
        history_days=365,
        segment_upper_bounds=[0, 5, 12],
        favourite_threshold=0.8,
        bootstrap=dict(unit="utc_week", resamples=200, seed=3301),
    )
    config.update(changes)
    return config


def _league(name, clubs, start, weeks, seed):
    rng = np.random.default_rng(seed)
    rows = []
    for week in range(weeks):
        kickoff = start + timedelta(weeks=week, hours=15)
        order = list(rng.permutation(len(clubs)))
        for slot in range(len(clubs) // 2):
            home, away = clubs[order[2 * slot]], clubs[order[2 * slot + 1]]
            rows.append(
                (
                    f"demo:{name}:{week}-{slot}",
                    "demo",
                    name,
                    kickoff,
                    kickoff + timedelta(hours=3),
                    home,
                    away,
                    int(rng.poisson(1.5)),
                    int(rng.poisson(1.1)),
                )
            )
    return rows


FIELDS = [
    "match_id",
    "source",
    "league",
    "kickoff_utc",
    "available_utc",
    "home",
    "away",
    "home_goals",
    "away_goals",
]


@pytest.fixture(scope="module")
def frames():
    old = datetime(2023, 8, 5, tzinfo=UTC)
    new = datetime(2024, 8, 3, tzinfo=UTC)
    stayed = ["A", "B", "C", "D", "E"]
    top = _league("甲级", [*stayed, "F"], old, 36, 1) + _league("甲级", [*stayed, "N"], new, 30, 2)
    second = _league("乙级", ["N", "P", "Q", "R"], old, 36, 3) + _league(
        "乙级", ["F", "P", "Q", "R"], new, 30, 4
    )
    return pd.DataFrame(top, columns=FIELDS), pd.DataFrame(second, columns=FIELDS)


def test_repository_parameters_load_and_keep_selection_apart_from_validation():
    config = load_config()
    assert CONFIG.is_file() and config["bootstrap"]["seed"] == 3301
    assert not set(config["selection_seasons"]) & set(config["validation_seasons"])
    assert config["segment_upper_bounds"] == [0, 5, 12]


@pytest.mark.parametrize(
    "changes",
    [
        dict(validation_seasons=["2425"]),
        dict(k_candidates=[0, 3]),
        dict(k_candidates=[]),
        dict(segment_upper_bounds=[5, 12]),
        dict(segment_upper_bounds=[0, 12, 5]),
        dict(history_days=0),
        dict(version=2),
    ],
)
def test_invalid_parameters_are_refused(tmp_path, changes):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(_config(**changes)), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_segments_use_the_smaller_side_and_the_stated_boundaries():
    bounds = [0, 5, 12]
    assert [segment(n, 40, bounds) for n in (0, 1, 5, 6, 12, 13, 99)] == [
        "0",
        "1-5",
        "1-5",
        "6-12",
        "6-12",
        "13+",
        "13+",
    ]
    assert segment(40, 3, bounds) == "1-5"


def test_shrinkage_formula_and_targets():
    params = {"attack_A": 1.0, "defence_A": -1.0}
    weak, mean = (-0.4, 0.3), (0.0, 0.0)
    common = dict(weak=weak, mean=mean)
    assert shrunk(params, "A", n=7, k=0, in_league_last_season=True, **common) == (1.0, -1.0)
    # A side that was in the league last season is pulled to the league mean ...
    assert shrunk(params, "A", n=6, k=6, in_league_last_season=True, **common) == (0.5, -0.5)
    # ... a side new to the league towards the weak-team target.
    assert shrunk(params, "A", n=6, k=6, in_league_last_season=False, **common) == pytest.approx(
        (0.3, -0.35)
    )
    # No known matches: the target alone. Many matches: almost the estimate.
    none = shrunk(params, "A", n=0, k=3, in_league_last_season=False, **common)
    assert none == pytest.approx(weak, abs=1e-12)
    far = shrunk(params, "A", n=3000, k=3, in_league_last_season=True, **common)
    assert far == pytest.approx((1.0, -1.0), abs=2e-3)
    # A side the fit has never seen starts from the weak-team estimate.
    unseen = shrunk(params, "Z", n=0, k=3, in_league_last_season=False, **common)
    assert unseen == pytest.approx(weak, abs=1e-12)


def test_substituting_a_fits_own_strengths_reproduces_the_formal_forecast(frames):
    top, _ = frames
    cutoff = pd.Timestamp("2025-01-01", tz="UTC")
    model = DixonColes(365.0, unknown_team_prior=True).fit(top, as_of=cutoff)
    params = model.engine.get_params()
    row = dict(home="A", away="N", league="甲级", kickoff_utc=cutoff + pd.Timedelta(days=2))
    formal = had(model.predict_with_metadata(row)[0])
    own = [(params[f"attack_{t}"], params[f"defence_{t}"]) for t in ("A", "N")]
    same = view_probabilities(model, row, own)
    assert all(abs(same[k] - formal[k]) < 1e-9 for k in "hda")
    weak, mean = prior_targets(params, model.teams, model.prior_parameters)
    assert weak == (model.prior_parameters["attack"], model.prior_parameters["defence"])
    pulled = view_probabilities(model, row, [mean, mean])
    assert abs(sum(pulled.values()) - 1) < 1e-9 and pulled != same


def test_replay_pairs_every_version_on_the_same_fixture_and_cutoff(frames):
    top, second = frames
    config = _config()
    records = list(replay_league(top, second, "甲级", ENTRY, "2425", config, end=END, max_weeks=6))
    assert len(records) == 18 and len({r["match_id"] for r in records}) == 18
    for record in records:
        assert pd.Timestamp(record["cutoff_utc"]) < pd.Timestamp(record["kickoff_utc"])
        for name in columns(config):
            total = sum(record[f"{name}:{outcome}"] for outcome in "hda")
            assert abs(total - 1) < 1e-9
        assert not any("odds" in key or "market" in key for key in record)
    # The promoted side has no league history in week one and no previous season there.
    first = next(r for r in records if "N" in (r["home"], r["away"]))
    side = "home" if first["home"] == "N" else "away"
    assert first[f"n_{side}"] == 0 and first[f"stayed_{side}"] == 0 and first["segment"] == "0"
    later = [r for r in records if "N" in (r["home"], r["away"])][-1]
    assert later[f"n_{'home' if later['home'] == 'N' else 'away'}"] == 5
    assert later["segment"] == "1-5"
    # A side that stayed keeps a full window and the league-mean target.
    stay = next(r for r in records if r["home"] == "A" or r["away"] == "A")
    assert stay["stayed_home" if stay["home"] == "A" else "stayed_away"] == 1


def test_results_after_a_cutoff_cannot_change_that_weeks_records(frames):
    top, second = frames
    config = _config()
    before = list(replay_league(top, second, "甲级", ENTRY, "2425", config, end=END, max_weeks=3))
    last_cutoff = max(pd.Timestamp(r["cutoff_utc"]) for r in before)
    changed, other = top.copy(), second.copy()
    replayed = {r["match_id"] for r in before}
    for frame in (changed, other):
        late = (frame.available_utc >= last_cutoff) & ~frame.match_id.isin(replayed)
        frame.loc[late, ["home_goals", "away_goals"]] = [9, 0]
    after = list(replay_league(changed, other, "甲级", ENTRY, "2425", config, end=END, max_weeks=3))
    assert after == before


def _row(season, week, actual, variants):
    return dict(
        league="甲级",
        season=season,
        segment="1-5",
        kickoff_utc=(datetime(2025, 1, 6, tzinfo=UTC) + timedelta(weeks=week)).isoformat(),
        actual=actual,
        variants={name: {"probabilities": list(p)} for name, p in variants.items()},
    )


def _rows():
    good, bad, flat = (0.7, 0.2, 0.1), (0.1, 0.2, 0.7), (0.34, 0.33, 0.33)
    rows = []
    for week in range(6):
        # Selection season: k=6 is better. Validation season: k=3 would look better.
        for season, three, six in (("2425", bad, good), ("2526", good, bad)):
            rows.append(
                _row(
                    season,
                    week,
                    0,
                    {
                        "dc": flat,
                        "dc-prior-3": three,
                        "dc-prior-6": six,
                        "dc-tier": flat,
                        "dc-prior-tier-3": three,
                        "dc-prior-tier-6": six,
                    },
                )
            )
    return rows


def test_k_is_chosen_on_the_selection_seasons_only():
    config = _config()
    k, scores = choose_k(_rows(), config)
    assert k == 6 and scores[6] < scores[3]
    with pytest.raises(ValueError):
        choose_k([r for r in _rows() if r["season"] == "2526"], config)


def test_score_is_segmented_and_repeatable_with_the_fixed_seed():
    config = _config()
    first, again = score(_rows(), config), score(_rows(), config)
    assert first == again
    assert first["selected_k"] == 6 and first["versions"][1] == "dc-prior-6"
    table = first["groups"]["validation"]
    assert set(table) == {"1-5", "all"} and table["1-5"]["n"] == 6
    difference = table["1-5"]["prior_minus_dc"]["log_loss"]
    # On the validation season the selected k is the worse one: the comparison shows it.
    assert difference["difference"] > 0 and difference["interval"][0] > 0
    assert table["1-5"]["favourites"]["dc"]["n"] == 0
