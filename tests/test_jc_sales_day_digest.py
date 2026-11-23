"""Replay the real November 23 catalog and frozen probabilities through generate."""

import hashlib
import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from test_cloud_predict import scheduled_archive as scheduled_archive
from test_daily_digest import snapshot
from test_digest_review_regressions import install
from zucai.cloud_feeds import export_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db
from zucai.forecast_archive import publish_forecasts
from zucai.report.render import write_report

FIXTURE = Path(__file__).parents[1] / "data/fixtures/jc-sales-day/2026-11-23.json"
RUN = "45048943753-1"
DAY = "2026-11-23"
MISSING = "jc:2042949"


def install_forecast(archive, stage, batch):
    # Synthetic artifacts let archive verification run without the production
    # model files. The frozen probabilities, fixture IDs and times are unchanged.
    models = stage / "models" / RUN
    models.mkdir(parents=True)
    database_sha = "b" * 64
    leagues = {}
    for model in batch["models"]:
        league = model["league"]
        artifact = (models / league).with_suffix(".pkl")
        artifact.write_text(league, encoding="utf-8")
        train = artifact.with_suffix(".train.json")
        train.write_text("{}")
        sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        for row in batch["predictions"]:
            if row["model_version"] == model["model_version"]:
                row["model_version"] = sha
        model["model_version"] = sha
        leagues[league] = dict(
            status="trained",
            model_path=artifact.relative_to(stage).as_posix(),
            training_json_path=train.relative_to(stage).as_posix(),
            sha256=sha,
            training_database_sha256=database_sha,
        )
    forecasts = {}
    for day in (DAY, "2026-11-24"):
        directory = f"forecasts/{day}/{RUN}"
        frozen = {
            **batch,
            "batch_id": f"{RUN}-{day}",
            "input": {**batch["input"], "kickoff_date_beijing": day},
            "predictions": [] if day == DAY else batch["predictions"],
        }
        write_report(frozen, stage / directory)
        forecasts[day] = dict(
            path=directory, batch_id=frozen["batch_id"], generated_utc=frozen["generated_utc"]
        )
    publish_forecasts(
        archive,
        stage,
        dict(
            run_id=RUN,
            generated_utc=batch["generated_utc"],
            database_sha256=database_sha,
            leagues=leagues,
            forecasts=forecasts,
        ),
    )


@pytest.mark.parametrize("case", ["8-of-8", "7-of-8", "same-number-other-id", "frozen-exclusion"])
def test_cross_midnight_sales_day_at_top(tmp_path, case):
    sample = json.loads(FIXTURE.read_bytes())
    catalog = sample["catalog"]
    original = sample["batch"]
    batch = deepcopy(original)
    missing = case != "8-of-8"
    target = next(row for row in batch["predictions"] if row["match_id"] == MISSING)
    if case == "7-of-8":
        batch["predictions"] = [r for r in batch["predictions"] if r["match_id"] != MISSING]
    elif case == "same-number-other-id":
        target["match_id"] = "jc:9999999"
    elif case == "frozen-exclusion":
        target.update(status="excluded", reason="缺少同来源同联赛的模型")
        for key in ("model_probabilities", "score_matrix"):
            target.pop(key)
    archive = tmp_path / "archive"
    db = tmp_path / "empty.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    export_feeds(db, archive, date.fromisoformat(DAY), RUN)
    # A later observation of today's printed number must not replace the
    # catalog entry at the report cutoff. Yesterday's number does not count.
    future = dict(catalog[-1], captured_utc="2026-11-23T00:00:00Z", match_id="jc:9999998")
    yesterday = dict(catalog[-1], business_date="2026-11-22", jc_num="周日008")
    install(archive, [*catalog, future, yesterday])
    install_forecast(archive, tmp_path / "stage", batch)
    from forecast_publication_support import confirm_fixture

    confirm_fixture(archive)
    before = snapshot(archive)

    text = generate(archive, tmp_path / "daily", now=sample["now"], run_id="offline").read_text(
        encoding="utf-8"
    )
    head, _ = text.split("## 今日与明日预测（仅发报时未开球）", 1)
    assert "## 竞彩销售日 2026-11-23" in head
    assert f"应有 8 场、已预测 {7 if missing else 8} 场、缺 {int(missing)} 场" in head
    rows = [line for line in head.splitlines() if line.startswith("| 周一")]
    assert [line.split(" | ")[0] for line in rows] == [f"| 周一{i:03d}" for i in range(1, 9)]
    by_id = {r["match_id"]: r for r in original["predictions"]}
    for row, line in zip(catalog, rows, strict=True):
        assert "2026-11-24" in line and f"{row['home']} VS {row['away']}" in line
        if missing and row["match_id"] == MISSING:
            reason = (
                f"冻结批次 {RUN}-2026-11-24：缺少同来源同联赛的模型"
                if case == "frozen-exclusion"
                else "原因未查明"
            )
            assert reason in line and "— | —" in line
        else:
            values = by_id[row["match_id"]]["model_probabilities"]
            assert " / ".join(f"{values[k]:.2%}" for k in "hda") in line
            assert "2026-11-23 02:04:41" in line
    if missing:
        assert "缺失编号：周一008" in head
    assert snapshot(archive) == before


def test_midnight_forecast_then_digest(scheduled_archive, monkeypatch, tmp_path):
    from forecast_publication_support import confirm_fixture
    from test_cloud_predict import ROOT
    from zucai import cloud_predict
    from zucai.report.render import load_batch
    from zucai.scoreboard.score import instant

    historical = json.loads(
        (ROOT / "data/fixtures/jc-sales-day/midnight-2026-11-23.json").read_bytes()
    )
    archive, params = scheduled_archive
    catalog = historical["catalog"]
    ids = {r["match_id"] for r in historical["matches"]}
    assert "2026-11-24" not in historical["forecasts"]
    assert {r["match_id"] for r in catalog} == ids
    # Match source and selling checks have already run. A directory-only ID and
    # a stored fixture outside the spillover must never be promoted.
    unrelated = dict(
        catalog[0], match_id="jc:unrelated", jc_num="周二099", business_date="2026-11-24"
    )
    unsold = dict(catalog[0], match_id="jc:never-stored", jc_num="周一009")
    from zucai.db import session_scope
    from zucai.models import Match

    db = tmp_path / "seed.db"
    engine = get_engine(db)
    with session_scope(engine) as session:
        for row in [*catalog, unrelated]:
            session.add(
                Match(
                    match_id=row["match_id"],
                    league=row["league"],
                    source="jc",
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc=row["kickoff_utc"],
                    status="scheduled",
                    jc_num=row["jc_num"],
                )
            )
    engine.dispose()
    archive = tmp_path / "midnight"
    export_feeds(db, archive, date(2026, 11, 22), "seed-midnight")
    install(archive, [*catalog, unrelated, unsold], historical["skipped"])
    before = instant(historical["forecasts"]["2026-11-23"]["generated_utc"])
    monkeypatch.setattr(cloud_predict, "_now", lambda: before)
    result = cloud_predict.run(archive, params, "midnight-replay")
    batch = load_batch(archive / result["forecasts"]["2026-11-24"]["path"] / "forecast.json")
    assert {r["match_id"] for r in batch["predictions"]} == ids
    confirm_fixture(archive, historical["forecasts"]["2026-11-23"]["published_utc"])
    text = generate(archive, tmp_path / "daily", now=historical["report_generated_utc"]).read_text()
    # Test training has an English model only. Trust exactly its real output.
    good = [r for r in batch["predictions"] if r["status"] == "predicted"]
    assert [r["league"] for r in good] == ["英超"]
    assert "应有 9 场、已预测 1 场、缺 8 场" in text
    assert "midnight-replay-2026-11-24" in text


@pytest.mark.parametrize("kind", ["empty", "after-kickoff", "unpublished"])
def test_sales_day_preserves_exact_published_prediction(kind):
    from zucai.digest_context import frozen_exclusions, frozen_rows
    from zucai.digest_views import jc_sales_day_section
    from zucai.scoreboard.score import instant

    sample = json.loads(FIXTURE.read_bytes())
    batch = sample["batch"]
    target = batch["predictions"][0]
    now = instant(target["kickoff_utc"])
    newer = dict(
        batch,
        batch_id="later",
        generated_utc=now.isoformat(),
        predictions=[] if kind == "empty" else [dict(target, status="excluded", reason="已开球")],
    )
    batches = [(DAY, "first", batch), (DAY, "later", newer)]
    pub = {} if kind == "unpublished" else {"first": {"published_utc": batch["generated_utc"]}}
    latest, _ = frozen_rows(batches, {}, now, publications=pub)
    reasons = frozen_exclusions(batches, now)
    text = "\n".join(
        jc_sales_day_section(
            sample["catalog"],
            [],
            latest,
            reasons,
            date.fromisoformat(DAY),
            window={"missing_batch_dates": []},
        )
    )
    assert "已开球" not in text
    assert ("已预测 0 场" if kind == "unpublished" else "已预测 8 场") in text
