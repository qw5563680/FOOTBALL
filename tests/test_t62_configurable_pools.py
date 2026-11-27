import json

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from test_pooled_pipeline import MODEL_CUTOFF
from test_pooled_pipeline import contract as contract
from test_pooled_pipeline import database as database
from test_pooled_pipeline import history as history
from zucai.db import get_engine
from zucai.models import Match
from zucai.predict.command import train
from zucai.predict.dixon_coles import DixonColes, InsufficientHistory
from zucai.predict.parameters import read_parameters
from zucai.report.batch import _models, predict_batch
from zucai.report.render import load_batch, write_report


def _entry(target, training_leagues):
    return {
        "half_life_days": 365,
        "selection_as_of_utc": "2023-07-01T00:00Z",
        "sources": ["jc", "fd", "apif", "fdorg"],
        "pooled": True,
        "p2_ready": False,
        "leagues": [target],
        "training_leagues": training_leagues,
    }


def _config(tmp_path, entries):
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"schema_version": 1, "leagues": entries}), encoding="utf-8")
    return path


def test_rejects_target_missing_from_training_scope(tmp_path):
    path = _config(tmp_path, {"样本池": _entry("样本杯", ["样本联赛"])})
    with pytest.raises(ValueError, match="训练范围须包含目标赛事"):
        read_parameters(path)


def test_rejects_conflicting_target_routes_instead_of_overwriting(tmp_path):
    entry = _entry("样本杯", ["样本杯", "样本联赛"])
    path = _config(tmp_path, {"样本池甲": entry, "样本池乙": entry})
    with pytest.raises(ValueError, match="样本杯.*样本池甲.*样本池乙"):
        read_parameters(path)


def test_disconnected_pool_cannot_fit_until_a_real_bridge_is_known(history):
    games = history.loc[history.league.isin(("英超", "德甲"))].copy()
    games = games.loc[games.available_utc < MODEL_CUTOFF].copy()
    for name in ("home", "away"):
        games[name] = games.league + name
    games.attrs.update(
        pool_name="样本池",
        prediction_leagues=["英超"],
        training_leagues=["英超", "德甲"],
    )
    bridge = games.iloc[[0]].copy()
    bridge["home"] = games.loc[games.league.eq("英超"), "home"].iloc[0]
    bridge["away"] = games.loc[games.league.eq("德甲"), "away"].iloc[0]
    bridge["match_id"] = "jc:synthetic-bridge"
    bridge["source"] = "jc"
    bridge["kickoff_utc"] = MODEL_CUTOFF - pd.Timedelta(hours=3)
    bridge["available_utc"] = MODEL_CUTOFF
    games = pd.concat([games, bridge])
    with pytest.raises(InsufficientHistory, match="样本池.*不连通"):
        DixonColes(pooled=True).fit(games, as_of=MODEL_CUTOFF)
    model = DixonColes(pooled=True).fit(games, as_of=MODEL_CUTOFF + pd.Timedelta(seconds=1))
    assert model.league == "样本池"
    assert model.training_rows == len(games)
    target = dict(
        home=bridge.home.iloc[0],
        away=bridge.away.iloc[0],
        source="jc",
        league="英超",
        kickoff_utc=MODEL_CUTOFF + pd.Timedelta(days=1),
    )
    assert model.predict_score_matrix(target).sum() == pytest.approx(1)
    with pytest.raises(ValueError, match="不在.*目标赛事"):
        model.predict_score_matrix(dict(target, league="德甲"))
    with pytest.raises(InsufficientHistory, match="缺少球队已核实的对阵连接"):
        model.predict_score_matrix(dict(target, away="无训练连接球队"))


def test_multiple_pool_artifacts_route_by_target_and_freeze(database, contract, tmp_path):
    pool_targets = {"样本池甲": "欧冠", "样本池乙": "欧罗巴"}
    scope = ["欧冠", "欧罗巴", "欧协联", "英超", "德甲"]
    entries = {pool: _entry(target, scope) for pool, target in pool_targets.items()}
    params = _config(tmp_path, entries)
    artifacts = []
    for pool, target in pool_targets.items():
        path = tmp_path / f"{pool}.pkl"
        artifacts.append(path)
        result = train(
            db=database,
            league=pool,
            as_of=MODEL_CUTOFF,
            since="2024-02-01T00:00Z",
            output=path,
            params_file=params,
        )
        assert result["league"] == pool and result["prediction_leagues"] == [target]
        assert result["backtest"]["target_leagues"] == [target]
    registry = _models(artifacts, MODEL_CUTOFF, params)
    assert {(league, model.league) for (_, league), (model, _, _) in registry.items()} == {
        (target, pool) for pool, target in pool_targets.items()
    }

    engine = get_engine(database)
    try:
        with Session(engine) as session:
            keys = [
                next(row["match_id"] for row in contract["upcoming"] if row["league"] == name)
                for name in pool_targets.values()
            ]
            assert all(session.get(Match, key).source == "jc" for key in keys)
            batch = predict_batch(
                session,
                model_files=artifacts,
                match_ids=keys,
                as_of=MODEL_CUTOFF,
                generated=MODEL_CUTOFF,
                params_file=params,
                persist=False,
            )
    finally:
        engine.dispose()
    assert batch["bet_allowed"] is False
    assert all(row["status"] == "predicted" for row in batch["predictions"])
    assert all(not row["p2_ready"] for row in batch["models"])
    assert {
        row["model_definition"]["league"] for row in batch["predictions"]
    } == pool_targets.keys()
    assert load_batch(write_report(batch, tmp_path / "frozen")) == batch
