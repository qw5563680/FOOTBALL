"""T39 presentation uses frozen fixtures, never a live feed or a scoring engine."""

import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from zucai.daily_digest import _instant, generate
from zucai.digest_context import frozen_rows
from zucai.digest_views import forecast_sections, header_lines, pending_sections, review_sections

FIX = Path(__file__).parents[1] / "data/fixtures/t39/digest.json"
SAMPLE = json.loads(FIX.read_bytes())


def test_pending_classes_use_this_digest_time_and_earliest_leg():
    from zucai.digest_context import pending_tickets

    earlier = pending_tickets(SAMPLE["tickets"], _instant(SAMPLE["earlier_utc"]))
    assert earlier["A"][0]["phase"] == "started"
    assert earlier["A"][1]["phase"] == "upcoming"
    later = pending_tickets(SAMPLE["tickets"], _instant(SAMPLE["later_utc"]))
    assert [r["phase"] for r in later["A"]] == ["started", "started"]
    mixed = deepcopy(SAMPLE["tickets"])
    mixed[1]["legs"].append(mixed[0]["legs"][0])
    assert pending_tickets(mixed, _instant(SAMPLE["earlier_utc"]))["A"][1]["phase"] == "started"


def test_previous_pending_reconciles_only_visible_frozen_settlements():
    from zucai.digest_context import pending_tickets, settled_since

    previous = {"pending": pending_tickets(SAMPLE["tickets"], _instant(SAMPLE["earlier_utc"]))}
    current = deepcopy(SAMPLE["tickets"])
    for row, result in zip(current, SAMPLE["settlements"], strict=True):
        row["settlement"] = result
    assert settled_since(previous, current, _instant(SAMPLE["earlier_utc"]))["A"]["settled"] == 0
    diff = settled_since(previous, current, _instant(SAMPLE["later_utc"]))["A"]
    assert (diff["previous"], diff["upcoming"], diff["started"]) == (2, 1, 1)
    assert (diff["settled"], diff["won"], diff["lost"], diff["profit"]) == (2, 1, 1, -80)


def test_catalog_checks_final_number_and_separates_proof_from_reason():
    from zucai.digest_context import catalog_gaps

    known = {"jc:t39-late": {"match_id": "jc:t39-late", "status": "excluded"}}
    gaps = catalog_gaps(SAMPLE["catalog"], [], {"jc:t39-one"}, known)
    assert [r["jc_num"] for r in gaps] == ["周三003", "周三012"]
    assert gaps[0]["reason"] == "unknown"
    assert gaps[1]["reason"] == "no_prematch"
    assert all(r["confirmed"] is True for r in gaps)
    explained = catalog_gaps(SAMPLE["catalog"], SAMPLE["skipped"], {"jc:t39-one"}, known)
    assert explained[0]["reason"] == "sell_status_not_1"
    assert catalog_gaps(None, [], set(), {}) is None


def _batch():
    return json.loads((FIX.parents[1] / "t25/forecast.json").read_bytes())


def test_latest_valid_prematch_survives_post_kickoff_exclusion_and_review_counts():
    early = _batch()
    early["predictions"] = early["predictions"][:1]
    early["batch_id"] = "earlier"
    late = deepcopy(early)
    late["batch_id"] = "last-valid"
    late["generated_utc"] = late["as_of_utc"] = "2026-11-03T11:00:00Z"
    late["predictions"][0]["model_probabilities"] = {"h": 0.2, "d": 0.3, "a": 0.5}
    after = deepcopy(late)
    after["batch_id"] = "after-start"
    after["generated_utc"] = after["as_of_utc"] = "2026-11-03T12:00:00Z"
    after["predictions"][0].update(
        status="excluded", reason="预测截点时已开赛", model_probabilities=None
    )
    batches = [("2026-11-03", f"{b['batch_id']}/forecast.json", b) for b in (early, late, after)]
    now = _instant("2026-11-03T14:00:00Z")
    # Explicit synthetic publication evidence for this version-selection fixture.
    publications = {name: {"published_utc": b["generated_utc"]} for _, name, b in batches}
    latest, observations = frozen_rows(batches, {}, now, publications=publications)
    row = next(iter(latest.values()))
    assert row["batch_id"] == "last-valid"
    future = "\n".join(forecast_sections(batches, now.date(), None, now=now))
    assert row["home"] not in future and "预测截点时已开赛" not in future
    score = {
        **row,
        "record_id": "active",
        "available_utc": "2026-11-03T13:59:00Z",
        "home_goals": 0,
        "away_goals": 1,
        "actual": "a",
    }
    obsolete = {**score, "record_id": "superseded", "actual": "h"}
    summary = {"audit": {"active_keys": ["active"]}}
    review = "\n".join(
        review_sections(latest, observations, {}, [score, obsolete], summary, None, None, now)
    )
    assert "已结 1 场、猜对 1 场、猜错 0 场、待核对 0 场" in review
    assert "20.00% / 30.00% / 50.00%" in review
    assert "last-valid" in review and "after-start" not in review


@pytest.mark.parametrize(
    "case",
    json.loads((FIX.parent / "review_status.json").read_bytes())["cases"],
    ids=lambda case: case["name"],
)
def test_header_stage_wording_and_separate_success_watermarks(case):
    feed = {
        "collection": {
            "jc": {
                "status": "complete",
                "schedule_updated_utc": SAMPLE["earlier_utc"],
                "results_updated_utc": "2026-11-03T08:00:00Z",
                "run_id": "stage-run",
                **case["jc"],
            }
        }
    }
    row = {"kickoff_utc": SAMPLE["past_kickoff_utc"]}
    rows = {
        "jc:pending": row,
        "jc:settled": row,
        "jc:future": {"kickoff_utc": "2026-11-06T00:00:00Z"},
    }
    matches = {
        "jc:settled": {
            "result": {"available_utc": SAMPLE["earlier_utc"], "home_goals": 0, "away_goals": 0}
        }
    }
    text = "\n".join(
        header_lines(
            feed,
            rows,
            matches,
            _instant(SAMPLE["later_utc"]),
            run_id=case.get("digest_run_id"),
            jc_sync_status=case.get("job_status", "success"),
            jc_sync_run_id=case.get("job_run_id", "failed-1"),
        )
    )
    assert "竞彩接口最近成功检查：2026-11-05 01:39:22" in text
    assert "赛果更新至：2026-11-03 16:00:00" in text
    for expected in case["expected"]:
        assert expected in text
    for absent in case["absent"]:
        assert absent not in text


def test_header_unknown_job_failure_does_not_invent_failed_stage():
    feed = {"collection": {"jc": {"status": "complete"}}}
    text = "\n".join(
        header_lines(
            feed,
            {},
            {},
            _instant(SAMPLE["later_utc"]),
            jc_sync_status="failure",
            jc_sync_run_id="failed-1",
        )
    )
    assert "竞彩同步失败（run failed-1）" in text
    assert "失败阶段未记录" in text
    assert "赛程更新失败" not in text and "赛果更新失败" not in text
    skipped = "\n".join(
        header_lines(
            feed,
            {},
            {},
            _instant(SAMPLE["later_utc"]),
            jc_sync_status="skipped",
        )
    )
    assert "失败" not in skipped


def test_gap_wording_keeps_evidence_separate_and_never_infers_final_number():
    now = _instant(SAMPLE["later_utc"])
    matches = {"jc:t39-late": SAMPLE["catalog"][-1]}
    text = "\n".join(review_sections({}, {}, matches, [], None, SAMPLE["catalog"][1:], [], now))
    assert "缺 周三003" in text and "确定漏了；原因未查明" in text
    assert "缺 周三012" in text and "确定漏了；入库但无赛前预测" in text
    assert text.index("缺 周三003") < text.index("缺 周三012")
    for catalog in (None, []):
        unknown = "\n".join(review_sections({}, {}, matches, [], None, catalog, [], now))
        if catalog is None:
            assert "截点前没有可用清单，无法核对是否漏预测" in unknown
            assert "确定漏了" not in unknown
        else:
            assert "已有归档清单" in unknown
            assert "漏预测合计 0 场" in unknown


def test_future_omitted_from_latest_batch_is_checked_against_rendered_table():
    row = {**SAMPLE["catalog"][0], "batch_id": "old"}
    now = _instant(SAMPLE["earlier_utc"])
    text = "\n".join(
        review_sections(
            {row["match_id"]: row},
            {row["match_id"]: row},
            {},
            [],
            None,
            [row],
            [],
            now,
            displayed_ids=set(),
        )
    )
    assert "缺 周三001" in text and "有旧赛前预测" in text


def test_visible_future_missing_model_is_not_reported_as_missing_number():
    batch = _batch()
    row = batch["predictions"][1]
    row["jc_num"] = "周二012"
    displayed = set()
    text = "\n".join(
        forecast_sections(
            [("2026-11-03", "forecast.json", batch)],
            NOW.date(),
            None,
            now=NOW,
            displayed_ids=displayed,
        )
    )
    assert "周二012" in text and "未预测：缺少该联赛模型" in text
    catalog = [{**SAMPLE["catalog"][-1], **row, "business_date": "2026-11-03"}]
    review = "\n".join(
        review_sections(
            {},
            {},
            {},
            [],
            None,
            catalog,
            [],
            NOW,
            displayed_ids=displayed,
        )
    )
    assert "缺 周二012" not in review


def _install_ledger(archive, ledger):
    path = archive / "scoreboard/ledger.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ledger))
    summary_path = archive / "scoreboard/summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["files"]["ledger.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
    summary_path.write_text(json.dumps(summary, ensure_ascii=False))


def test_frozen_daily_versions_reconcile_and_rerun_is_byte_identical(digest_archive, tmp_path):
    from zucai.digest_context import pending_tickets

    out = tmp_path / "daily"
    _install_ledger(digest_archive, SAMPLE["tickets"])
    earlier, later = (_instant(SAMPLE[key]) for key in ("earlier_utc", "later_utc"))
    before = snapshot(digest_archive)
    first = generate(digest_archive, out, now=earlier, run_id="early").read_text()
    assert "发报时未开球 1 注；发报时已开球、赛果未核对 1 注" in first
    assert "本日首版" in first
    receipt = json.loads((out / "2026-11-05/early.json").read_bytes())
    assert receipt["pending"] == pending_tickets(SAMPLE["tickets"], earlier)
    assert snapshot(digest_archive) == before
    frozen_first = (out / "2026-11-05/early.md").read_bytes()
    ledger = deepcopy(SAMPLE["tickets"])
    for row, result in zip(ledger, SAMPLE["settlements"], strict=True):
        row["settlement"] = result
    _install_ledger(digest_archive, ledger)
    second = generate(digest_archive, out, now=later, run_id="late").read_text()
    assert "本日第 2 次更新" in second
    assert (
        "上一版待结 2 注（1 注未开球、1 注已开球未核对）→ 本版已结 2 注：1 中 1 错，盈亏 -80.00"
        in second
    )
    assert (out / "2026-11-05/early.md").read_bytes() == frozen_first
    frozen_all = snapshot(out)
    generate(digest_archive, out, now=later, run_id="late")
    assert snapshot(out) == frozen_all
    next_day = generate(
        digest_archive, out, now=later + timedelta(days=1), run_id="tomorrow"
    ).read_text()
    assert "本日首版" in next_day and "本日第 1 次更新" in next_day


def test_missing_kickoff_is_not_assumed_unstarted():
    from zucai.digest_context import pending_tickets

    rows = deepcopy(SAMPLE["tickets"][:1])
    rows[0]["legs"][0].pop("kickoff_utc")
    pending = pending_tickets(rows, _instant(SAMPLE["later_utc"]))
    text = "\n".join(pending_sections(pending, rows, None, _instant(SAMPLE["later_utc"])))
    assert "开球时间缺失、无法分类 1 注" in text
    assert "发报时未开球 0 注" in text


def test_account_settled_count_investment_profit_roi_are_shown_together(digest_archive, tmp_path):
    path = digest_archive / "scoreboard/summary.json"
    summary = json.loads(path.read_bytes())
    summary["accounts"]["1.05"]["D"].update(settled=1, settled_stakes=100, returns=0, roi=-1)
    path.write_text(json.dumps(summary))
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    assert "已结 1 注，投入 100.00，盈亏 -100.00，回报率 -100.00%" in text


def test_receipt_corruption_and_unsafe_run_id_do_not_replace_previous(digest_archive, tmp_path):
    out = tmp_path / "daily"
    generate(digest_archive, out, now=NOW, run_id="first")
    before = snapshot(out)
    with pytest.raises(ValueError, match="run ID"):
        generate(digest_archive, out, now=NOW, run_id="../escape")
    assert snapshot(out) == before
    (out / "2026-11-03/first.md").write_text("changed")
    with pytest.raises(ValueError, match="哈希"):
        generate(digest_archive, out, now=NOW, run_id="second")
