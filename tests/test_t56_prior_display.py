"""Real frozen T53 examples have compact rows and one explanation per table."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from zucai.digest_views import forecast_sections
from zucai.prediction_disclosure import prior_footnote, prior_label
from zucai.report.render import _csv_bytes, _markdown


def real_batch():
    return json.loads(
        (Path(__file__).parents[1] / "data/fixtures/t53/forecast-three.json").read_bytes()
    )


def test_real_daily_row_is_at_most_half_original_and_keeps_all_evidence():
    batch = real_batch()
    now = datetime.fromisoformat("2026-11-05T16:05:00+00:00")
    text = "\n".join(
        forecast_sections(
            [("2026-11-06", "fixture/forecast.json", batch)], now.date(), None, now=now
        )
    )
    line = next(line for line in text.splitlines() if line.startswith("|") and "AC米兰" in line)
    assert len(line) <= 160  # Baseline T53 daily rendering is exactly 320 characters.
    assert "默认实力：客队 费伦茨" in line
    assert "96.29% / 3.07% / 0.64%" in line
    assert text.count("默认实力说明：") == 1
    assert "weakest_quartile_half_shrinkage" in text.replace("\\_", "_")
    assert all(row["model_version"] in text for row in batch["predictions"])
    assert "4:0 14.70%" in text and "6:0 8.61%" in text
    assert "S" in line


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
