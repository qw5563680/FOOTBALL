"""Run trusted trained models against an explicit, frozen pre-match input set."""

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.models import Match, Odds, Prediction, Team
from zucai.predict.base import model_sources, utc
from zucai.predict.derive import had
from zucai.predict.dixon_coles import DixonColes, InsufficientHistory
from zucai.predict.parameters import model_readiness
from zucai.sources.sporttery import BEIJING


def _models(paths, as_of, params_file=None):
    registry = {}
    for path in paths:
        path = Path(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        model = DixonColes.load(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("模型文件在载入期间发生变化")
        sources = model_sources(model)
        keys = [(source, model.league) for source in sources]
        if any(key in registry for key in keys):
            raise ValueError("同一来源/联赛提供了多个模型，请明确选择一个版本")
        error = None
        if utc(model.as_of) > as_of:
            error = "模型训练截点晚于本次预测截点"
        elif utc(model.latest_available) >= utc(model.as_of):
            error = "模型使用了训练截点后才可知的结果"
        meta = {
            "model_name": "dixon-coles",
            "model_version": digest,
            "artifact_sha256": digest,
            "artifact_name": path.name,
            "sources": sources,
            "training_source_counts": getattr(model, "training_source_counts", None),
            "league": model.league,
            "model_as_of_utc": utc(model.as_of).isoformat(),
            "training_games": int(model.training_rows),
            "last_available_utc": utc(model.latest_available).isoformat(),
            "availability_basis": getattr(model, "availability_basis", "unknown"),
            "assumed_training_share": getattr(model, "assumed_training_share", None),
        }
        meta.update(model_readiness(model, as_of=as_of, params_file=params_file))
        for key in keys:
            registry[key] = (model, meta, error)
    if not registry:
        raise ValueError("请提供至少一个本项目训练的可信模型文件")
    return registry


def _market(session, match, cutoff):
    snapshots = defaultdict(dict)
    rows = session.scalars(
        select(Odds).where(
            Odds.match_id == match.match_id,
            Odds.pool == "had",
            Odds.line == 0,
            Odds.snapshot_utc < match.kickoff_utc,
            Odds.snapshot_utc <= cutoff,
        )
    )
    for row in rows:
        if row.selection in "hda" and len(row.selection) == 1:
            snapshots[row.snapshot_utc][row.selection] = row.price
    for stamp, prices in sorted(snapshots.items(), reverse=True):
        if set(prices) != {"h", "d", "a"}:
            continue
        if not all(math.isfinite(p) and p > 1 for p in prices.values()):
            continue
        inverse = {key: 1 / prices[key] for key in "hda"}
        total = sum(inverse.values())
        return {
            "prices": prices,
            "snapshot_utc": stamp,
            "probabilities": {key: inverse[key] / total for key in "hda"},
            "type": "P3",
            "source": "stored_had_snapshot",
            "execution_verified": False,
        }
    return None


def _save(session, match_id, name, version, probs, grid, batch):
    session.add(
        Prediction(
            match_id=match_id,
            model_name=name,
            model_version=version,
            p_home=probs["h"],
            p_draw=probs["d"],
            p_away=probs["a"],
            score_matrix=grid.tolist(),
            created_utc=batch["generated_utc"],
            batch_id=batch["batch_id"],
            mode=batch["mode"],
            as_of_utc=batch["as_of_utc"],
        )
    )


def predict_batch(
    session: Session,
    *,
    model_files: list[Path],
    as_of: datetime,
    generated: datetime,
    replay: bool = False,
    day: date | None = None,
    match_ids: list[str] | None = None,
    market_weight: float = 0,
    expected_identities: dict | None = None,
    params_file=None,
) -> dict:
    cutoff, created = utc(as_of), utc(generated)
    if cutoff > created:
        raise ValueError("预测截点不能晚于真实生成时间")
    if not replay and cutoff != created:
        raise ValueError("历史截点必须显式启用回放 --replay")
    if (day is None) == (match_ids is None):
        raise ValueError("按开球日期或比赛ID清单选择一种输入")
    if match_ids is not None and (not match_ids or len(set(match_ids)) != len(match_ids)):
        raise ValueError("比赛ID清单不能为空或重复")
    if not math.isfinite(market_weight) or not 0 <= market_weight <= 1:
        raise ValueError("市场实验权重必须在0至1之间")
    registry = _models(model_files, cutoff, params_file)
    if day is not None:
        start = datetime.combine(day, time(), BEIJING)
        keys = list(
            session.scalars(
                select(Match.match_id)
                .where(Match.kickoff_utc >= start, Match.kickoff_utc < start + timedelta(days=1))
                .order_by(Match.kickoff_utc, Match.match_id)
            )
        )
    else:
        keys = list(match_ids)
    if not keys:
        raise ValueError("指定范围没有已核实的比赛")
    batch = {
        "schema_version": 2,
        "batch_id": uuid4().hex,
        "generated_utc": created.isoformat(),
        "as_of_utc": cutoff.isoformat(),
        "mode": "historical_replay" if replay else "prematch",
        "probability_type": "P2",
        "validation_status": "league_gated",
        "bet_allowed": False,
        "input": {
            "match_ids": keys,
            "kickoff_date_beijing": day.isoformat() if day else None,
            "market_weight": market_weight,
            "expected_identities": expected_identities or {},
        },
        "models": list({id(item[0]): item[1] for item in registry.values()}.values()),
        "predictions": [],
    }
    for key in keys:
        item = {"match_id": key, "status": "excluded", "reason": None}
        batch["predictions"].append(item)
        match = session.get(Match, key)
        if match is None:
            item["reason"] = "未找到已核实比赛ID"
            continue
        item.update(
            {
                "source": match.source,
                "league": match.league,
                "jc_num": match.jc_num,
                "home": session.get(Team, match.home_team_id).name,
                "away": session.get(Team, match.away_team_id).name,
                "kickoff_utc": match.kickoff_utc,
                "kickoff_beijing": utc(match.kickoff_utc).tz_convert(BEIJING).isoformat(),
            }
        )
        expected = (expected_identities or {}).get(key, {})
        if any(
            (utc(value) != utc(item[field]) if field == "kickoff_utc" else value != item[field])
            for field, value in expected.items()
        ):
            item["reason"] = "输入清单与已核实的比赛身份、主客或时间不一致"
            continue
        if utc(match.kickoff_utc) <= cutoff:
            item["reason"] = "预测截点时已开赛"
            continue
        if match.status == "cancelled" or (match.status != "scheduled" and not replay):
            item["reason"] = "当前场次状态不适用于赛前预测"
            continue
        scope = registry.get((match.source, match.league))
        if scope is None:
            item["reason"] = "缺少同来源同联赛的模型"
            continue
        model, provenance, error = scope
        if error:
            item["reason"] = error
            continue
        try:
            grid, metadata = model.predict_with_metadata(item)
            probs = had(grid)
        except (InsufficientHistory, ValueError) as exc:
            item["reason"] = str(exc)
            continue
        market = _market(session, match, cutoff)
        if market_weight and market is None:
            raise ValueError(f"{key} 缺少完整赛前SP，不能执行显式市场混合实验")
        item.update(
            {
                "status": "predicted",
                "reason": None,
                "probability_type": "P2",
                "model_name": "dixon-coles",
                "model_version": provenance["model_version"],
                "p2_ready": provenance["p2_ready"],
                "p2_label": provenance["p2_label"],
                "model_probabilities": probs,
                "score_matrix": grid.tolist(),
                "metadata": metadata,
                "top_scores": [
                    {"score": f"{h}:{a}", "probability": float(grid[h, a])}
                    for h, a in sorted(
                        np.ndindex(grid.shape), key=lambda pair: (-grid[pair], pair)
                    )[:5]
                ],
                "market": market,
                "ev": {key: probs[key] * market["prices"][key] - 1 for key in "hda"}
                if market
                else None,
                "experimental_blend": None,
            }
        )
        _save(session, key, "dixon-coles", provenance["model_version"], probs, grid, batch)
        if market_weight and market:
            blend_version = hashlib.sha256(
                json.dumps(
                    {
                        "base_model_version": provenance["model_version"],
                        "market_weight": market_weight,
                        "market": market,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            combined = {
                key: (1 - market_weight) * probs[key] + market_weight * market["probabilities"][key]
                for key in "hda"
            }
            # Preserve the model's conditional score distribution within each outcome.
            home, away = np.indices(grid.shape)
            blended = grid.copy()
            for outcome, mask in (("h", home > away), ("d", home == away), ("a", home < away)):
                if probs[outcome] <= 0:
                    raise ValueError("零概率赛果无法重标比分分布，未写入混合结果")
                blended[mask] *= combined[outcome] / probs[outcome]
            item["experimental_blend"] = {
                "model_name": "dixon-coles+market",
                "model_version": blend_version,
                "market_weight": market_weight,
                "market_dependent": True,
                "probabilities": combined,
                "score_distribution_method": "outcome_reweighted_model_conditionals",
            }
            _save(
                session,
                key,
                "dixon-coles+market",
                blend_version,
                combined,
                blended,
                batch,
            )
    session.flush()
    return batch
