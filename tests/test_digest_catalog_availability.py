"""Full daily generation must distinguish temporal evidence from scope filtering."""

import json
from pathlib import Path

import pytest

from test_daily_digest import digest_archive as digest_archive
from test_daily_digest import snapshot
from test_digest_horizon import catalog
from test_digest_review_regressions import install
from zucai.daily_digest import generate

CASES = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t46/catalog-availability.json").read_bytes()
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_generate_catalog_availability_states(digest_archive, tmp_path, case):
    row = {
        **catalog()[0],
        "captured_utc": case["captured_utc"],
        "kickoff_utc": case["kickoff_utc"],
    }
    install(digest_archive, [row])
    before = snapshot(digest_archive)
    out = tmp_path / "daily-output"
    path = generate(digest_archive, out, now=case["now"], run_id="offline-availability")
    text = path.read_text()
    section = text.split("### 竞彩完整清单逐号核对", 1)[1].split("\n## ", 1)[0]
    if case["expected"] == "unavailable":
        assert "截点前没有可用清单，无法核对是否漏预测。" in section, section
        assert "漏预测合计" not in section
        assert "已有归档清单" not in section
        assert row["jc_num"] not in section
    elif case["expected"] == "empty_scope":
        assert "已有归档清单，本周开球范围内及近期未知时间的条目为空。" in section
        assert "漏预测合计 0 场" in section
        assert "无法核对是否漏预测" not in section
        assert row["jc_num"] not in section
    else:
        assert row["jc_num"] in section
        assert "原因未查明 1 场" in section and "漏预测合计 1 场" in section
        assert "无法核对是否漏预测" not in section
    output_before = snapshot(out)
    generate(digest_archive, out, now=case["now"], run_id="offline-availability")
    assert snapshot(out) == output_before
    assert snapshot(digest_archive) == before
