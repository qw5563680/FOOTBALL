"""T73: the review reads frozen non-prediction reasons from cropped real archive rows."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from zucai.daily_digest import _instant
from zucai.digest_context import MODEL_SCOPE_REASON, catalog_gaps, frozen_exclusions
from zucai.digest_views import review_sections

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t73/real_2026_11_17.json"
FIXTURE_SHA256 = "03afb20eab06533110f71a6441cf2dc9e6066a2a7d049227e78c914e925c50aa"
REAL = json.loads(FIXTURE.read_bytes())
T39 = json.loads((Path(__file__).parents[1] / "data/fixtures/t39/digest.json").read_bytes())
# Generation time of the published 2026-11-18 digest version 44151042457-1.
NOW = _instant("2026-11-17T19:37:10.402362+00:00")
SINGAPORE, CHINA, JAPAN, CHILE = "jc:2042828", "jc:2042829", "jc:2042830", "jc:2042837"


def _batches(keep=lambda batch: True):
    return [
        (row["day"], row["path"], deepcopy(row["batch"]))
        for row in REAL["batches"]
        if keep(row["batch"])
    ]


def _at(rows, now=NOW):
    return [row for row in rows if _instant(row["captured_utc"]) <= now]


def _gaps(exclusions, *, skipped=None):
    rows = catalog_gaps(
        _at(REAL["catalog"]),
        _at(REAL["skipped"]) if skipped is None else skipped,
        set(),
        {},
        known_ids={SINGAPORE, CHINA, JAPAN, CHILE},
        exclusions=exclusions,
    )
    return {row["match_id"]: row for row in rows}


def test_fixture_is_the_pinned_real_archive_crop():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    assert REAL["synthetic"] is False
    assert {row["match_id"] for row in REAL["catalog"]} == {SINGAPORE, CHINA, JAPAN, CHILE}


def test_real_tuesday_internationals_are_out_of_model_scope_not_unexplained():
    exclusions = frozen_exclusions(_batches(), NOW)
    gaps = _gaps(exclusions)
    for match_id in (SINGAPORE, CHINA, JAPAN):
        assert gaps[match_id]["category"] == "out_of_scope"
        assert gaps[match_id]["confirmed"] is False
        assert gaps[match_id]["frozen_exclusion"]["reason"] == MODEL_SCOPE_REASON
    # Singapore kicked off 10:00Z; the 10:37Z and 11:38Z batches say "already started"
    # and cannot supply its reason. China and Japan take the latest cutoff before kickoff.
    assert gaps[SINGAPORE]["frozen_exclusion"]["batch_id"] == "44007765058-1-2026-11-17"
    assert gaps[CHINA]["frozen_exclusion"]["batch_id"] == "44070879208-1-2026-11-17"
    assert gaps[JAPAN]["frozen_exclusion"]["batch_id"] == "44070879208-1-2026-11-17"

    text = "\n".join(
        review_sections(
            {},
            {},
            {},
            [],
            None,
            [row for row in _at(REAL["catalog"]) if row["match_id"] != CHILE],
            [],
            NOW,
            exclusions=exclusions,
        )
    )
    assert (
        "确定漏了（原因已知）0 场；原因未查明 0 场；模型范围外未预测 3 场（不计入漏预测）" in text
    )
    assert "漏预测合计 0 场，只含前两类。" in text
    assert (
        "- 周二001（2026-11-17，新加坡 VS 巴西）：模型范围外未预测；"
        "冻结批次写明：缺少同来源同联赛的模型"
        "（批次 44007765058-1-2026-11-17，截点 2026-11-17 11:26:30）；不计入漏预测。" in text
    )
    assert "原因未查明（" not in text and "缺 周二" not in text


def test_real_sync_skip_decides_the_class_and_both_reasons_are_listed():
    exclusions = frozen_exclusions(_batches(), NOW)
    row = _gaps(exclusions)[CHILE]
    assert row["category"] == "missing" and row["reason"] == "sell_status_not_1"
    assert row["frozen_exclusion"]["reason"] == MODEL_SCOPE_REASON
    text = "\n".join(
        review_sections(
            {},
            {},
            {},
            [],
            None,
            _at(REAL["catalog"]),
            _at(REAL["skipped"]),
            NOW,
            exclusions=exclusions,
        )
    )
    line = next(item for item in text.splitlines() if item.startswith("- 缺 周二010"))
    assert "确定漏了；同步时跳过：" in line and "冻结批次写明：缺少同来源同联赛的模型" in line
    assert "确定漏了（原因已知）1 场；原因未查明 0 场；模型范围外未预测 3 场" in text
    assert "漏预测合计 1 场" in text


def test_reason_stated_only_after_kickoff_is_not_evidence():
    late = frozen_exclusions(
        _batches(lambda batch: batch["batch_id"] != "44007765058-1-2026-11-17"), NOW
    )
    assert SINGAPORE not in late
    gaps = _gaps(late)
    assert gaps[SINGAPORE]["category"] == "unexplained"
    assert gaps[SINGAPORE]["reason_known"] is False
    assert gaps[CHINA]["category"] == gaps[JAPAN]["category"] == "out_of_scope"


def test_batches_not_yet_generated_or_not_prematch_supply_no_reason():
    early = _instant("2026-11-17T03:20:00+00:00")
    assert frozen_exclusions(_batches(), early) == {}
    replay = _batches()
    for _, _, batch in replay:
        batch["mode"] = "replay"
    assert frozen_exclusions(replay, NOW) == {}
    predicted = _batches()
    for _, _, batch in predicted:
        for row in batch["predictions"]:
            row["status"] = "predicted"
    assert frozen_exclusions(predicted, NOW) == {}


def test_other_frozen_reason_is_a_known_miss_and_still_counted():
    changed = _batches()
    for _, _, batch in changed:
        for row in batch["predictions"]:
            if row["match_id"] == JAPAN:
                row["reason"] = "当前场次状态不适用于赛前预测"
    exclusions = frozen_exclusions(changed, NOW)
    row = _gaps(exclusions, skipped=[])[JAPAN]
    assert row["category"] == "missing" and row["reason_known"] is True
    text = "\n".join(
        review_sections(
            {},
            {},
            {},
            [],
            None,
            [r for r in _at(REAL["catalog"]) if r["match_id"] == JAPAN],
            [],
            NOW,
            exclusions=exclusions,
        )
    )
    assert "确定漏了（原因已知）1 场；原因未查明 0 场；模型范围外未预测 0 场" in text
    assert "确定漏了；冻结批次写明：当前场次状态不适用于赛前预测（批次 " in text
    assert "漏预测合计 1 场" in text


def test_without_any_contemporaneous_record_the_reason_stays_unknown():
    gaps = _gaps({}, skipped=[])
    assert {row["category"] for row in gaps.values()} == {"unexplained"}
    text = "\n".join(
        review_sections({}, {}, {}, [], None, _at(REAL["catalog"]), [], NOW, exclusions={})
    )
    assert "原因未查明 4 场；模型范围外未预测 0 场" in text and "漏预测合计 4 场" in text
    assert text.count("（已查同步跳过记录与截点不晚于开球的冻结批次，均无该场记录）") == 4


def test_t39_missing_numbers_stay_unexplained_with_provenance():
    now = _instant(T39["later_utc"])
    matches = {"jc:t39-late": T39["catalog"][-1]}
    gaps = catalog_gaps(T39["catalog"], [], {"jc:t39-one"}, matches, exclusions={})
    assert [(row["jc_num"], row["category"]) for row in gaps] == [
        ("周三003", "unexplained"),
        ("周三012", "unexplained"),
    ]
    text = "\n".join(
        review_sections({}, {}, matches, [], None, T39["catalog"][1:], [], now, exclusions={})
    )
    for number in ("周三003", "周三012"):
        line = next(item for item in text.splitlines() if item.startswith(f"- 缺 {number}"))
        assert "原因未查明" in line
        assert "（已查同步跳过记录与截点不晚于开球的冻结批次，均无该场记录）。" in line
    assert "原因未查明 2 场；模型范围外未预测 0 场" in text and "漏预测合计 2 场" in text
