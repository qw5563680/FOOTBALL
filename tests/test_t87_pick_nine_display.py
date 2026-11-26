from copy import deepcopy
from datetime import timedelta
from math import prod

import pytest

from sfc_publication_support import confirm_report
from test_sfc_daily import files
from test_sfc_daily import sfc_archive as sfc_archive
from test_sfc_report import NOW
from test_sfc_report import complete_issue as complete_issue
from test_t82_sfc_conflict_digest import _render as render_t82
from test_t82_sfc_conflict_digest import replay as replay
from zucai.daily_digest import generate
from zucai.report import sfc
from zucai.sfc_digest import _pick_nine_score_line
from zucai.sfc_reports import publish_reports, read_reports

HEADING = "### 任九 · 模型复式"
COLUMNS = "| 原场号 | 对阵 | 复式选项 | 本场覆盖概率 |"


def _multi(text):
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def test_verified_frozen_report_replays_nine_choices_cost_and_probability(sfc_archive, tmp_path):
    archive, _, _, issue = sfc_archive
    assert publish_reports(archive, now=NOW) == [issue.issue_no]
    assert COLUMNS not in (archive / f"sfc/{issue.issue_no}/report.md").read_text()
    assert confirm_report(archive)
    originals = files(archive / "sfc")
    text = generate(archive, tmp_path / "first", now=NOW + timedelta(minutes=2)).read_text()
    again = generate(archive, tmp_path / "second", now=NOW + timedelta(minutes=2)).read_text()
    assert text == again
    assert files(archive / "sfc") == originals
    assert text.index("### 任九 · 模型单选") < text.index(HEADING)
    section = _multi(text)
    assert COLUMNS in section
    assert "建议购买" not in text and "推荐" not in text

    [(path, report)] = read_reports(archive)
    assert report["pick_nine_max_lines"] == 64
    plan = sfc.pick_nine_multi_plan(report)
    assert len(plan["selections"]) == 9
    assert len([line for line in section.splitlines() if line[2:3].isdigit()]) == 9
    assert plan["lines"] == prod(len(row["outcomes"]) for row in plan["selections"]) <= 64
    assert f"合计 {plan['lines']} 注（上限 64 注）" in section
    assert f"按每注 2 元为 {2 * plan['lines']} 元" in section
    assert f"复式九场全中概率 {plan['coverage_probability']:.2%}" in section
    assert f"同一期单选九场全中概率 {0.5**9:.2%}" in section
    assert "各场相互独立" in section
    assert COLUMNS in (archive / path / "report.md").read_text()
    print(f"任九复式 {plan['lines']} 注，全中估算 {plan['coverage_probability']:.2%}")


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("too_few", "只有 8 场有可用赛前概率，不足 9 场。"),
        ("late", "未确认报告在截止前发布，本期不展示任九复式。"),
        ("legacy", "这版冻结报告未记录任九复式注数上限。"),
        ("frozen_conflict", "官方公告和 500 网的场序或主客队不一致，本期不展示任九复式。"),
    ],
)
def test_no_multi_when_shared_eligibility_or_frozen_budget_is_missing(sfc_archive, case, reason):
    archive, _, _, issue = sfc_archive
    publish_reports(archive, now=NOW)
    assert confirm_report(archive)
    [(_, report)] = read_reports(archive)
    if case == "too_few":
        report["rows"][0]["probabilities"] = None
        report["rows"][1]["prior_disclosure"] = {"used": True}
        report["rows"][2]["identity_pending"] = {"home": {"name": "甲", "other": "乙"}}
        report["rows"][3]["kickoff_basis"] = "placeholder"
        report["rows"][4]["kickoff_utc"] = report["published_utc"]
        report["rows"][5]["probabilities"] = None
        assert reason in "\n".join(sfc.pick_nine_lines(report))
    elif case == "late":
        report["published_utc"] = report["source"]["sale_end_utc"]
        report["sale_state"] = "missed_deadline"
    elif case == "legacy":
        report.pop("pick_nine_max_lines")
    else:
        report["source"]["official_verification"] = {
            "differences": [dict(status="conflict", field="away", seq=2)]
        }
    assert report["issue_no"] == issue.issue_no
    assert sfc.pick_nine_multi_plan(report) is None
    output = "\n".join(sfc.pick_nine_multi_lines(report))
    assert reason in output and COLUMNS not in output


def test_t82_official_lineup_conflict_blocks_multi(replay, tmp_path):
    notice = deepcopy(replay[3])
    row = notice["fixtures"][6]
    row["home"], row["away"] = row["away"], row["home"]
    check, text = render_t82(replay, tmp_path, notice, "t87-conflict")
    assert check["status"] == "conflict"
    assert "场序或主客队不一致" in text
    assert "本期不展示单选、复式和任九" in text
    assert HEADING not in text and COLUMNS not in text


def test_multi_review_line_requires_current_report_version_and_result_cutoff():
    candidate = "sfc/26156/history/early/sfc.json"
    confirmed = "sfc/26156/history/early-published/sfc.json"
    now = NOW + timedelta(days=2)
    record = dict(
        all_hit=True,
        settled_utc=now.isoformat(),
        report_path=confirmed,
        candidate_path=candidate,
        report_sha256="a" * 64,
    )
    report = {"publication_evidence": {"candidate_path": candidate}}
    settled = {"26156": record}
    assert _pick_nine_score_line(settled, "26156", "not-the-version", {}, now, str) is None
    assert _pick_nine_score_line(settled, "26156", "derived", report, NOW, str) is None
    assert "第 26156 期 9 场全中" in _pick_nine_score_line(
        settled, "26156", "derived", report, now, str
    )
    record["all_hit"] = False
    assert "第 26156 期 未全中" in _pick_nine_score_line(settled, "26157", None, None, now, str)
