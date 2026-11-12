"""T52 empty JC views must not erase observed SFC/catalog fixture presence."""

import hashlib
import json
from copy import deepcopy

import pytest

from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from zucai.daily_digest import generate
from zucai.digest_views import forecast_sections


def _install_state(archive, *, observed="2026-11-03T09:00:00Z", **changes):
    row = {
        "seq": 1,
        "fixture_id": "t52-synthetic",
        "league": "欧国联",
        "home": "测试国家队甲",
        "away": "测试国家队乙",
        "kickoff_utc": "2026-11-03T20:00:00Z",
        "mapping": "unmatched",
        "match_id": None,
        "reason": "目标比赛不存在",
    }
    row.update(changes)
    issue = {
        "issue_no": "26163",
        "observed_utc": observed,
        "sale_end_utc": "2026-11-03T18:00:00Z",
        "snapshot": [row],
    }
    name = "state/sfc/26163.json"
    path = archive / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"issue": issue}, ensure_ascii=False))
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    return issue


@pytest.mark.parametrize("mapped", [False, True])
def test_empty_jc_day_discloses_known_sfc_fixtures_without_rewriting_frozen_inputs(
    digest_archive, tmp_path, mapped
):
    _install_state(
        digest_archive,
        mapping="matched" if mapped else "unmatched",
        match_id="apif:t52-synthetic" if mapped else None,
    )
    before = snapshot(digest_archive)
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    tomorrow = text.split("### 明日 2026-11-04", 1)[1].split("## 14 场胜负彩", 1)[0]
    assert "原始胜负彩期次快照有 1 场未开球比赛" in tomorrow
    assert ("已映射 1 场" if mapped else "未映射 1 场") in tomorrow
    assert "本批竞彩表未展示这些比赛" in tomorrow
    assert "胜负彩专用冻结预测见下方" in tomorrow
    assert "该批次无未开球比赛" not in tomorrow
    assert snapshot(digest_archive) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"observed": "2026-11-03T10:01:00Z"},
        {"kickoff_utc": "2026-11-02T20:00:00Z"},
        {"source_ended": True},
        {"kickoff_basis": "placeholder"},
    ],
)
def test_unknown_or_later_sfc_facts_do_not_claim_upcoming_presence(
    digest_archive, tmp_path, changes
):
    _install_state(digest_archive, **changes)
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    tomorrow = text.split("### 明日 2026-11-04", 1)[1].split("## 14 场胜负彩", 1)[0]
    assert "原始胜负彩期次快照有" not in tomorrow
    assert "该批次无未开球比赛" in tomorrow


@pytest.mark.parametrize("has_batch", [False, True])
def test_known_jc_catalog_distinguishes_missing_prediction_from_no_matches(has_batch):
    from test_digest_rework import _batch

    batch = _batch()
    batch["predictions"] = []
    catalog = [
        {
            "match_id": "jc:t52-catalog-only",
            "kickoff_utc": "2026-11-03T20:00:00Z",
            "captured_utc": "2026-11-03T09:00:00Z",
        }
    ]
    original = deepcopy(catalog)
    text = "\n".join(
        forecast_sections(
            [("2026-11-04", "empty/forecast.json", batch)] if has_batch else [],
            NOW.date(),
            None,
            now=NOW,
            catalog=catalog,
        )
    )
    tomorrow = text.split("### 明日 2026-11-04", 1)[1]
    assert "竞彩目录有 1 场未开球比赛，但本批未提供对应竞彩预测" in tomorrow
    assert "该批次无未开球比赛" not in tomorrow
    assert catalog == original


def test_partial_day_retains_probabilities_and_reports_only_undisplayed_fixtures():
    from test_digest_rework import _batch

    batch = _batch()
    batch["predictions"] = batch["predictions"][:1]
    shown = batch["predictions"][0]
    issue = {
        "issue_no": "26163",
        "observed_utc": "2026-11-03T09:00:00Z",
        "snapshot": [
            {**shown, "mapping": "matched"},
            {**shown, "match_id": None, "mapping": "unmatched"},
        ],
    }
    catalog = [
        {**shown, "captured_utc": "2026-11-03T09:00:00Z"},
        {**shown, "match_id": "jc:missing", "captured_utc": "2026-11-03T09:00:00Z"},
    ]
    original = deepcopy(batch)
    text = "\n".join(
        forecast_sections(
            [("2026-11-03", "partial/forecast.json", batch)],
            NOW.date(),
            None,
            now=NOW,
            catalog=catalog,
            sfc_issues=[("state/sfc/26163.json", issue)],
        )
    )
    assert "竞彩目录有 1 场未开球比赛，但本批未提供对应竞彩预测" in text
    assert "原始胜负彩期次快照有 1 场未开球比赛" in text
    assert "未映射 1 场、已映射 0 场" in text
    assert "48.00% / 28.00% / 24.00%" in text
    assert batch == original


def test_sfc_only_context_does_not_claim_all_frozen_predictions_absent():
    issue = {
        "issue_no": "26163",
        "observed_utc": "2026-11-03T09:00:00Z",
        "snapshot": [
            {
                "match_id": "apif:t52-synthetic",
                "mapping": "matched",
                "kickoff_utc": "2026-11-03T20:00:00Z",
            }
        ],
    }
    text = "\n".join(
        forecast_sections(
            [], NOW.date(), None, now=NOW, sfc_issues=[("state/sfc/26163.json", issue)]
        )
    ).split("### 明日 2026-11-04", 1)[1]
    assert "尚无竞彩冻结预测" in text
    assert "尚无冻结预测" not in text
    assert "胜负彩专用冻结预测见下方" in text
