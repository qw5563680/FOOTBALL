"""Check the actual daily Markdown emitted from frozen 1X2 records."""

import json
from copy import deepcopy
from datetime import date, datetime, timedelta

from forecast_publication_support import confirm_fixture
from sfc_publication_support import confirm_report
from test_daily_digest import snapshot
from test_digest_review_regressions import install
from test_jc_sales_day_digest import DAY, FIXTURE, install_forecast
from test_sfc_daily import NOW as SFC_NOW
from test_sfc_daily import sfc_archive as sfc_archive
from test_sfc_report import complete_issue as complete_issue
from zucai import sfc_digest
from zucai.cloud_feeds import export_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db
from zucai.digest_context import frozen_rows
from zucai.digest_views import forecast_sections
from zucai.sfc_reports import publish_reports, read_reports
from zucai.sources.sporttery import BEIJING


def _body(text, heading, next_heading):
    return text.split(heading, 1)[1].split(next_heading, 1)[0]


def _row(text, number):
    return next(line for line in text.splitlines() if line.startswith(f"| {number} |"))


def test_football_digest_today_tomorrow_and_sales_day_use_same_frozen_cells(tmp_path):
    sample = json.loads(FIXTURE.read_bytes())
    batch = sample["batch"]
    no_prediction = batch["predictions"][-1]
    no_prediction.update(status="excluded", reason="缺少同期球队模型")
    no_prediction.pop("model_probabilities")
    no_prediction.pop("score_matrix")
    earlier = dict(
        batch["predictions"][0],
        match_id="jc:upset-today",
        jc_num="周一009",
        kickoff_utc="2026-11-23T12:00:00+00:00",
    )
    today = deepcopy(dict(batch, predictions=[earlier]))

    database = tmp_path / "empty.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(database, archive, date.fromisoformat(DAY), "t86-offline")
    install(archive, sample["catalog"])
    install_forecast(archive, tmp_path / "tomorrow-stage", batch, run="t86-tomorrow")
    install_forecast(archive, tmp_path / "today-stage", today, run="t86-today", batch_day=DAY)
    confirm_fixture(archive)
    frozen = snapshot(archive)

    args = dict(now=sample["now"], run_id="t86-offline")
    text = generate(archive, tmp_path / "daily-first", **args).read_text(encoding="utf-8")
    repeated = generate(archive, tmp_path / "daily-second", **args).read_text(encoding="utf-8")
    assert text == repeated
    assert snapshot(archive) == frozen
    assert "建议购买" not in text and "推荐" not in text
    sales = _body(text, "## 竞彩销售日", "## 今日与明日预测")
    current = _body(text, "### 今日 2026-11-23", "### 明日 2026-11-24")
    tomorrow = _body(text, "### 明日 2026-11-24", "## 市场信息状态")
    assert all("| 爆冷 |" in table for table in (sales, current, tomorrow))
    assert "62.23% · 最可能冷在客胜 · 高" in _row(current, "周一009")
    for table in (sales, tomorrow):
        assert "62.23% · 最可能冷在客胜 · 高" in _row(table, "周一001")
        assert "32.61% · 最可能冷在平 · 低" in _row(table, "周一002")
        assert "不评（使用默认实力）" in _row(table, "周一004")
        assert "不评（没有模型概率）" in _row(table, "周一008")


def test_football_digest_sfc_cells_replay_the_verified_frozen_report(sfc_archive, tmp_path):
    archive, _, _, issue = sfc_archive
    assert publish_reports(archive, now=SFC_NOW) == [issue.issue_no]
    confirm_report(archive)
    frozen = snapshot(archive / "sfc")
    at = SFC_NOW + timedelta(minutes=2)
    texts = [
        generate(archive, tmp_path / f"daily-{i}", now=at, run_id="t86-sfc-offline").read_text(
            encoding="utf-8"
        )
        for i in range(2)
    ]
    assert texts[0] == texts[1]
    assert snapshot(archive / "sfc") == frozen
    table = _body(texts[0], "## 14 场胜负彩", "## 复盘")
    assert "| 爆冷 |" in table
    assert table.count(r"50\.00% · 最可能冷在平 · 中") == 14

    state_path = f"state/sfc/{issue.issue_no}.json"
    issue_row = json.loads((archive / state_path).read_bytes())["issue"]
    ((path, report),) = read_reports(archive)
    waiting, placeholder = report["rows"][:2]
    waiting["identity_pending"] = {"home": {"name": waiting["home"], "other": "另一个队名"}}
    placeholder["kickoff_basis"] = "placeholder"
    text = "\n".join(sfc_digest._issue_lines(state_path, issue_row, [(path, report)], [], at, str))
    assert "不评（身份待核）" in _row(text, "1")
    assert "不评（占位开球时间）" in _row(text, "2")
    assert r"50\.00% · 最可能冷在平 · 中" in _row(text, "3")


def test_cross_midnight_lane_uses_the_same_published_probabilities():
    sample = json.loads(FIXTURE.read_bytes())
    fixture = sample["catalog"][0]
    before = datetime.fromisoformat("2026-11-22T15:57:32+00:00")
    generated = (before - timedelta(minutes=1)).isoformat()
    catalog = [dict(fixture, captured_utc=generated)]
    batch = {
        **sample["batch"],
        "batch_id": "spillover",
        "generated_utc": generated,
        "as_of_utc": generated,
        "input": {
            "kickoff_date_beijing": "2026-11-24",
            "scope": "jc_spillover",
            "jc_scope_match_ids": [fixture["match_id"]],
        },
        "predictions": [sample["batch"]["predictions"][0]],
    }
    path = "forecasts/partial"
    batches = [("2026-11-24", path, batch)]
    latest, _ = frozen_rows(batches, {}, before, publications={path: {"published_utc": generated}})
    text = "\n".join(
        forecast_sections(
            batches,
            before.astimezone(BEIJING).date(),
            None,
            now=before,
            catalog=catalog,
            latest=latest,
        )
    )
    lane = text.split("### 竞彩销售日跨午夜 2026-11-24", 1)[1]
    assert "| 爆冷 |" in lane
    assert "62.23% · 最可能冷在客胜 · 高" in _row(lane, "周一001")
