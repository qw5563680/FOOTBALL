"""Complete generate entrypoint regressions; synthetic archives only."""

import csv
import hashlib
import json
from pathlib import Path

import pytest

from test_daily_digest import digest_archive as digest_archive
from test_daily_digest import snapshot
from test_digest_horizon import catalog
from zucai.daily_digest import generate
from zucai.digest_context import catalog_gaps, prediction_window
from zucai.scoreboard.score import instant

CASES = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t46/review-boundaries.json").read_bytes()
)


def install(archive, rows, skipped=(), stored=()):
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    shards = {}
    for name, items in [("jc_catalog", rows), ("jc_skipped", skipped)]:
        for row in items:
            month = instant(row["captured_utc"]).strftime("%Y-%m")
            shards.setdefault(f"state/audit/{name}/{month}.csv", []).append(row)
    for name, values in shards.items():
        path = archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(values[0]))
            writer.writeheader()
            writer.writerows(values)
        manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if stored:
        path = archive / "state/matches.jsonl"
        values = [
            dict(
                match_id=r["match_id"],
                source="jc",
                league=r["league"],
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=r["kickoff_utc"],
                status="scheduled",
                jc_num=r["jc_num"],
            )
            for r in stored
        ]
        with path.open("a") as stream:
            for row in values:
                stream.write(
                    json.dumps([row.get(c) for c in manifest["columns"]["matches"]]) + "\n"
                )
        manifest["counts"]["matches"] += len(values)
        manifest["files"]["state/matches.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("case", CASES)
def test_generate_cross_boundary_catalog_survives_old_business_day_and_capture_month(
    digest_archive, tmp_path, case
):
    row = {
        **catalog()[0],
        "business_date": case["day"],
        "jc_num": "周日030",
        "captured_utc": case["captured"],
        "kickoff_utc": case["kickoff"],
    }
    old = {
        **row,
        "jc_num": "无关历史999",
        "match_id": "jc:old",
        "kickoff_utc": "2026-01-01T01:00:00Z",
    }
    install(digest_archive, [row, old])
    before = snapshot(digest_archive)
    text = generate(digest_archive, tmp_path / "out", now=case["now"]).read_text()
    assert "周日030" in text and "确定漏了；原因未查明" in text
    assert "无当期完整清单" not in text and "无关历史999" not in text
    assert "漏预测合计 1 场" in text
    assert snapshot(digest_archive) == before


@pytest.mark.parametrize("reason", [None, "sell_status_not_1", "unknown", "stale"])
def test_generate_stored_match_needs_contemporaneous_reason_evidence(
    digest_archive, tmp_path, reason
):
    row = catalog()[-3]
    skipped = (
        []
        if reason is None
        else [{**row, "reason": "sell_status_not_1" if reason == "stale" else reason}]
    )
    if reason == "stale":
        skipped[0]["captured_utc"] = "2026-11-05T03:30:00Z"
    # All four classes in one complete report; the second genuine gap is not stored.
    rows = catalog()
    install(digest_archive, rows, skipped, stored=[row])
    text = generate(digest_archive, tmp_path / "out", now="2026-11-06T03:47:00Z").read_text()
    known = reason == "sell_status_not_1"
    assert f"确定漏了（原因已知）{int(known)} 场；原因未查明 {2 - int(known)} 场" in text
    assert "尚未进入预测窗口 7 场" in text
    assert "开球时间缺失，无法判断窗口 1 场" in text
    assert "漏预测合计 2 场" in text
    detail = next(line for line in text.splitlines() if line.startswith("- 缺 周六030"))
    if known:
        assert "同步时跳过" in detail and "整批缺失原因待查" not in detail
    else:
        assert "入库但无赛前预测" in detail and "原因未查明" in detail
        assert "整批缺失原因待查" in detail
    gap = catalog_gaps(
        [row],
        skipped,
        set(),
        {},
        known_ids={row["match_id"]},
        window=prediction_window([], instant("2026-11-06T03:47:00Z")),
    )[0]
    assert gap["confirmed"] is True and gap["reason_known"] is known


@pytest.mark.parametrize("kickoff", ["", "invalid"])
def test_generate_unknown_time_survives_cross_week_but_not_unrelated_old_capture(
    digest_archive, tmp_path, kickoff
):
    row = {
        **catalog()[0],
        "business_date": "2026-11-08",
        "jc_num": "周日030",
        "captured_utc": "2026-11-08T12:00:00Z",
        "kickoff_utc": kickoff,
    }
    old = {**row, "jc_num": "过期未知999", "captured_utc": "2026-10-01T12:00:00Z"}
    install(digest_archive, [row, old])
    text = generate(digest_archive, tmp_path / "out", now="2026-11-09T03:47:00Z").read_text()
    assert "周日030" in text and "开球时间缺失，无法判断窗口 1 场" in text
    assert "过期未知999" not in text and "漏预测合计 0 场" in text
