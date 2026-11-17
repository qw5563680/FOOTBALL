"""T71 市场参考（三栏显示）的离线测试。

只使用 data/fixtures 离线样本与本地临时数据库；不发任何真实请求。
"""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from test_prediction_batch import CUTOFF
from test_prediction_batch import setup_batch as setup_batch
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Odds, Team
from zucai.report import batch as batch_module
from zucai.report.batch import _market


@pytest.fixture
def market_db(tmp_path):
    """Odds variations for the three missing reasons + a first-capture case."""
    db = tmp_path / "market.db"
    engine = get_engine(db)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in ("A", "B")}
        session.add_all(teams.values())
        session.flush()
        for key, kickoff in (
            ("jc:first", CUTOFF + timedelta(hours=3)),
            ("jc:incomplete", CUTOFF + timedelta(hours=3)),
            ("jc:none", CUTOFF + timedelta(hours=3)),
            ("jc:late", CUTOFF + timedelta(hours=3)),
        ):
            session.add(
                Match(
                    match_id=key,
                    source="jc",
                    league="L",
                    home_team_id=teams["A"].id,
                    away_team_id=teams["B"].id,
                    kickoff_utc=kickoff,
                    status="scheduled",
                    jc_num="周日001",
                )
            )
        session.flush()

        def add(match_id, stamp, quotes):
            for option, price in quotes.items():
                session.add(
                    Odds(
                        match_id=match_id,
                        pool="had",
                        line=0,
                        selection=option,
                        price=price,
                        snapshot_utc=stamp,
                    )
                )

        add("jc:first", CUTOFF - timedelta(minutes=30), {"h": 3.0, "d": 4.0, "a": 5.0})
        add("jc:first", CUTOFF - timedelta(minutes=10), {"h": 2.0, "d": 3.0, "a": 6.0})
        add("jc:incomplete", CUTOFF - timedelta(minutes=10), {"h": 2.0, "d": 3.0})
        add("jc:late", CUTOFF + timedelta(seconds=1), {"h": 5.0, "d": 5.0, "a": 5.0})
    yield engine
    engine.dispose()


def _one(engine, match_id):
    with session_scope(engine) as session:
        return _market(session, session.get(Match, match_id), CUTOFF)


def test_adopts_latest_complete_snapshot_before_cutoff(market_db):
    market, reason = _one(market_db, "jc:first")
    assert reason is None
    assert datetime.fromisoformat(market["snapshot_utc"]) == CUTOFF - timedelta(minutes=10)
    assert market["prices"] == {"h": 2.0, "d": 3.0, "a": 6.0}
    probs = market["probabilities"]
    assert math.isclose(sum(probs.values()), 1.0, rel_tol=1e-12)
    assert math.isclose(probs["h"], 0.5, rel_tol=1e-12)
    assert math.isclose(market["return_rate"], 1 / (1 / 2 + 1 / 3 + 1 / 6), rel_tol=1e-12)
    assert datetime.fromisoformat(market["first_capture_utc"]) == CUTOFF - timedelta(minutes=30)
    assert market["first_capture_utc"] != market["snapshot_utc"]
    assert market["method_version"] == batch_module.MARKET_REFERENCE_VERSION
    assert market["operator"] == "竞彩官方"
    assert market["supplier"] == "中国体育彩票（竞彩官网）"
    assert market["source_quote_time"] == "来源未提供"
    assert market["collected_utc"] == market["snapshot_utc"]
    assert market["snapshot_id"] == market["snapshot_utc"]
    assert market["opening_odds"] == "来源未提供初盘"


def test_incomplete_snapshot_never_generates(market_db):
    market, reason = _one(market_db, "jc:incomplete")
    assert market is None and reason == "报价不完整"


def test_no_odds_at_all(market_db):
    market, reason = _one(market_db, "jc:none")
    assert market is None and reason == "未开售"


def test_complete_but_collected_after_cutoff(market_db):
    market, reason = _one(market_db, "jc:late")
    assert market is None and reason == "采集晚于截点"


def test_predict_batch_enriches_predicted_and_excluded_rows(setup_batch):
    engine, path, model, _ = setup_batch
    from zucai.report.batch import predict_batch

    with session_scope(engine) as session:
        batch = predict_batch(
            session,
            model_files=[path],
            as_of=CUTOFF,
            generated=CUTOFF,
            match_ids=["jc:1", "jc:2"],
        )
    rows = {row["match_id"]: row for row in batch["predictions"]}
    predicted = rows["jc:1"]
    assert predicted["status"] == "predicted"
    assert predicted["market"] is not None
    assert predicted["market"]["method_version"] == "market-reference-v1"
    assert datetime.fromisoformat(predicted["market"]["snapshot_utc"]) == CUTOFF - timedelta(
        minutes=10
    )
    assert predicted["market_missing_reason"] is None
    excluded = rows["jc:2"]
    assert excluded["status"] == "excluded"
    assert excluded["market_reference"] is None
    assert excluded["market_missing_reason"] == "未开售"
    assert "market" not in excluded


def test_duplicate_collections_do_not_double_count(market_db):
    """Same quotes re-collected at a second stamp: one adopted snapshot, counted once."""
    with session_scope(market_db) as session:
        for option, price in {"h": 2.0, "d": 3.0, "a": 6.0}.items():
            session.add(
                Odds(
                    match_id="jc:first",
                    pool="had",
                    line=0,
                    selection=option,
                    price=price,
                    snapshot_utc=CUTOFF - timedelta(minutes=5),
                )
            )
    market, reason = _one(market_db, "jc:first")
    assert reason is None
    assert datetime.fromisoformat(market["snapshot_id"]) == CUTOFF - timedelta(minutes=5)
    assert market["prices"] == {"h": 2.0, "d": 3.0, "a": 6.0}
    assert math.isclose(sum(market["probabilities"].values()), 1.0, rel_tol=1e-12)
    assert datetime.fromisoformat(market["first_capture_utc"]) == CUTOFF - timedelta(minutes=30)


def test_digest_three_columns_and_status_table():
    from zucai.digest_views import FUSION_UNAVAILABLE, forecast_sections, market_status_section

    rows = [
        {
            "match_id": "jc:1",
            "status": "predicted",
            "jc_num": "周日001",
            "kickoff_utc": "2026-11-03T11:00:00+00:00",
            "league": "L",
            "home": "A",
            "away": "B",
            "model_probabilities": {"h": 0.48, "d": 0.28, "a": 0.24},
            "market": {
                "probabilities": {"h": 0.5, "d": 0.25, "a": 0.25},
                "snapshot_utc": "2026-11-03T05:00:00+00:00",
            },
            "market_missing_reason": None,
            "model_name": "DixonColes",
            "model_version": "v1",
            "reference": "基线",
            "model_status": "可用",
            "top_scores": [],
        },
        {
            "match_id": "jc:2",
            "status": "excluded",
            "reason": "缺少该联赛模型",
            "jc_num": "周日002",
            "kickoff_utc": "2026-11-03T12:00:00+00:00",
            "league": "L",
            "home": "A",
            "away": "B",
            "market_reference": None,
            "market_missing_reason": "未开售",
        },
    ]
    batch = {
        "batch_id": "t71-digest",
        "generated_utc": "2026-11-03T00:00:00+00:00",
        "predictions": rows,
    }
    from datetime import date

    lines = forecast_sections(
        [("2026-11-03", "forecasts/b1/forecast.json", batch)],
        date(2026, 11, 3),
        Path("archive"),
        matches={},
    )
    text = "\n".join(lines)
    assert "球队模型（胜 / 平 / 负）" in text
    assert "市场参考（胜 / 平 / 负）" in text
    assert "球队＋市场（实验）" in text
    assert "50.00% / 25.00% / 25.00%" in text
    assert FUSION_UNAVAILABLE in text
    assert "球队模型未生成（缺少该联赛模型）" in text
    assert "市场参考未生成（未开售）" in text
    assert "差距最大" in text
    status = market_status_section()
    status_text = "\n".join(status)
    assert "市场信息状态" in status_text
    assert "已真实接通" in status_text
    assert "权限不足" in status_text
    assert "尚未实现" in status_text


def test_market_sample_page_end_to_end(market_db, tmp_path):
    from zucai.market_sample import page

    archive = tmp_path / "archive"
    archive.mkdir()
    manifest = {
        "runs": {
            "r1": {
                "forecasts": {
                    "b1": {"path": "forecasts/b1"},
                }
            }
        }
    }
    (archive / "forecasts" / "b1").mkdir(parents=True)
    (archive / "forecasts" / "b1" / "forecast.json").write_text(
        json.dumps(
            {
                "predictions": [
                    {
                        "match_id": "jc:first",
                        "status": "predicted",
                        "model_probabilities": {"h": 0.48, "d": 0.28, "a": 0.24},
                        "market": {
                            "probabilities": {"h": 0.5, "d": 0.25, "a": 0.25},
                            "snapshot_utc": (CUTOFF - timedelta(minutes=10)).isoformat(),
                            "first_capture_utc": (CUTOFF - timedelta(minutes=30)).isoformat(),
                            "snapshot_id": (CUTOFF - timedelta(minutes=10)).isoformat(),
                            "method_version": "market-reference-v1",
                            "return_rate": 0.9,
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (archive / "feed_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with session_scope(market_db) as session:
        text = page(session, archive, "jc:first")
    assert "市场参考出处页：jc:first" in text
    assert "中国体育彩票（竞彩官网）" in text
    assert "竞彩官方" in text
    assert "胜平负报价快照" in text
    assert "首次采集" in text
    assert "预测采用" in text
    assert "来源报价时间" in text
    assert "来源未提供" in text
    assert "来源未提供初盘" in text
    assert "必发" in text
    assert "成交价" in text and "成交量" in text and "盘口深度" in text
    assert "权限不足" in text
