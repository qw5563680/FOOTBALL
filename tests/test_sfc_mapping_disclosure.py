"""T58 report-only reasons; synthetic mutations never change fixture identities."""

from copy import deepcopy
from datetime import datetime

import pytest

from test_sfc_report import NOW, build
from test_sfc_report import complete_issue as complete_issue
from test_t58_sfc_names import FIXTURE
from test_t58_sfc_names import real_26164 as real_26164
from zucai.report.sfc import _unmapped_reason


@pytest.mark.parametrize(
    "row,expected",
    [
        (
            {"league": "友谊赛", "kickoff_basis": "placeholder"},
            "赛事不在采集范围（友谊赛）；开球时间为占位，待核实",
        ),
        (
            {"league": "巴甲", "mapping_evidence": {"basis": "exact_team_names"}},
            "队名精确匹配未找到对应比赛，待场次桥接（竞彩开售状态未核实）",
        ),
        (
            {"mapping_evidence": {"basis": "provider_fixture_and_team_ids"}},
            "已有来源编号，待场次桥接（竞彩开售状态未核实）",
        ),
        (
            {"league": "欧国联", "mapping_evidence": {"basis": "national_team_name_missing"}},
            "队名对照表缺该队",
        ),
        (
            {
                "league": "欧国联",
                "mapping_evidence": {"basis": "national_team_ids_and_exact_kickoff"},
            },
            "国家队编号与开球时间未找到唯一比赛，待核实",
        ),
        (
            {"mapping": "ambiguous", "mapping_evidence": {"basis": "exact_team_names"}},
            "存在多个候选比赛，待核实",
        ),
        (
            {"mapping_evidence": {"basis": "conflicting_identity"}},
            "比赛身份依据冲突，待核实",
        ),
        ({}, "尚无已核验的比赛对应关系"),
    ],
)
def test_unmapped_reason_uses_explicit_evidence_without_claiming_not_on_sale(row, expected):
    before = deepcopy(row)
    assert _unmapped_reason(row, row.get("mapping", "unmatched")) == expected
    assert row == before


def test_report_discloses_mapping_gap_without_changing_snapshot_or_probabilities(
    complete_issue, monkeypatch
):
    engine, issue, batch = complete_issue
    original_batch, original_snapshot = deepcopy(batch), deepcopy(issue.snapshot)
    rows = deepcopy(issue.snapshot)
    rows[0].update(
        match_id=None,
        matched_identity=None,
        mapping="unmatched",
        reason="unmatched",
        mapping_evidence={"basis": "national_team_name_missing"},
    )
    monkeypatch.setattr("zucai.report.sfc.refresh_snapshot", lambda *_: deepcopy(rows))
    report = build(engine, issue, batch, allow_missing=True)
    assert report["rows"][0]["reason"] == "未映射：队名对照表缺该队"
    assert report["rows"][0]["probabilities"] is None
    assert report["rows"][0]["selection"] == list("hda")
    for row, prediction in zip(report["rows"][1:], batch["predictions"][1:], strict=True):
        assert row["probabilities"] == prediction["model_probabilities"]
        assert row["reason"] is None
    assert batch == original_batch
    assert issue.snapshot == original_snapshot

    from zucai.report.sfc import _csv, _markdown
    from zucai.sfc_digest import _issue_lines

    report.update(
        publication_schema=1,
        published_utc=None,
        publication_commit=None,
        sale_end_utc=issue.sale_end_utc,
        sale_state="deadline_unconfirmed",
    )
    digest = "\n".join(
        _issue_lines(
            "state/sfc/26156.json",
            {
                "issue_no": issue.issue_no,
                "sale_end_utc": issue.sale_end_utc,
                "snapshot": rows,
            },
            [("sfc/26156", report)],
            [],
            NOW,
            lambda path: path,
        )
    )
    for text in (_markdown(report).decode(), _csv(report).decode("utf-8-sig"), digest):
        assert "无冻结概率；未映射：队名对照表缺该队" in text
        assert text.count("无冻结概率") == 14
        assert "50.00%" not in text and r"50\.00%" not in text

    # A later verified binding must not keep the old unmapped explanation.
    monkeypatch.setattr("zucai.report.sfc.refresh_snapshot", lambda *_: deepcopy(issue.snapshot))
    mapped_report = build(engine, issue, batch, allow_missing=True)
    assert mapped_report["rows"][0]["reason"] is None
    assert (
        mapped_report["rows"][0]["probabilities"] == batch["predictions"][0]["model_probabilities"]
    )


def test_real_26164_old_five_gaps_and_new_two_gaps_are_disclosed(real_26164, monkeypatch):
    from zucai import nations
    from zucai.report.sfc import _csv, _markdown, build_sfc_report

    session, issue = real_26164
    fixture_bytes = {p: p.read_bytes() for p in FIXTURE.iterdir() if p.is_file()}
    snapshot = deepcopy(issue.snapshot)
    # Deliberately empty diagnostic batch: this test never generates model probabilities.
    batch = {
        "schema_version": 1,
        "batch_id": "t58-real-snapshot-mapping-diagnostic-without-model",
        "generated_utc": issue.observed_utc,
        "as_of_utc": issue.observed_utc,
        "mode": "historical_replay",
        "probability_type": "P2",
        "validation_status": "experimental_unadopted",
        "bet_allowed": False,
        "input": {},
        "models": [],
        "predictions": [],
    }

    def report():
        return build_sfc_report(
            session,
            issue,
            batch,
            forecast_sha256="0" * 64,
            generated=datetime.fromisoformat(issue.observed_utc),
            allow_missing=True,
        )

    old_aliases = deepcopy(nations.sfc_aliases())
    for provider_id in ("77", "55", "26", "37", "157"):
        old_aliases["欧国联"].pop(provider_id, None)
    with monkeypatch.context() as scoped:
        scoped.setattr(nations, "sfc_aliases", lambda: old_aliases)
        before = report()
    after = report()
    assert [r["seq"] for r in before["rows"] if r["match_id"] is None] == [1, 2, 6, 10, 14]
    assert [r["seq"] for r in after["rows"] if r["match_id"] is None] == [1, 14]
    for seq in (2, 6, 10):
        assert before["rows"][seq - 1]["reason"] == "未映射：队名对照表缺该队"
        assert "未映射" not in after["rows"][seq - 1]["reason"]
    for result in (before, after):
        assert all(row["probabilities"] is None for row in result["rows"])
        assert "赛事不在采集范围（友谊赛）" in result["rows"][0]["reason"]
        assert "开球时间为占位，待核实" in result["rows"][0]["reason"]
        assert "待场次桥接（竞彩开售状态未核实）" in result["rows"][13]["reason"]
        for text in (_markdown(result).decode(), _csv(result).decode("utf-8-sig")):
            assert "开球时间为占位，待核实" in text
            assert "待场次桥接（竞彩开售状态未核实）" in text
    assert issue.snapshot == snapshot
    assert all(path.read_bytes() == content for path, content in fixture_bytes.items())
