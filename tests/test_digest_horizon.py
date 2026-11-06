"""Kickoff dates, planned horizon and successful freezes remain distinct."""

import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

from test_daily_digest import digest_archive as digest_archive
from test_daily_digest import snapshot
from zucai.daily_digest import generate
from zucai.digest_context import catalog_gaps, prediction_window
from zucai.digest_views import review_sections
from zucai.scoreboard.score import instant

FIX = Path(__file__).parents[1] / "data/fixtures/t46/catalog.json"


def catalog():
    return json.loads(FIX.read_bytes())


def test_november_eighth_is_outside_november_sixth_report_but_real_gaps_survive():
    rows = catalog()
    now = instant("2026-11-06T03:47:00Z")
    window = prediction_window([], now)
    gaps = catalog_gaps(rows, [], set(), {}, window=window, known_ids={rows[-3]["match_id"]})
    assert Counter(r["category"] for r in gaps) == dict(
        outside_window=7, missing=1, unexplained=1, unknown_window=1
    )
    assert all(r["kickoff_date_beijing"] == "2026-11-08" for r in gaps[:7])
    assert all(r["expected_window_date"] == "2026-11-07" for r in gaps[:7])
    assert all(r["confirmed"] is False for r in gaps[:7])
    matches = {rows[-3]["match_id"]: rows[-3]}
    text = "\n".join(review_sections({}, {}, matches, [], None, rows, [], now, window=window))
    assert "确定漏了（原因已知）1 场；原因未查明 1 场；尚未进入预测窗口 7 场" in text
    assert "漏预测合计 2 场" in text
    assert "开球时间缺失，无法判断窗口 1 场" in text
    assert "下一班预测" not in text
    assert "预计自 2026-11-07" in text
    for line in text.splitlines():
        if any(f"周六{n:03d}" in line for n in range(22, 29)):
            assert "尚未进入预测窗口" in line and "确定漏了" not in line


@pytest.mark.parametrize(
    "now,kickoff,day,enter",
    [
        ("2026-11-06T15:59:59Z", "2026-11-07T16:00:00Z", "2026-11-08", "2026-11-07"),
        ("2026-11-06T16:00:00Z", "2026-11-08T16:00:00Z", "2026-11-09", "2026-11-08"),
        ("2026-11-29T23:00:00Z", "2026-12-01T16:00:00Z", "2026-12-02", "2026-12-01"),
        ("2026-12-30T23:00:00Z", "2027-01-01T16:00:00Z", "2027-01-02", "2027-01-01"),
    ],
)
def test_beijing_midnight_month_and_year_boundaries(now, kickoff, day, enter):
    row = {**catalog()[0], "kickoff_utc": kickoff, "business_date": "1999-01-01"}
    gap = catalog_gaps([row], [], set(), {}, window=prediction_window([], instant(now)))[0]
    assert (gap["category"], gap["kickoff_date_beijing"], gap["expected_window_date"]) == (
        "outside_window",
        day,
        enter,
    )
    # One second before the boundary is still inside the planned two dates.
    row["kickoff_utc"] = (instant(kickoff) - timedelta(seconds=1)).isoformat()
    assert (
        catalog_gaps([row], [], set(), {}, window=prediction_window([], instant(now)))[0][
            "category"
        ]
        == "unexplained"
    )


@pytest.mark.parametrize("successful", [[], ["2026-11-06"], ["2026-11-07"]])
def test_missing_whole_batch_does_not_shrink_planned_window(successful):
    now = instant("2026-11-06T03:47:00Z")
    batches = [
        (
            "wrong-folder-date",
            "fixture",
            {
                "mode": "prematch",
                "generated_utc": "2026-11-06T02:00:00Z",
                "input": {"kickoff_date_beijing": day},
            },
        )
        for day in successful
    ]
    window = prediction_window(batches, now)
    assert window["planned_dates"] == ["2026-11-06", "2026-11-07"]
    assert window["successful_dates"] == successful
    gaps = catalog_gaps(catalog()[-3:-1], [], set(), {}, window=window)
    assert all(r["category"] == "unexplained" for r in gaps)
    for r in gaps:
        assert r["batch_missing"] == (r["kickoff_date_beijing"] not in successful)


@pytest.mark.parametrize("kickoff", [None, "", "invalid"])
def test_missing_or_invalid_time_cannot_guess_from_number_day(kickoff):
    row = {**catalog()[0], "kickoff_utc": kickoff}
    gap = catalog_gaps(
        [row], [], set(), {}, window=prediction_window([], instant("2026-11-06T03:47:00Z"))
    )[0]
    assert gap["category"] == "unknown_window" and not gap["confirmed"]
    assert gap["kickoff_date_beijing"] is None


def test_duplicate_catalog_updates_and_digest_replay_are_stable():
    rows = catalog()
    rows += [{**rows[0], "captured_utc": "2026-11-06T03:40:00Z"}]
    now = instant("2026-11-06T03:47:00Z")
    first = review_sections({}, {}, {}, [], None, rows, [], now)
    assert first == review_sections({}, {}, {}, [], None, rows, [], now)
    gaps = catalog_gaps(rows, [], set(), {}, window=prediction_window([], now))
    assert len(gaps) == 10
    assert sum(Counter(g["category"] for g in gaps).values()) == 10


def test_past_kickoff_missing_prediction_is_still_missing_and_future_batch_is_not_success():
    now = instant("2026-11-06T03:47:00Z")
    future = {
        "mode": "prematch",
        "generated_utc": "2026-11-06T04:00:00Z",
        "input": {"kickoff_date_beijing": "2026-11-07"},
    }
    window = prediction_window([("2026-11-07", "fixture", future)], now)
    assert window["successful_dates"] == []
    row = {**catalog()[0], "kickoff_utc": "2026-11-05T12:00:00Z"}
    gaps = catalog_gaps([row], [], set(), {}, window=window, known_ids={row["match_id"]})
    assert gaps[0]["category"] == "missing" and gaps[0]["confirmed"]


def test_zero_counts_and_missing_kickoff_are_visible_in_render():
    now = instant("2026-11-06T03:47:00Z")
    rows = catalog()[-1:]
    matches = {rows[0]["match_id"]: rows[0]}
    text = "\n".join(review_sections({}, {}, matches, [], None, rows, [], now))
    assert "开球时间缺失，无法判断窗口 1 场" in text
    text = "\n".join(
        review_sections(
            {},
            {},
            {},
            [],
            None,
            catalog(),
            [],
            now,
            displayed_ids={r["match_id"] for r in catalog()},
        )
    )
    assert "漏预测合计 0 场" in text


def test_full_digest_repeat_does_not_change_inputs_or_counts(digest_archive, tmp_path):
    import csv
    import hashlib

    archive = digest_archive
    relative = "state/audit/jc_catalog/2026-11.csv"
    target = archive / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = catalog()
    with target.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    before = snapshot(archive)
    out = tmp_path / "t46-daily"
    kwargs = dict(now="2026-11-06T03:47:00Z", run_id="offline-t46")
    path = generate(archive, out, **kwargs)
    first = snapshot(out)
    assert "尚未进入预测窗口 7 场" in path.read_text()
    assert "计划日期尚无成功批次：2026-11-06、2026-11-07" in path.read_text()
    generate(archive, out, **kwargs)
    assert snapshot(out) == first
    assert snapshot(archive) == before
