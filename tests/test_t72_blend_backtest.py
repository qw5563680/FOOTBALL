"""The retrospective selector observes only the first time slice before freezing w."""

import json

import pytest

from zucai.market_blend_backtest import PHASES, evaluate_phases


def _observation(match, actual="h", *, paired=True, at=None):
    return {
        "match_id": match,
        "kickoff_utc": at,
        "league": "英超",
        "actual": actual,
        "team": {"h": 0.8, "d": 0.1, "a": 0.1},
        "closing": {"h": 0.1, "d": 0.1, "a": 0.8} if paired else None,
    }


def test_weight_is_frozen_before_validation_and_holdout(tmp_path):
    seen = []

    def fetch(phase):
        seen.append(phase.name)
        if phase.name != "training":
            fixed = json.loads((tmp_path / "weight-selection.json").read_bytes())
            assert fixed["weight"] == 0
        return [
            _observation(
                f"fd:{phase.name}",
                "h" if phase.name == "training" else "a",
                at=phase.start,
            ),
            _observation(f"jc:{phase.name}-unpaired", paired=False, at=phase.start),
        ], {}

    report = evaluate_phases(fetch, tmp_path)
    assert seen == [phase.name for phase in PHASES]
    assert report["weight"] == 0
    for name, row in report["phases"].items():
        assert row["total"] == 2 and row["paired"] == 1
        assert row["excluded"] == {"closing_quote_missing": 1}
        assert row["metrics"]["blend"] == row["metrics"]["team"]
        assert row["metrics"]["closing"]["first_choice_hit"] == (name != "training")
        results = (tmp_path / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        assert json.loads(results[0])["blend"] == _observation("fd:unused")["team"]
        assert json.loads(results[1])["blend"] is None
    with pytest.raises(FileExistsError):
        evaluate_phases(lambda _: pytest.fail("previous output was opened again"), tmp_path)


@pytest.mark.parametrize("wrong_phase", [True, False])
def test_wrong_or_duplicate_match_is_rejected_before_selecting(tmp_path, wrong_phase):
    rows = [_observation("fd:one", at=PHASES[0].end if wrong_phase else PHASES[0].start)]
    if not wrong_phase:
        rows += rows
    with pytest.raises(ValueError, match="窗口|重复"):
        evaluate_phases(lambda _: (rows, {}), tmp_path)
    assert not (tmp_path / "weight-selection.json").exists()


def test_no_paired_training_never_calls_validation(tmp_path):
    def fetch(phase):
        assert phase == PHASES[0]
        return [_observation("fd:one", paired=False, at=phase.start)], {}

    with pytest.raises(ValueError, match="训练段没有共同样本"):
        evaluate_phases(fetch, tmp_path)
    assert not (tmp_path / "weight-selection.json").exists()


def test_actual_rolling_model_is_computed_before_joining_closing(tmp_path, monkeypatch):
    import pandas as pd

    from zucai.db import get_engine, init_db
    from zucai.market_blend_backtest import Phase, _league_rows
    from zucai.predict import backtest

    source = []
    for index in range(200):
        kickoff = pd.Timestamp("2022-07-10T12:00:00Z") + pd.Timedelta(days=4 * index)
        home = index % 4
        source.append(
            {
                "match_id": f"fd:{index}",
                "source": "fd",
                "league": "英超",
                "home": "ABCD"[home],
                "away": "ABCD"[(home + 1 + (index // 4) % 3) % 4],
                "kickoff_utc": kickoff,
                "available_utc": kickoff + pd.Timedelta(hours=3),
                "available_source": "assumed",
                "home_goals": index % 4,
                "away_goals": (index // 3) % 3,
                "counterpart_id": None,
            }
        )
    calls = []
    actual_evaluate = backtest.evaluate

    def evaluate(frame, market, **kwargs):
        calls.append("predict")
        assert market == {} and "closing" not in kwargs
        assert kwargs["parameters_by_season"] == {"2425": 180}
        return actual_evaluate(frame, market, **kwargs)

    def closing(session, labels):
        calls.append("closing")
        assert len(labels) == 4
        return {labels.iloc[0].match_id: (0.5, 0.3, 0.2)}

    history = pd.DataFrame(source)
    start = "2024-07-01T00:00:00+00:00"
    end = "2024-07-17T00:00:00+00:00"
    in_period = (history.kickoff_utc >= pd.Timestamp(start)) & (
        history.kickoff_utc < pd.Timestamp(end)
    )
    history.loc[history.loc[in_period].index[-1], "available_utc"] = pd.Timestamp(end)
    database = tmp_path / "synthetic.sqlite"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    monkeypatch.setattr(backtest, "read_history", lambda session, league, sources: history)
    monkeypatch.setattr(backtest, "evaluate", evaluate)
    monkeypatch.setattr(backtest, "read_closing", closing)

    rows, folds = _league_rows(
        database,
        Phase("training", start, end),
        "英超",
        {
            "evaluation_windows": [
                {
                    "validation_season": "2425",
                    "selection_as_of_utc": start,
                    "half_life_days": 180,
                },
                {
                    "validation_season": "2526",
                    "selection_as_of_utc": "2025-07-02T00:00:00Z",
                    "half_life_days": 730,
                },
            ]
        },
    )
    assert calls == ["predict", "closing"]
    assert all(fold["training_games"] > 100 for fold in folds)
    assert len(rows) == 4 and sum(row["team"] is not None for row in rows) == 3
    assert rows[0]["closing"] == {"h": 0.5, "d": 0.3, "a": 0.2}
    assert rows[-1]["actual"] is None and rows[-1]["team"] is None
    assert all(
        row["team_cutoff_utc"] < row["kickoff_utc"] for row in rows if row["team"] is not None
    )
