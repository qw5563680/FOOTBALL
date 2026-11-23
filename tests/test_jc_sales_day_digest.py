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
from zucai.sources.sporttery import BEIJING

FIXTURE = Path(__file__).parents[1] / "data/fixtures/jc-sales-day/2026-11-23.json"
RUN = "45048943753-1"
DAY = "2026-11-23"
MISSING = "jc:2042949"


def install_forecast(archive, stage, batch, *, run=RUN):
    # Synthetic artifacts let archive verification run without the production
    # model files. The frozen probabilities, fixture IDs and times are unchanged.
    models = stage / "models" / run
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
        directory = f"forecasts/{day}/{run}"
        frozen = {
            **batch,
            "batch_id": f"{run}-{day}",
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
            run_id=run,
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
    assert batch["input"]["scope"] == "jc_spillover"
    assert set(batch["input"]["jc_scope_match_ids"]) == ids | {"jc:never-stored"}
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


@pytest.mark.parametrize("after_midnight", [False, True])
def test_spillover_known_count_and_coverage_after_midnight(after_midnight):
    from zucai.digest_context import prediction_window
    from zucai.digest_views import forecast_sections
    from zucai.scoreboard.score import instant

    sample = json.loads(FIXTURE.read_bytes())
    eligible = sample["catalog"][0]
    other = dict(eligible, business_date="2026-11-24", jc_num="周二001", match_id="jc:outside")
    before = instant("2026-11-22T15:57:32Z")
    catalog = [dict(r, captured_utc=before.isoformat()) for r in [eligible, other]]
    day = "2026-11-24"
    empty = {
        **sample["batch"],
        "batch_id": "partial",
        "generated_utc": before.isoformat(),
        "input": {
            "kickoff_date_beijing": day,
            "scope": "jc_spillover",
            "jc_scope_match_ids": [eligible["match_id"]],
        },
        "predictions": [],
    }
    batches = [(day, "forecasts/partial", empty)]
    if not after_midnight:
        text = "\n".join(
            forecast_sections(
                batches, before.astimezone(BEIJING).date(), "o/r", now=before, catalog=catalog
            )
        )
        part = text.split("### 竞彩销售日跨午夜")[1]
        assert "竞彩目录有 1 场未开球比赛" in part and "竞彩目录有 2 场" not in part
        assert day in prediction_window(batches, before, catalog)["successful_dates"]
        return
    after = instant("2026-11-22T16:01:00Z")
    window = prediction_window(batches, after, catalog)
    assert day in window["missing_batch_dates"] and day not in window["successful_dates"]
    part = "\n".join(
        forecast_sections(
            batches, after.astimezone(BEIJING).date(), "o/r", now=after, catalog=catalog
        )
    ).split("### 明日")[1]
    assert "竞彩目录有 1 场未开球比赛" in part and "竞彩目录有 2 场" not in part
    assert "仅有按比赛编号冻结的部分批次" in part
    full = dict(empty, input={"kickoff_date_beijing": day}, generated_utc=after.isoformat())
    assert (
        day
        in prediction_window([*batches, (day, "full", full)], after, catalog)["successful_dates"]
    )


@pytest.mark.parametrize(
    "report_at, upcoming", [("02:02", True), ("02:05", False), ("03:00", False)]
)
def test_sales_day_uses_earlier_published_version_after_kickoff_correction(
    tmp_path, report_at, upcoming
):
    from forecast_publication_support import confirm_fixture
    from zucai.scoreboard.score import instant

    sample = json.loads(FIXTURE.read_bytes())
    early = deepcopy(sample["batch"])
    key = early["predictions"][0]["match_id"]
    catalog = [next(r for r in sample["catalog"] if r["match_id"] == key)]
    first_at, second_at = "2026-11-23T01:00:00Z", "2026-11-23T02:00:00Z"
    advanced = "2026-11-23T02:05:00Z"
    early.update(as_of_utc=first_at, generated_utc=first_at)
    db = tmp_path / "empty.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(db, archive, date.fromisoformat(DAY), "sample")
    install(
        archive,
        [*catalog, dict(catalog[0], kickoff_utc=advanced, captured_utc="2026-11-23T02:01:00Z")],
    )
    install_forecast(archive, tmp_path / "early", early, run="before")
    confirm_fixture(archive, first_at)
    late = deepcopy(sample["batch"])
    late.update(as_of_utc=second_at, generated_utc=second_at)
    install_forecast(archive, tmp_path / "late", late, run="after")
    confirm_fixture(archive, advanced, only_unpublished=True)
    frozen = snapshot(archive)
    text = generate(
        archive, tmp_path / "daily", now=instant(f"2026-11-23T{report_at}:00Z")
    ).read_text()
    assert snapshot(archive) == frozen
    sales = text.split("## 竞彩销售日")[1].split("## 今日与明日")[0]
    assert "应有 1 场、已预测 1 场、缺 0 场" in sales
    line = next(line for line in sales.splitlines() if line.startswith("| 周一"))
    assert "2026-11-23 10:05:00" in line and "2026-11-23 09:00:00" in line
    assert "2026-11-23 10:00:00" not in line
    today, tomorrow = text.split("### 今日 2026-11-23", 1)[1].split("### 明日 2026-11-24", 1)
    tomorrow = tomorrow.split("\n## ", 1)[0]
    assert today.count("| 周一001 |") == int(upcoming)
    assert tomorrow.count("| 周一001 |") == 0
    assert "未开球比赛，但本批未提供对应竞彩预测" not in today
    if upcoming:
        assert "forecasts/2026-11-24/before/forecast.json" in today
        assert (
            "生成 UTC：2026-11-23T01:00:00Z" in today
            or "生成 UTC：2026-11-23T01:00:00+00:00" in today
        )
        assert "2026-11-23 10:05:00" in today


def test_old_manifest_marks_even_empty_spillover_as_partial():
    from zucai.digest_context import jc_digest_batch, prediction_window
    from zucai.scoreboard.score import instant

    after = instant("2026-11-22T16:01:00Z")
    day = "2026-11-24"
    batch = {
        "input": {"kickoff_date_beijing": day},
        "mode": "prematch",
        "generated_utc": "2026-11-22T15:57:32Z",
        "predictions": [],
    }
    read = jc_digest_batch(batch, spillover=True)
    assert day in prediction_window([(day, "old", read)], after)["missing_batch_dates"]
    assert "scope" not in batch["input"]
