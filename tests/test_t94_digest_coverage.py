"""Offline display of forecast coverage with separate baseline probabilities."""

import json
from datetime import timedelta

from forecast_publication_support import confirm_fixture
from test_cloud_predict import NOW, ROOT
from test_cloud_predict import scheduled_archive as scheduled_archive
from test_daily_digest import snapshot
from test_digest_review_regressions import install
from zucai import cloud_predict
from zucai.daily_digest import generate
from zucai.digest_coverage import sections


def test_header_counts_match_rows_and_baseline_has_provenance_and_gap_reason():
    baseline = {
        "model_name": "historical-1x2-baseline",
        "model_version": "v1",
        "probabilities": {"h": 0.5, "d": 0.2, "a": 0.3},
        "history_scope": "global",
        "history_count": 10,
    }
    fixture = {"home": "甲 | 主队", "away": "乙 客队", "kickoff_utc": "2026-11-26T16:30:00Z"}
    coverage = {
        "coverage_path": "forecasts/2026-11-26/test-run/coverage.json",
        "as_of_utc": "2026-11-26T10:00:00Z",
        "jc": {
            "2026-11-26": [
                dict(fixture, jc_num="周四001", formal=True, baseline=None),
                dict(
                    fixture,
                    jc_num="周四002",
                    formal=False,
                    baseline=baseline,
                    reason="亚冠精英未配置模型",
                    action="所有者决定是否立项",
                ),
            ],
        },
        "sfc": {
            "26171": [
                dict(
                    fixture,
                    seq=1,
                    formal=True,
                    formal_probabilities={"h": 0.6, "d": 0.25, "a": 0.15},
                    baseline=None,
                ),
                dict(
                    fixture,
                    seq=2,
                    formal=False,
                    baseline=baseline,
                    reason="客队只有占位身份",
                    action="Claude 核对球队身份",
                ),
                dict(
                    fixture,
                    seq=3,
                    formal=False,
                    baseline=None,
                    reason="比赛已开球",
                    action=None,
                ),
            ],
        },
    }

    header, lines = sections(coverage, None)

    assert any("正式预测 1/2，基准 1/2" in line for line in header)
    assert any("正式预测 1/3，基准 1/3" in line for line in header)
    assert any("未生成 1/3" in line for line in header)
    assert any("forecasts/2026-11-26/test-run/coverage.json" in line for line in lines)

    rows = [line for line in lines if "甲 \\| 主队" in line]
    assert len(rows) == 5
    assert sum("正式预测" in line for line in rows) == 2
    assert sum("基准（无球队信息）" in line for line in rows) == 2
    assert sum("未生成" in line for line in rows) == 1
    assert sum("50.00% / 20.00% / 30.00%" in line for line in rows) == 2
    assert any("第 1 场" in line and "60.00% / 25.00% / 15.00% | —" in line for line in rows)
    assert any("周四002" in line and "亚冠精英未配置模型" in line for line in rows)
    assert any("第 2 场" in line and "客队只有占位身份" in line for line in rows)
    assert any("比赛已开球" in line for line in rows)


def test_published_coverage_appears_in_digest_and_frozen_files_stay_intact(
    scheduled_archive, tmp_path, monkeypatch
):
    archive, params = scheduled_archive
    fixtures = {
        row["match_id"]: row
        for row in json.loads((ROOT / "data/fixtures/t17/upcoming.json").read_bytes())
    }
    catalog = [
        {
            **{
                key: fixtures[match][key]
                for key in ("match_id", "home", "away", "league", "kickoff_utc")
            },
            "jc_num": number,
            "captured_utc": NOW.isoformat(),
            "business_date": "2026-11-03",
        }
        for match, number in (("jc:t17-today", "周二001"), ("jc:t17-missing", "周二002"))
    ]
    install(archive, catalog)
    config = json.loads(params.read_bytes())
    config["leagues"].pop("芬超")
    params.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    cloud_predict.run(archive, params, "coverage-fixture")

    output = tmp_path / "daily"
    render_time = NOW + timedelta(minutes=1)
    before = generate(archive, output, now=render_time, run_id="before-publish").read_text()
    assert "覆盖情况｜" not in before
    frozen = snapshot(output / "2026-11-03")

    confirm_fixture(archive, (NOW + timedelta(minutes=2)).isoformat())
    inputs = snapshot(archive)
    text = generate(
        archive, output, now=NOW + timedelta(minutes=3), run_id="after-publish"
    ).read_text()

    preamble, detail = text.split("## 预测覆盖明细", 1)
    assert "竞彩今日：正式预测 1/2，基准 1/2" in preamble
    coverage, rest = detail.split("\n## ", 1)
    assert sum("正式预测" in row for row in coverage.splitlines() if "周二001" in row) == 1
    baseline_lines = [row for row in coverage.splitlines() if "周二002" in row]
    assert len(baseline_lines) == 1
    assert (
        "基准（无球队信息）" in baseline_lines[0] and "该联赛没有配置正式模型" in baseline_lines[0]
    )
    assert "% / " in baseline_lines[0]
    assert "基准（无球队信息）" not in rest
    assert snapshot(archive) == inputs
    assert all((output / "2026-11-03" / name).read_bytes() == data for name, data in frozen.items())

    replacement = dict(
        catalog[1],
        captured_utc=(NOW + timedelta(minutes=4)).isoformat(),
        match_id="jc:replacement",
        home="替换主队",
        away="替换客队",
    )
    install(archive, [*catalog, replacement])
    updated = generate(
        archive, output, now=NOW + timedelta(minutes=5), run_id="replacement"
    ).read_text()
    detail = updated.split("## 预测覆盖明细", 1)[1].split("\n## ", 1)[0]
    line = next(line for line in detail.splitlines() if "周二002" in line)
    assert "替换主队" in line and "替换客队" in line
    assert "已核实概率" in line and "基准（无球队信息）" not in line
