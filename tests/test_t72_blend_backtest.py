"""The retrospective selector observes only the first time slice before freezing w."""

import json
from hashlib import sha256

import pytest

from zucai import market_blend_backtest
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


@pytest.mark.parametrize(
    "review_url",
    ["http://github.com/qw5563680/FOOTBALL/pull/259", "https:///pull/259", "https://github.com/"],
)
def test_run_rejects_invalid_review_url_before_reading_inputs(tmp_path, monkeypatch, review_url):
    def unexpected_hash(_):
        pytest.fail("invalid review URL reached input verification")

    monkeypatch.setattr(market_blend_backtest, "_sha", unexpected_hash)
    output = tmp_path / "report"
    with pytest.raises(ValueError, match="审查记录链接"):
        market_blend_backtest.run(tmp_path / "unused-archive", output, review_url)
    assert not output.exists()


@pytest.mark.parametrize("changed", ["manifest", "parameters"])
def test_run_rejects_mismatched_pinned_input_before_restore(tmp_path, monkeypatch, changed):
    from zucai import cloud_feeds

    archive = tmp_path / "archive"
    archive.mkdir()
    manifest_path = archive / "feed_manifest.json"
    project = tmp_path / "project"
    params_path = project / "data/model/params.json"
    params_path.parent.mkdir(parents=True)
    manifest = b'{"fixture": "manifest"}'
    params = b'{"fixture": "parameters"}'
    manifest_path.write_bytes(manifest + b" " if changed == "manifest" else manifest)
    params_path.write_bytes(params + b" " if changed == "parameters" else params)
    monkeypatch.setattr(market_blend_backtest, "ROOT", project)
    monkeypatch.setattr(
        market_blend_backtest, "ARCHIVE_MANIFEST_SHA256", sha256(manifest).hexdigest()
    )
    monkeypatch.setattr(market_blend_backtest, "PARAMETERS_SHA256", sha256(params).hexdigest())

    def unexpected_restore(*args):
        pytest.fail("mismatched input reached database restore")

    monkeypatch.setattr(cloud_feeds, "restore_feeds", unexpected_restore)
    output = tmp_path / "report"
    review = "https://github.com/qw5563680/FOOTBALL/pull/259#issuecomment-7193832203"
    with pytest.raises(ValueError, match="归档或参数与协议固定版本不符"):
        market_blend_backtest.run(archive, output, review)
    assert not output.exists()


def test_actual_rolling_model_is_computed_before_joining_closing(tmp_path, monkeypatch):
    import pandas as pd
    from sqlalchemy.orm import Session

    from zucai.db import get_engine, init_db
    from zucai.market_blend_backtest import Phase, _league_rows, _select
    from zucai.models import ClosingOdds, Match, Team
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
    actual_closing = backtest.read_closing

    def evaluate(frame, market, **kwargs):
        calls.append("predict")
        assert market == {} and "closing" not in kwargs
        assert kwargs["parameters_by_season"] == {"2425": 180}
        return actual_evaluate(frame, market, **kwargs)

    def closing(session, labels):
        calls.append("closing")
        assert len(labels) == 4
        return actual_closing(session, labels)

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
    with Session(engine) as session:
        first = history.loc[in_period].iloc[0]
        home, away = Team(name=first.home), Team(name=first.away)
        session.add_all([home, away])
        session.flush()
        session.add(
            Match(
                match_id=first.match_id,
                source="fd",
                league=first.league,
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=first.kickoff_utc.to_pydatetime(),
            )
        )
        session.flush()
        session.add(ClosingOdds(match_id=first.match_id, bookmaker="B365", h=2, d=10 / 3, a=5))
        session.commit()
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
    assert rows[0]["closing"] == pytest.approx({"h": 0.5, "d": 0.3, "a": 0.2})
    assert rows[-1]["actual"] is None and rows[-1]["team"] is None
    assert all(
        row["team_cutoff_utc"] < row["kickoff_utc"] for row in rows if row["team"] is not None
    )

    weight, grid = _select(rows)
    assert 0 <= weight <= 1 and len(grid) == 101
