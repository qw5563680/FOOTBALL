"""PR136 preserves native legacy fitting; all data are offline synthetic fixtures."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.backtest import read_history
from zucai.predict.dixon_coles import DixonColes

ROOT = Path(__file__).parents[1]
LEGACY = ("英超", "西甲", "意甲", "德甲", "法甲", "葡超", "巴甲")


def database_for(tmp_path, rows):
    engine = get_engine(tmp_path / "legacy.db")
    init_db(engine)
    with session_scope(engine) as session:
        teams = {
            name: Team(name=name) for name in sorted({r[k] for r in rows for k in ("home", "away")})
        }
        session.add_all(teams.values())
        session.flush()
        for row in rows:
            session.add(
                Match(
                    match_id=row["match_id"],
                    source=row["source"],
                    league=row["league"],
                    home_team_id=teams[row["home"]].id,
                    away_team_id=teams[row["away"]].id,
                    kickoff_utc=row["kickoff_utc"],
                    status="finished",
                )
            )
            session.flush()
            session.add(
                Result(
                    **{
                        k: row[k]
                        for k in (
                            "match_id",
                            "home_goals",
                            "away_goals",
                            "available_utc",
                            "available_source",
                        )
                    }
                )
            )
    return engine


@pytest.mark.parametrize("league", LEGACY)
def test_seven_legacy_inputs_fit_and_probabilities_follow_unchanged_base_path(tmp_path, league):
    rows = json.loads((ROOT / "data/fixtures/t63/source-latency.json").read_text())["base_rows"]
    for row in rows:
        row["league"] = league
    engine = database_for(tmp_path, rows)
    try:
        with session_scope(engine) as session:
            actual = read_history(session, league, "fd,jc")
        assert not actual.attrs.get("linked_source_selection")
        expected = pd.DataFrame(rows)
        for key in ("kickoff_utc", "available_utc"):
            expected[key] = pd.to_datetime(expected[key], utc=True)
        expected.attrs["sources"] = ["fd", "jc"]
        current = DixonColes().fit(actual, as_of="2026-11-14T00:00Z")
        baseline = DixonColes().fit(expected, as_of="2026-11-14T00:00Z")
        np.testing.assert_array_equal(current.engine._params, baseline.engine._params)
        target = {
            "home": rows[0]["home"],
            "away": rows[0]["away"],
            "league": league,
            "source": "jc",
            "kickoff_utc": "2026-11-15T00:00Z",
        }
        np.testing.assert_array_equal(
            current.predict_score_matrix(target), baseline.predict_score_matrix(target)
        )
    finally:
        engine.dispose()


def test_european_pool_keeps_legacy_domestic_rows_without_new_identity_proofs(tmp_path):
    history = pd.read_csv(ROOT / "data/fixtures/t21/model-history.csv", comment="#")
    rows = history.to_dict("records")
    engine = database_for(tmp_path, rows)
    try:
        scope = sorted(set(history.league))
        with session_scope(engine) as session:
            actual = read_history(session, "欧战", "apif,fd,fdorg,jc", training_leagues=scope)
        assert len(actual) == len(history)
        assert not actual.attrs.get("linked_source_selection")
        for key in ("kickoff_utc", "available_utc"):
            history[key] = pd.to_datetime(history[key], utc=True)
        history.attrs.update(sources=["apif", "fd", "fdorg", "jc"], training_leagues=scope)
        current = DixonColes(pooled=True).fit(actual, as_of="2024-02-15T00:00Z")
        baseline = DixonColes(pooled=True).fit(history, as_of="2024-02-15T00:00Z")
        np.testing.assert_array_equal(current.engine._params, baseline.engine._params)
        assert current.training_source_counts == baseline.training_source_counts
    finally:
        engine.dispose()
