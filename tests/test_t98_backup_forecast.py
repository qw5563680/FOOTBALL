"""T98 forecast and coverage use only observed fallback fixture evidence."""

import json
from datetime import date, timedelta

import pytest

from test_cloud_predict import NOW
from test_cloud_predict import scheduled_archive as scheduled_archive
from test_digest_review_regressions import install
from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, session_scope
from zucai.forecast_coverage import read_coverage
from zucai.models import Match, Odds
from zucai.report.render import load_batch, write_report

BACKUP = "jc:2043201"
FUTURE = "jc:2043202"
MISSING = "jc:2043203"
PROOF = {
    "url": "https://trade.500.com/jczq/",
    "observed_utc": NOW.isoformat(),
    "raw_sha256": "a" * 64,
    "sales_date": "2026-11-04",
    "jc_num": "周三032",
    "league": "英超",
    "home": "A",
    "away": "B",
    "kickoff_utc": "2026-11-04T17:00:00+00:00",
    "home_team_id": 1,
    "away_team_id": 2,
}
REASON = "客队无法核实，500 网球队 1582（未核简称）"
NO_ODDS = "500 网备用赛程没有官方报价"


def test_backup_coverage_overlays_only_observed_rows_and_respects_official_slots():
    from zucai.forecast_coverage import with_backup_catalog

    official_row = {
        "match_id": "jc:official",
        "business_date": PROOF["sales_date"],
        "jc_num": PROOF["jc_num"],
        "captured_utc": NOW.isoformat(),
    }
    late = {**PROOF, "observed_utc": (NOW + timedelta(seconds=1)).isoformat()}
    gaps = [
        {**PROOF, "match_id": MISSING, "jc_num": "周三033", "reason": REASON},
    ]
    rows = with_backup_catalog([official_row], {BACKUP: PROOF, FUTURE: late}, gaps, NOW)
    assert rows[0] == official_row
    assert [row["match_id"] for row in rows] == ["jc:official", MISSING]
    assert rows[1]["captured_utc"] == PROOF["observed_utc"]
    assert rows[1]["jc_backup_gap"] == {"reason": REASON}
    assert with_backup_catalog(None, {}, (), NOW) is None
    restored = {**official_row, "match_id": BACKUP}
    assert with_backup_catalog([restored], {BACKUP: PROOF}, (), NOW) == [restored]


def test_cloud_prediction_includes_observed_spillover_without_any_market_quotes(
    scheduled_archive, tmp_path, monkeypatch
):
    from zucai import cloud_predict

    _, params = scheduled_archive
    observed = {
        BACKUP: PROOF,
        FUTURE: {**PROOF, "observed_utc": (NOW + timedelta(seconds=1)).isoformat()},
    }
    observed[FUTURE].update(
        jc_num="周二034", sales_date="2026-11-03", kickoff_utc="2026-11-03T16:00:00+00:00"
    )
    engine = get_engine(tmp_path / "seed.db")
    with session_scope(engine) as session:
        for match_id, proof in observed.items():
            session.add(
                Match(
                    match_id=match_id,
                    source="jc",
                    league=proof["league"],
                    jc_num=proof["jc_num"],
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc=proof["kickoff_utc"],
                    status="scheduled",
                )
            )
        session.flush()
        # A stored quote, even with a valid cutoff, cannot make a fallback row sellable.
        for side, price in zip("hda", [12.345, 13.579, 15.791], strict=True):
            session.add(
                Odds(
                    match_id=BACKUP,
                    pool="had",
                    line=0,
                    selection=side,
                    price=price,
                    snapshot_utc=NOW,
                )
            )
    engine.dispose()
    from zucai.jc_fixture_backup import STATE_FILE

    state_path = tmp_path / STATE_FILE
    state_path.parent.mkdir(exist_ok=True)
    state_path.write_text(json.dumps({"schema_version": 1, "active": observed, "reconciled": []}))
    archive = tmp_path / "backup-feed"
    feed = export_feeds(
        tmp_path / "seed.db",
        archive,
        date(2026, 11, 3),
        "seed-backup",
        collection={
            "jc": {"schedule_status": "failed"},
            "jc_backup": {
                "gaps": [{**PROOF, "match_id": MISSING, "jc_num": "周三033", "reason": REASON}]
            },
        },
    )
    assert STATE_FILE in feed["files"]
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    run = cloud_predict.run(archive, params, "backup-predict")
    paths = {date: archive / item["path"] for date, item in run["forecasts"].items()}
    assert set(paths) == {"2026-11-03", "2026-11-04", "2026-11-05"}
    batch = load_batch(paths["2026-11-05"] / "forecast.json")
    assert batch["input"]["scope"] == "jc_spillover"
    assert batch["input"]["jc_scope_match_ids"] == [BACKUP]
    (forecast,) = batch["predictions"]
    assert forecast["match_id"] == BACKUP and forecast["jc_backup"] == PROOF
    assert forecast["status"] == "predicted" and forecast["model_probabilities"]
    assert forecast["market_reference"] is None and forecast["market"] is None
    assert forecast["market_missing_reason"] == NO_ODDS
    assert forecast["experimental_blend"]["status"] == "not_generated"
    assert forecast["experimental_blend"]["reason"] == NO_ODDS
    assert "12.345" not in (paths["2026-11-05"] / "forecast.json").read_text()
    candidates_path = archive / run["forecasts"]["2026-11-05"]["source_versions_path"]
    (candidate,) = json.loads(candidates_path.read_bytes())["predictions"]
    assert candidate["jc_backup"] == PROOF and candidate.get("market") is None
    assert FUTURE not in (paths["2026-11-04"] / "forecast.json").read_text()
    coverage = read_coverage(archive, run)
    covered, gap = coverage["jc"]["2026-11-04"]
    assert covered["match_id"] == BACKUP and covered["formal"]
    assert covered["jc_backup"] == PROOF
    assert gap["match_id"] == MISSING and gap["reason"] == REASON
    assert gap in [
        {k: v for k, v in row.items() if k not in {"scope", "group"}} for row in coverage["gaps"]
    ]
    assert not gap["formal"] and gap["baseline"] is None

    backup_path = paths["2026-11-05"] / "forecast.json"
    original = backup_path.read_bytes()
    invalid = json.loads(original)
    invalid["predictions"][0]["market_reference"] = {"prices": {"h": 2, "d": 3, "a": 4}}
    with pytest.raises(ValueError, match="备用赛程.*市场报价"):
        write_report(invalid, tmp_path / "invalid")

    # The official withdrawal is authoritative even though the calibrated model
    # could still predict this scheduled match from the restored database.
    stopped_at = NOW + timedelta(seconds=1)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active": {FUTURE: observed[FUTURE]},
                "reconciled": [
                    {
                        "match_id": BACKUP,
                        "observed_utc": stopped_at.isoformat(),
                        "retained": False,
                    }
                ],
            }
        )
    )
    shared = {
        **{key: PROOF[key] for key in ("jc_num", "league", "home", "away", "kickoff_utc")},
        "match_id": BACKUP,
        "business_date": PROOF["sales_date"],
    }
    export_feeds(
        tmp_path / "seed.db",
        archive,
        date(2026, 11, 3),
        "seed-stopped",
        collection={
            "jc": {"schedule_status": "complete", "schedule_updated_utc": stopped_at.isoformat()}
        },
    )
    install(archive, [{**shared, "captured_utc": stopped_at.isoformat(), "retained": "0"}])
    monkeypatch.setattr(cloud_predict, "_now", lambda: stopped_at)
    stopped = cloud_predict.run(archive, params, "official-stopped", sporttery_status="success")
    assert all(
        row["match_id"] != BACKUP
        for batch in stopped["forecasts"].values()
        for row in load_batch(archive / batch["path"] / "forecast.json")["predictions"]
    )

    # A subsequent official offering activates the normal market reference.
    second = NOW + timedelta(seconds=2)
    export_feeds(
        tmp_path / "seed.db",
        archive,
        date(2026, 11, 3),
        "seed-official",
        collection={
            "jc": {"schedule_status": "complete", "schedule_updated_utc": second.isoformat()}
        },
    )
    install(
        archive,
        [
            {
                **shared,
                "captured_utc": second.isoformat(),
                "retained": "1",
            }
        ],
    )
    monkeypatch.setattr(cloud_predict, "_now", lambda: second)
    official = cloud_predict.run(archive, params, "official-predict", sporttery_status="success")
    official_path = archive / official["forecasts"]["2026-11-05"]["path"] / "forecast.json"
    (restored,) = load_batch(official_path)["predictions"]
    assert restored["match_id"] == BACKUP and "jc_backup" not in restored
    assert restored["market"]["prices"] == {"h": 12.345, "d": 13.579, "a": 15.791}
    assert backup_path.read_bytes() == original
