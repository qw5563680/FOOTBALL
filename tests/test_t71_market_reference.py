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
    assert market is None and reason == "没有采集到胜平负报价（未开售或未采集）"


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
            match_ids=["jc:1", "jc:2", "jc:3"],
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
    assert excluded["market_missing_reason"] == "没有采集到胜平负报价（未开售或未采集）"
    assert "market" not in excluded
    kicked = rows["jc:3"]
    assert kicked["status"] == "excluded"
    assert kicked["reason"] == "预测截点时已开赛"
    assert kicked["market_reference"] is None
    assert kicked["market_missing_reason"] == "预测截点时已开赛"
    assert "market" not in kicked


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
            "market_missing_reason": "没有采集到胜平负报价（未开售或未采集）",
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
    assert "市场参考未生成（没有采集到胜平负报价（未开售或未采集））" in text
    assert "差距最大" in text
    status = market_status_section(rows)
    status_text = "\n".join(status)
    assert "市场信息状态" in status_text
    assert "已真实接通" in status_text
    assert "权限未核实" in status_text
    assert "尚未实现" in status_text
    empty_status = market_status_section([])
    empty_text = "\n".join(empty_status)
    assert "| 竞彩胜平负赔率 | 当日未采集 |" in empty_text
    assert "| 竞彩让球胜平负、比分等玩法赔率 | 当日未采集 |" in empty_text


def _production_forecast_archive(archive, rows_by_run):
    """Real-shape forecast archive: root forecast_manifest.json,
    batches at forecasts/<date>/<run>/forecast.json."""
    (archive / "forecast_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "algorithm": "sha256",
                "runs": {
                    run_id: {
                        "forecasts": {
                            "2026-11-03": {
                                "path": f"forecasts/2026-11-03/{run_id}",
                                "batch_id": f"b-{run_id}",
                                "generated_utc": generated,
                            }
                        }
                    }
                    for run_id, (generated, _rows) in rows_by_run.items()
                },
                "files": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for run_id, (_generated, rows) in rows_by_run.items():
        folder = archive / "forecasts" / "2026-11-03" / run_id
        folder.mkdir(parents=True)
        (folder / "forecast.json").write_text(
            json.dumps({"predictions": rows}, ensure_ascii=False), encoding="utf-8"
        )


def test_market_sample_page_lists_every_batch_and_premerge_snapshot(market_db, tmp_path):
    from zucai.market_sample import page

    archive = tmp_path / "archive"
    archive.mkdir()
    first_stamp = (CUTOFF - timedelta(minutes=10)).isoformat()
    second_stamp = (CUTOFF - timedelta(minutes=5)).isoformat()
    _production_forecast_archive(
        archive,
        {
            "run-new": (
                "2026-11-03T01:00:00+00:00",
                [
                    {
                        "match_id": "jc:first",
                        "status": "predicted",
                        "model_probabilities": {"h": 0.48, "d": 0.28, "a": 0.24},
                        "market": {
                            "probabilities": {"h": 0.5, "d": 0.25, "a": 0.25},
                            "snapshot_utc": first_stamp,
                            "first_capture_utc": (CUTOFF - timedelta(minutes=30)).isoformat(),
                            "snapshot_id": first_stamp,
                            "method_version": "market-reference-v1",
                            "return_rate": 0.9,
                        },
                    }
                ],
            ),
            "run-premerge": (
                "2026-11-03T00:30:00+00:00",
                [
                    {
                        "match_id": "jc:first",
                        "status": "predicted",
                        "model_probabilities": {"h": 0.5, "d": 0.27, "a": 0.23},
                        "market": {
                            "probabilities": {"h": 0.5, "d": 0.25, "a": 0.25},
                            "snapshot_utc": second_stamp,
                            "first_capture_utc": (CUTOFF - timedelta(minutes=30)).isoformat(),
                        },
                    }
                ],
            ),
        },
    )
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
    assert "权限未核实" in text
    assert "冻结批次 run-new" in text and "冻结批次 run-premerge" in text
    # pre-merge batch adopts its snapshot_utc (no snapshot_id)
    assert f"该批采用的胜平负快照：{second_stamp}" in text
    assert f"该批采用的胜平负快照：{first_stamp}" in text


def test_market_sample_command_reads_production_forecast_manifest(tmp_path, monkeypatch):
    """The command entry works on the real archive shape (no feed_manifest.json runs)."""
    from datetime import date

    from zucai.cloud_feeds import export_feeds, main
    from zucai.db import get_engine, init_db, session_scope

    db = tmp_path / "source" / "zucai.db"
    engine = get_engine(db)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {name: Team(name=name) for name in ("A", "B")}
        session.add_all(teams.values())
        session.flush()
        session.add(
            Match(
                match_id="jc:2042784",
                source="jc",
                league="L",
                home_team_id=teams["A"].id,
                away_team_id=teams["B"].id,
                kickoff_utc="2026-11-16T10:00:00+00:00",
                status="scheduled",
                jc_num="周日001",
            )
        )
        session.flush()
        for option, price in {"h": 1.24, "d": 4.85, "a": 7.90}.items():
            session.add(
                Odds(
                    match_id="jc:2042784",
                    pool="had",
                    line=0,
                    selection=option,
                    price=price,
                    snapshot_utc="2026-11-15T15:01:11+00:00",
                )
            )
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(db, archive, date(2026, 11, 16), "seed")
    _production_forecast_archive(
        archive,
        {
            "run-1": (
                "2026-11-16T00:00:00+00:00",
                [
                    {
                        "match_id": "jc:2042784",
                        "status": "predicted",
                        "model_probabilities": {"h": 0.3861, "d": 0.3367, "a": 0.2772},
                        "market": {
                            "probabilities": {"h": 0.7079, "d": 0.181, "a": 0.1111},
                            "snapshot_utc": "2026-11-15T15:01:11+00:00",
                            "first_capture_utc": "2026-11-15T15:01:11+00:00",
                            "snapshot_id": "2026-11-15T15:01:11+00:00",
                            "method_version": "market-reference-v1",
                            "return_rate": 0.8778,
                        },
                    }
                ],
            )
        },
    )
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out.md"
    monkeypatch.chdir(work)
    monkeypatch.setattr(
        "sys.argv",
        [
            "zucai.cloud_feeds",
            "market-sample",
            "--archive",
            str(archive),
            "--match",
            "jc:2042784",
            "--out",
            str(out),
        ],
    )
    main()
    text = out.read_text(encoding="utf-8")
    assert "市场参考出处页：jc:2042784" in text
    assert "冻结批次 run-1" in text
    assert "该批采用的胜平负快照：2026-11-15T15:01:11+00:00" in text
    assert "预测采用" in text
    assert "权限未核实" in text


def test_digest_fusion_only_uses_the_new_frozen_experiment_and_marks_directions():
    from datetime import date

    from zucai.digest_views import FUSION_UNAVAILABLE, forecast_sections

    common = {
        "status": "predicted",
        "source": "jc",
        "kickoff_utc": "2026-11-03T11:00:00+00:00",
        "league": "L",
        "home": "A",
        "away": "B",
        "model_probabilities": {"h": 0.5, "d": 0.3, "a": 0.2},
        "model_name": "dixon-coles",
        "model_version": "pure-v1",
    }
    experiment = {
        "model_name": "team-market-blend-v1",
        "status": "generated",
        "probabilities": {"h": 0.3, "d": 0.6, "a": 0.1},
    }
    market = {"probabilities": experiment["probabilities"]}
    rows = [
        dict(
            common,
            match_id="jc:swap",
            jc_num="周二001",
            market=market,
            experimental_blend=dict(experiment, market_weight=1.0),
        ),
        dict(
            common,
            match_id="jc:unchanged",
            jc_num="周二002",
            market=market,
            experimental_blend=dict(
                experiment, market_weight=0.5, probabilities={"h": 0.6, "d": 0.3, "a": 0.1}
            ),
        ),
        dict(
            common,
            match_id="jc:manual",
            jc_num="周二003",
            market=market,
            experimental_blend=dict(experiment, model_name="dixon-coles+market", market_weight=1.0),
        ),
        dict(
            common,
            match_id="jc:missing",
            jc_num="周二004",
            status="excluded",
            reason="缺少该联赛模型",
            experimental_blend={
                "model_name": "team-market-blend-v1",
                "status": "not_generated",
                "reason": "球队模型未生成",
            },
        ),
        dict(
            common,
            match_id="jc:historic",
            jc_num="周二005",
            market=market,
            experimental_blend=experiment,
        ),
        dict(
            common,
            match_id="jc:conflicting",
            jc_num="周二006",
            market={"probabilities": {"h": 0.300001, "d": 0.599999, "a": 0.1}},
            experimental_blend=dict(experiment, market_weight=1.0),
        ),
    ]
    batch = {
        "batch_id": "t72-digest",
        "generated_utc": "2026-11-03T00:00:00+00:00",
        "predictions": rows,
    }
    lines = forecast_sections(
        [("2026-11-03", "forecasts/b1/forecast.json", batch)], date(2026, 11, 3), None
    )
    render = [line for line in lines if line.startswith("| 周二")]
    weight_note = "当前权重 1.00，等于市场参考"
    assert len(render) == 6
    assert [line for line in lines if weight_note in line] == [render[0]]
    assert f"| 30.00% / 60.00% / 10.00% | 30.00% / 60.00% / 10.00%（{weight_note}）" in render[0]
    assert "50.00% / 30.00% / 20.00%" in render[0]
    assert "30.00% / 60.00% / 10.00%" in render[0] and "主胜 → 平" in render[0]
    assert "首选变化" not in render[1]
    assert f"| 30.00% / 60.00% / 10.00% | {FUSION_UNAVAILABLE} |" in render[2]
    assert "未生成（球队模型未生成）" in render[3]
    assert "融合有结果 4 场，首选方向变化 3 场。" in lines
