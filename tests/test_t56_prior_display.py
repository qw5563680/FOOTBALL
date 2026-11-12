"""Real frozen T53 examples have compact rows and one explanation per table."""

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from zucai.digest_views import forecast_sections
from zucai.prediction_disclosure import prior_footnote, prior_label
from zucai.report.render import _csv_bytes, _markdown


def real_batch():
    return json.loads(
        (Path(__file__).parents[1] / "data/fixtures/t53/forecast-three.json").read_bytes()
    )


def test_real_daily_row_keeps_scores_and_model_name_inline_with_hash_reference():
    batch = real_batch()
    now = datetime.fromisoformat("2026-11-05T16:05:00+00:00")
    text = "\n".join(
        forecast_sections(
            [("2026-11-06", "fixture/forecast.json", batch)], now.date(), None, now=now
        )
    )
    line = next(line for line in text.splitlines() if line.startswith("|") and "AC米兰" in line)
    assert "默认实力：客队 费伦茨" in line
    assert "96.29% / 3.07% / 0.64%" in line
    assert text.count("默认实力说明：") == 1
    assert "weakest_quartile_half_shrinkage" in text.replace("\\_", "_")
    assert all(row["model_version"] in text for row in batch["predictions"])
    row = next(row for row in batch["predictions"] if row["match_id"] == "jc:2042523")
    for score in row["top_scores"][:5]:
        assert f"{score['score']} {score['probability']:.2%}" in line
    assert f"{row['model_name']} / M1" in line
    assert row["model_version"] not in line
    assert "比分 S" not in text and "| S1 |" not in text


@pytest.mark.parametrize("prior_today", [False, True])
def test_model_hash_and_p2_display_are_consistent_across_days_and_prior_use(prior_today):
    now = datetime.fromisoformat("2026-11-05T16:05:00+00:00")
    batch = real_batch()
    batches = []
    for offset in range(2):
        current = deepcopy(batch)
        day = (date(2026, 11, 6) + timedelta(days=offset)).isoformat()
        current["batch_id"] += "-" + day
        for index, row in enumerate(current["predictions"]):
            unknown = index == 2 and (prior_today if offset == 0 else not prior_today)
            row["metadata"] = {
                "unknown_team_prior": unknown,
                "home_unknown_team": False,
                "away_unknown_team": unknown,
                "prior_method": "weakest_quartile_half_shrinkage" if unknown else None,
            }
            row["p2_ready"] = index == 0
            row["kickoff_utc"] = (
                datetime.fromisoformat(row["kickoff_utc"]) + timedelta(days=offset)
            ).isoformat()
        batches.append((day, f"fixture/{day}/forecast.json", current))
    text = "\n".join(forecast_sections(batches, date(2026, 11, 6), None, now=now))
    lines = [line for line in text.splitlines() if line.startswith("| 周四")]
    assert len(lines) == 6
    assert [line.split(" | ")[5].split("；")[0] for line in lines] == [
        "可用",
        "实验",
        "实验",
        "可用",
        "实验",
        "实验",
    ]
    assert all("dixon-coles / M1" in line for line in lines)
    assert "experimental" not in text and "ready" not in text
    assert text.count("默认实力说明：") == 1
    assert text.count(batch["predictions"][0]["model_version"]) == 2


def test_non_hash_model_version_stays_inline():
    batch = real_batch()
    batch["predictions"] = [batch["predictions"][0]]
    batch["predictions"][0]["model_version"] = "legacy-v1"
    now = datetime.fromisoformat("2026-11-05T16:05:00+00:00")
    text = "\n".join(
        forecast_sections(
            [("2026-11-06", "fixture/forecast.json", batch)], now.date(), None, now=now
        )
    )
    line = next(line for line in text.splitlines() if line.startswith("| 周四"))
    assert "dixon-coles / legacy-v1" in line
    assert "模型版本 M" not in text


def test_short_label_and_csv_preserve_original_long_disclosure():
    batch = real_batch()
    row = next(row for row in batch["predictions"] if row["match_id"] == "jc:2042523")
    assert prior_label(row) == "默认实力：客队 费伦茨"
    exported = _csv_bytes(batch).decode("utf-8-sig")
    assert hashlib.sha256(_csv_bytes(batch)).hexdigest() == (
        "531d2edff7c0b6f88e3f0cd7b2cf12adce37793fea02cae39ff4138725c2ae57"
    )
    assert (
        "使用默认实力（客队 费伦茨；先验方法：weakest_quartile_half_shrinkage）；"
        "仅保留研究概率，不进入主候选" in exported
    )
    text = _markdown(batch)
    assert text.count("默认实力说明：") == 1
    lines = [line for line in text.splitlines() if line.startswith("|")]
    assert not any("先验方法" in line for line in lines)


def test_mixed_prior_methods_remain_attached_to_their_exact_matches():
    rows = [
        {"match_id": "jc:one", "metadata": {"unknown_team_prior": True, "prior_method": "first"}},
        {"match_id": "jc:two", "metadata": {"unknown_team_prior": True, "prior_method": "second"}},
        {"match_id": "jc:three", "metadata": {"unknown_team_prior": True}},
    ]
    note = prior_footnote(rows)
    assert note.count("默认实力说明：") == 1
    assert "jc:one=first" in note and "jc:two=second" in note and "jc:three=未记录" in note
