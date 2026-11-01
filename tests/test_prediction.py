from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from zucai.predict.backtest import evaluate, market_probabilities
from zucai.predict.derive import crs, had, hafu, hhad, ttg
from zucai.predict.dixon_coles import DixonColes

START = datetime(2020, 1, 1, tzinfo=UTC)
CUTOFF = START + timedelta(days=35)


@pytest.fixture
def games():
    rng = np.random.default_rng(25)
    clubs = ["A", "B", "C", "D"]
    rows = []
    for day in range(60):
        kickoff = START + timedelta(days=day, hours=13)
        for slot in range(2):
            home = (day + slot * 2) % 4
            away = (home + 1 + day % 3) % 4
            rows.append(
                (
                    f"demo:{day}-{slot}",
                    "demo",
                    "L",
                    kickoff,
                    kickoff + timedelta(hours=2),
                    clubs[home],
                    clubs[away],
                    rng.poisson(1.6),
                    rng.poisson(1.1),
                )
            )
    return pd.DataFrame(
        rows,
        columns=[
            "match_id",
            "source",
            "league",
            "kickoff_utc",
            "available_utc",
            "home",
            "away",
            "home_goals",
            "away_goals",
        ],
    )


def test_fit_cutoff_model_roundtrip_and_unknown_team(games, tmp_path):
    games.loc[0, "available_utc"] = CUTOFF
    games.loc[1, "available_utc"] = pd.NaT
    target = {"home": "A", "away": "B", "kickoff_utc": CUTOFF + timedelta(hours=1)}
    model = DixonColes().fit(games, as_of=CUTOFF)
    before = model.predict_score_matrix(target)
    assert model.training_rows == 68 and before.shape == (10, 10)
    assert before.min() >= 0 and np.isclose(before.sum(), 1)
    changed = games.copy()
    invisible = changed.available_utc.isna() | (changed.available_utc >= CUTOFF)
    changed.loc[invisible, "home_goals"] = 30
    np.testing.assert_allclose(
        DixonColes().fit(changed, as_of=CUTOFF).predict_score_matrix(target), before
    )
    path = tmp_path / "models" / "dc.pkl"
    model.save(path)
    np.testing.assert_array_equal(DixonColes.load(path).predict_score_matrix(target), before)
    with pytest.raises(ValueError, match="球队"):
        model.predict_score_matrix({**target, "home": "new"})
    with pytest.raises(ValueError, match="截点"):
        model.predict_score_matrix({**target, "kickoff_utc": START})


def test_weekly_backtest_uses_prior_weeks_and_separates_market(games):
    since = START + timedelta(days=33)  # Monday in UTC
    until = START + timedelta(days=60)
    first = evaluate(games, {}, since=since, as_of=until)
    assert first["model"]["n"] == 54 and first["market"]["n"] == 0
    quotes = {games.iloc[-1].match_id: (0.2, 0.3, 0.5)}
    second = evaluate(games, quotes, since=since, as_of=until)
    assert second["model"] == first["model"]
    assert second["paired_model"]["n"] == second["market"]["n"] == 1
    assert sum(b["n"] for b in second["model"]["calibration"]) == 3 * 54


def test_last_complete_market_snapshot_excludes_kickoff():
    old = CUTOFF - timedelta(hours=1)
    recent = CUTOFF - timedelta(minutes=1)
    rows = [
        (key, time, selection, price)
        for key, time, prices in [
            ("a", old, (2, 3, 6)),
            ("a", recent, (5, 2, 4)),
            ("a", CUTOFF, (99, 1.2, 3)),
        ]
        for selection, price in zip("hda", prices, strict=True)
    ]
    rows += [("a", CUTOFF - timedelta(seconds=1), "d", 3), ("b", old, "h", 2)]
    probs = market_probabilities(rows, {"a": CUTOFF, "b": CUTOFF})
    weights = 1 / np.array([5.0, 2.0, 4.0])
    np.testing.assert_allclose(probs["a"], weights / weights.sum())
    assert "b" not in probs


def test_score_derived_markets_preserve_mass_and_half_time_marginal():
    grid = np.zeros((10, 10))
    grid[2, 1] = 0.7
    grid[6, 6] = 0.3
    assert had(grid) == pytest.approx({"h": 0.7, "d": 0.3, "a": 0})
    assert hhad(grid, -1) == pytest.approx({"h": 0, "d": 0.7, "a": 0.3})
    assert hhad(grid.T, 1)["h"] == pytest.approx(hhad(grid, -1)["a"])
    assert ttg(grid)["7+"] == pytest.approx(0.3)
    assert crs(grid)["2:1"] == pytest.approx(0.7)
    assert crs(grid)["平其他"] == pytest.approx(0.3)
    joint = hafu(grid)
    assert sum(joint.values()) == pytest.approx(1)
    for outcome, probability in had(grid).items():
        assert sum(joint[half + outcome] for half in "hda") == pytest.approx(probability)
    for fn in (had, ttg, crs, hafu):
        with pytest.raises(ValueError):
            fn(np.zeros((10, 10)))


def test_train_cli_market_and_fit_parameters_are_independent(games, tmp_path, capsys):
    import json

    from zucai.cli import main
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, Odds, Result, Team

    db = tmp_path / "sample.db"
    engine = get_engine(db)
    init_db(engine)
    with session_scope(engine) as session:
        clubs = {name: Team(name=name) for name in "ABCD"}
        session.add_all(clubs.values())
        session.flush()
        results = []
        for row in games.itertuples():
            session.add(
                Match(
                    match_id=row.match_id,
                    source=row.source,
                    league=row.league,
                    home_team_id=clubs[row.home].id,
                    away_team_id=clubs[row.away].id,
                    kickoff_utc=row.kickoff_utc,
                    status="finished",
                )
            )
            results.append(
                Result(
                    match_id=row.match_id,
                    home_goals=row.home_goals,
                    away_goals=row.away_goals,
                    available_utc=row.available_utc,
                )
            )
        session.flush()
        session.add_all(results)
    path = tmp_path / "dc.pkl"
    argv = [
        "train",
        "--model",
        "dixon-coles",
        "--db",
        str(db),
        "--league",
        "L",
        "--source",
        "demo",
        "--since",
        "2020-02-03",
        "--as-of",
        "2020-03-01",
        "--output",
        str(path),
    ]
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    params = DixonColes.load(path).engine._params.copy()
    target = games.iloc[-1]
    with session_scope(engine) as session:
        for selection, price in zip("hda", [4, 3, 2], strict=True):
            session.add(
                Odds(
                    match_id=target.match_id,
                    pool="had",
                    selection=selection,
                    price=price,
                    snapshot_utc=target.kickoff_utc - timedelta(minutes=30),
                )
            )
    assert main(argv) == 0
    changed = json.loads(capsys.readouterr().out)
    assert first["backtest"]["market"]["n"] == 0 and changed["backtest"]["market"]["n"] == 1
    assert changed["backtest"]["model"] == first["backtest"]["model"]
    np.testing.assert_array_equal(params, DixonColes.load(path).engine._params)
    engine.dispose()


def test_t01_database_migration_and_score_correction(tmp_path):
    import sqlite3

    from sqlalchemy import text

    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, Team

    path = tmp_path / "old.db"
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        h, a = Team(name="H"), Team(name="A")
        session.add_all([h, a])
        session.flush()
        session.add(
            Match(
                match_id="jc:old",
                league="L",
                source="jc",
                home_team_id=h.id,
                away_team_id=a.id,
                kickoff_utc=START,
                status="finished",
            )
        )
    with sqlite3.connect(path) as db:
        db.executescript("""
            DROP TABLE results;
            CREATE TABLE results(match_id TEXT PRIMARY KEY REFERENCES matches(match_id),
                                 home_goals INTEGER NOT NULL, away_goals INTEGER NOT NULL,
                                 ht_home_goals INTEGER, ht_away_goals INTEGER);
            INSERT INTO results VALUES ('jc:old', 2, 0, NULL, NULL);
        """)
    init_db(engine)
    init_db(engine)
    with session_scope(engine) as session:
        assert session.execute(text("SELECT home_goals, available_utc FROM results")).one() == (
            2,
            None,
        )
        session.execute(text("UPDATE results SET available_utc='2020-01-02T00:00:00.000000Z'"))
        session.execute(text("UPDATE results SET home_goals=1"))
        corrected = session.scalar(text("SELECT available_utc FROM results"))
        assert datetime.fromisoformat(corrected) > CUTOFF
    engine.dispose()
