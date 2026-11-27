"""T98 explains backup fixtures using observed and frozen evidence only."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from zucai.digest_views import forecast_sections, header_lines, jc_sales_day_section

NOW = datetime(2026, 11, 3, 10, tzinfo=UTC)
RUN = "765432100-1"
DAY = date(2026, 11, 3)
BACKUP_LABEL = "赛程来自 500 网（备用）"
BACKUP_REASON = "500 网备用赛程没有官方报价"


def _feed(official_status="failed", **backup):
    return {
        "collection": {
            "jc": {"run_id": RUN, "schedule_status": official_status},
            "jc_backup": {
                "run_id": RUN,
                "observed_utc": NOW.isoformat(),
                "official_schedule_status": official_status,
                "status": "used",
                "inserted": 2,
                "active": 2,
                "gaps": [{"match_id": "jc:233", "reason": "客队未核实"}],
                "reconciled": [],
                **backup,
            },
        }
    }


def _header(feed, *, at=NOW, run=RUN, publish="success", sync="success"):
    return "\n".join(
        header_lines(
            feed,
            {},
            {},
            at,
            run_id=run,
            jc_sync_status=sync,
            jc_sync_run_id=run.rsplit("-", 1)[0],
            jc_publish_status=publish,
        )
    )


def test_header_distinguishes_official_failure_from_incomplete_catalog():
    failed = _header(_feed())
    assert "本次竞彩官方赛程采集失败" in failed
    assert "500 网备用名单补入 2 场，仍缺 1 场" in failed
    assert "现有赛程与赔率未确认更新" not in failed

    incomplete = _header(_feed("complete"))
    assert "本次竞彩官方赛程采集成功，名单有缺位" in incomplete
    assert "500 网备用名单补入 2 场，仍缺 1 场" in incomplete
    assert "采集失败" not in incomplete


def test_header_preserves_attempt_and_observation_cutoff():
    later = _feed(observed_utc=(NOW + timedelta(seconds=1)).isoformat())
    assert "500 网备用名单" not in _header(later)
    assert "归档最近一次竞彩官方赛程采集失败" in _header(_feed(), run="765432100-2")
    assert "本次竞彩官方赛程采集失败" not in _header(_feed(), publish="failure")
    assert "本次竞彩官方赛程采集失败" not in _header(_feed(), sync="skipped")


def test_recovery_reports_only_documented_field_changes_in_beijing_time():
    recovery = _feed(
        "complete",
        status="reconciled",
        inserted=0,
        active=0,
        gaps=[],
        reconciled=[
            {
                "match_id": "jc:234",
                "changes": {
                    "home": {"backup": "旧主队", "official": "官方主队"},
                    "venue": {"backup": "未证实", "official": "未证实"},
                    "kickoff_utc": {
                        "backup": "2026-11-03T12:00:00Z",
                        "official": "2026-11-03T12:30:00Z",
                    },
                    "sell_status": {"backup": None, "official": 1},
                },
            },
            {"match_id": "jc:235", "changes": {}},
        ],
    )
    text = _header(recovery)
    assert "2 场已恢复官方赛程，1 场字段有变更" in text
    assert "jc:234" in text
    assert "主队 旧主队 → 官方主队" in text
    assert "开球（北京时间） 2026-11-03 20:00:00 → 2026-11-03 20:30:00" in text
    assert "销售状态 未记录 → 1" in text
    assert "venue" not in text and "jc:235" not in text
    assert "名单有缺位" not in text


def _forecast_and_catalog():
    fixture = Path(__file__).parents[1] / "data/fixtures/t25/forecast.json"
    batch = json.loads(fixture.read_bytes())
    row = batch["predictions"][0]
    row.update(match_id="jc:234", jc_num="周二001")
    row["market"] = None
    row["market_missing_reason"] = BACKUP_REASON
    row["experimental_blend"] = {
        "model_name": "team-market-blend-v1",
        "status": "not_generated",
        "reason": BACKUP_REASON,
    }
    marker = {
        "url": "https://trade.500.com/jczq/",
        "observed_utc": "2026-11-03T09:30:00Z",
        "raw_sha256": "a" * 64,
        "sales_date": DAY.isoformat(),
        **{field: row[field] for field in ("league", "home", "away", "kickoff_utc", "jc_num")},
    }
    row["jc_backup"] = marker
    forecast = {
        **row,
        "published_utc": batch["generated_utc"],
        "generated_utc": batch["generated_utc"],
    }
    catalog = {
        **row,
        "business_date": DAY.isoformat(),
        "captured_utc": marker["observed_utc"],
    }
    batch["predictions"] = [row]
    return batch, forecast, catalog


def test_sales_day_and_batch_label_backup_and_keep_team_model_probability():
    batch, forecast, catalog = _forecast_and_catalog()
    upcoming = "\n".join(
        forecast_sections(
            [(DAY.isoformat(), "f/forecast.json", batch)], DAY, None, now=NOW, catalog=[catalog]
        )
    )
    table = "\n".join(
        jc_sales_day_section(
            [catalog],
            [],
            {forecast["match_id"]: forecast},
            {},
            DAY,
            window={"missing_batch_dates": []},
        )
    )
    for text in (upcoming, table):
        assert BACKUP_LABEL in text
        assert "48.00% / 28.00% / 24.00%" in text
    assert f"市场参考未生成（{BACKUP_REASON}）" in upcoming
    assert f"| 未生成（{BACKUP_REASON}） |" in upcoming


def test_sales_day_marks_backup_exclusion_and_restored_official_roster():
    _, forecast, catalog = _forecast_and_catalog()
    absent = "\n".join(
        jc_sales_day_section([catalog], [], {}, {}, DAY, window={"missing_batch_dates": []})
    )
    assert "应有 1 场、已预测 0 场、缺 1 场" in absent
    assert BACKUP_LABEL in absent

    catalog.pop("jc_backup")
    catalog["home"] = "官方主队"
    restored = "\n".join(
        jc_sales_day_section(
            [catalog],
            [],
            {forecast["match_id"]: forecast},
            {},
            DAY,
            window={"missing_batch_dates": []},
        )
    )
    assert "官方主队 VS 合成客队" in restored
    assert f"冻结预测所用{BACKUP_LABEL}" in restored


def test_later_official_recovery_does_not_replace_earlier_fallback_roster(tmp_path):
    from zucai.daily_digest import _sales_catalog_with_backup
    from zucai.jc_fixture_backup import STATE_FILE, reconciliation

    _, forecast, catalog = _forecast_and_catalog()
    retained = NOW + timedelta(minutes=30)
    state = tmp_path / STATE_FILE
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active": {},
                "reconciled": [
                    reconciliation(
                        forecast["match_id"], forecast["jc_backup"], retained, {"home": "官方主队"}
                    )
                ],
            }
        )
    )
    feed = _feed(observed_utc=retained.isoformat())
    feed["files"] = {STATE_FILE: "fixture-reconciliation"}
    evidence = [forecast]
    historical, replaced = _sales_catalog_with_backup(tmp_path, feed, None, evidence, NOW)
    assert historical[0]["jc_backup"] == catalog["jc_backup"]
    assert historical[0]["home"] == "合成主队"
    assert not replaced
    official = dict(catalog, home="官方主队", captured_utc=retained.isoformat())
    official.pop("jc_backup")
    assert _sales_catalog_with_backup(tmp_path, feed, [official], evidence, retained) == (
        [official],
        {},
    )
    assert _sales_catalog_with_backup(tmp_path, feed, None, evidence, retained) == (None, {})


def test_frozen_feed_digest_keeps_backup_and_unresolved_slots_without_a_prediction(tmp_path):
    from test_daily_digest import snapshot
    from zucai.cloud_feeds import export_feeds
    from zucai.daily_digest import generate
    from zucai.db import get_engine, init_db, session_scope
    from zucai.jc_fixture_backup import STATE_FILE
    from zucai.models import Match, Team

    _, forecast, _ = _forecast_and_catalog()
    database = tmp_path / "fixtures.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(id=1, name=forecast["home"]), Team(id=2, name=forecast["away"])])
        session.flush()
        session.add(
            Match(
                **{
                    field: forecast[field]
                    for field in ("match_id", "league", "jc_num", "kickoff_utc")
                },
                source="jc",
                home_team_id=1,
                away_team_id=2,
                status="scheduled",
            )
        )
    engine.dispose()
    backup = dict(forecast["jc_backup"], home_team_id=1, away_team_id=2)
    state = tmp_path / STATE_FILE
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {"schema_version": 1, "active": {forecast["match_id"]: backup}, "reconciled": []}
        )
    )
    gap = {
        **backup,
        "match_id": "jc:233",
        "jc_num": "周二002",
        "away": "未映射客队",
        "reason": "客队编号 81、简称未映射客队无已核实对应",
    }
    feed = _feed(inserted=1, active=1, gaps=[gap])
    archive = tmp_path / "archive"
    export_feeds(database, archive, DAY, RUN, collection=feed["collection"])
    before = snapshot(archive)
    report = generate(
        archive,
        tmp_path / "report",
        now=NOW,
        run_id=RUN,
        jc_sync_status="success",
        jc_sync_run_id=RUN.rsplit("-", 1)[0],
        jc_publish_status="success",
    ).read_text()
    assert "500 网备用名单补入 1 场，仍缺 1 场" in report
    assert "应有 2 场、已预测 0 场、缺 2 场" in report
    assert "客队编号 81、简称未映射客队无已核实对应" in report
    assert "赛程来自 500 网（备用）" in report
    assert snapshot(archive) == before


def test_reconciliation_keeps_old_bytes_but_hides_inapplicable_backup_probability(
    tmp_path, monkeypatch
):
    import test_daily_digest
    from test_digest_review_regressions import install
    from zucai.cloud_feeds import export_feeds
    from zucai.daily_digest import generate
    from zucai.db import get_engine, init_db
    from zucai.jc_fixture_backup import STATE_FILE, reconciliation

    batch, forecast, catalog = _forecast_and_catalog()
    # The test batch exercises immutable fixture probabilities, without a fusion model.
    batch["predictions"][0]["experimental_blend"] = None
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "forecast.json").write_text(json.dumps(batch))
    monkeypatch.setattr(test_daily_digest, "FIXTURES", fixtures)
    retained = NOW + timedelta(minutes=30)
    state = tmp_path / STATE_FILE
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active": {},
                "reconciled": [
                    {
                        **reconciliation(
                            forecast["match_id"],
                            forecast["jc_backup"],
                            retained,
                            {"home": "官方主队"},
                        ),
                        "identity_changed": True,
                    }
                ],
            }
        )
    )
    database = tmp_path / "empty.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(database, archive, DAY, RUN)
    install(
        archive,
        [
            {
                **{
                    key: catalog[key]
                    for key in (
                        "match_id",
                        "business_date",
                        "jc_num",
                        "away",
                        "league",
                        "kickoff_utc",
                    )
                },
                "home": "官方主队",
                "captured_utc": retained.isoformat(),
            }
        ],
    )
    test_daily_digest.add_forecasts(archive, tmp_path / "stage", RUN, batch["generated_utc"])
    before = test_daily_digest.snapshot(archive)
    previous = generate(archive, tmp_path / "old", now=NOW).read_text()
    current = generate(archive, tmp_path / "new", now=retained).read_text()
    assert "48.00% / 28.00% / 24.00%" in previous
    assert "48.00% / 28.00% / 24.00%" not in current
    assert "官方主队 VS 合成客队" in current
    assert "官方更正主客队或赛事后暂无一致的冻结预测" in current
    assert test_daily_digest.snapshot(archive) == before
