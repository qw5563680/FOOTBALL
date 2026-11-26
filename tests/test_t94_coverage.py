"""Real forecast entry-point checks for the coverage archive across successive runs."""

import json
from datetime import datetime

from forecast_publication_support import confirm_fixture
from test_cloud_predict import NOW, ROOT
from test_cloud_predict import scheduled_archive as scheduled_archive
from test_digest_review_regressions import install
from zucai import cloud_predict
from zucai.forecast_archive import verify_forecasts
from zucai.forecast_coverage import latest_coverage, read_coverage
from zucai.forecast_publication import publication_times
from zucai.scoreboard.score import load_forecasts


def _catalog(*matches):
    upcoming = {
        row["match_id"]: row
        for row in json.loads((ROOT / "data/fixtures/t17/upcoming.json").read_bytes())
    }
    return [
        {
            **{k: upcoming[key][k] for k in ("match_id", "home", "away", "league", "kickoff_utc")},
            "jc_num": f"周二{number:03d}",
            "captured_utc": NOW.isoformat(),
            "business_date": "2026-11-03",
        }
        for number, key in enumerate(matches, 1)
    ]


def test_every_formal_row_leaves_an_empty_gap_list(scheduled_archive, monkeypatch, tmp_path):
    archive, params = scheduled_archive
    install(archive, _catalog("jc:t17-today"))
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    run = cloud_predict.run(archive, params, "no-gaps")
    coverage = read_coverage(archive, run)
    assert coverage["gaps"] == []
    row = coverage["jc"]["2026-11-03"][0]
    assert row["formal"] and row["baseline"] is None
    assert "竞彩今日：正式预测 1/1，基准 0/1" in summary.read_text()
    assert run["coverage_path"] in verify_forecasts(archive)["files"]


def test_later_run_keeps_only_provable_earlier_baseline(scheduled_archive, monkeypatch):
    archive, params = scheduled_archive
    install(archive, _catalog("jc:t17-missing", "jc:t17-today"))
    config = json.loads(params.read_bytes())
    del config["leagues"]["芬超"]
    params.write_text(json.dumps(config), encoding="utf-8")
    clock = [NOW]
    monkeypatch.setattr(cloud_predict, "_now", lambda: clock[0])
    first = cloud_predict.run(archive, params, "earlier")
    original = (archive / first["coverage_path"]).read_bytes()
    parlay_path = first["forecasts"]["2026-11-03"]["parlay_path"]
    parlay = json.loads((archive / parlay_path).read_bytes())
    assert "historical-1x2-baseline" not in json.dumps(parlay)
    assert any(
        item["match_id"] == "jc:t17-missing"
        and "model_version_not_paper_eligible" in item["reasons"]
        for item in parlay["diagnostics"]
    )
    published = datetime.fromisoformat("2026-11-03T11:59:00+00:00")
    confirm_fixture(archive, published.isoformat())

    clock[0] = datetime.fromisoformat("2026-11-03T12:05:00+00:00")
    second = cloud_predict.run(archive, params, "later")
    new = read_coverage(archive, second)
    assert new["jc"]["2026-11-03"][0]["baseline_source"]["path"] == first["coverage_path"]
    assert new["jc"]["2026-11-03"][0]["baseline"] == json.loads(original)["gaps"][0]["baseline"]
    assert (archive / first["coverage_path"]).read_bytes() == original

    manifest = verify_forecasts(archive)
    view = latest_coverage(archive, manifest, clock[0])
    assert view["coverage_path"] == first["coverage_path"]
    confirm_fixture(archive, clock[0].isoformat(), only_unpublished=True)
    view = latest_coverage(archive, verify_forecasts(archive), clock[0])
    assert view["coverage_path"] == second["coverage_path"]
    assert view["jc"]["2026-11-03"][0]["baseline"] == json.loads(original)["gaps"][0]["baseline"]

    # If the fresh formal version only reaches the branch after kickoff, keep
    # the earlier published forecast for the same fixture.
    after = datetime.fromisoformat("2026-11-03T15:00:00+00:00")
    manifest = verify_forecasts(archive)
    publications = publication_times(archive, manifest)
    latest_formal = second["forecasts"]["2026-11-03"]["path"] + "/forecast.json"
    publications[latest_formal] = {
        **publications[latest_formal],
        "published_utc": after.isoformat(),
    }
    view = latest_coverage(
        archive,
        manifest,
        after,
        publications=publications,
        forecast_rows=load_forecasts(archive, after, publications=publications),
    )
    formal = view["jc"]["2026-11-03"][1]
    assert formal["formal"]
    assert (
        formal["formal_forecast_path"]
        == first["forecasts"]["2026-11-03"]["path"] + "/forecast.json"
    )
