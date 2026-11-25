"""T89's read-only display of stored backup snapshots and collection errors."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from zucai import sfc_digest
from zucai.okooo_probe import inspect_page

NOW = datetime(2026, 11, 24, 9, tzinfo=UTC)
ISSUE = "26169"
SNAPSHOT = "state/sfc/26169.json"
OKOOO = "https://www.okooo.com/zucai/26169/"


@pytest.fixture
def issue():
    sample = Path(__file__).parents[1] / "data/fixtures/okooo/26169.html"
    page = inspect_page(sample.read_bytes(), ISSUE, "sfc", NOW.isoformat())
    assert page["read_status"] == "complete"
    return dict(
        issue_no=ISSUE,
        source_url=OKOOO,
        sale_end_utc=page["sale_end_utc"],
        snapshot=[
            dict(
                seq=r["seq"],
                fixture_id=r["okooo_fixture_id"],
                league=r["league"],
                home=r["home"],
                away=r["away"],
                kickoff_utc=r["kickoff_beijing"],
                match_id=None,
                mapping="unmatched",
                mapping_evidence={
                    "basis": "okooo_exact_names_and_kickoff",
                    "provider": "okooo.com",
                    "missing_names": ["away"],
                },
            )
            for r in page["rows"]
        ],
    )


def _check(issue):
    row = issue["snapshot"][0]
    return dict(
        status="conflict",
        provider="okooo.com",
        checked_at="2026-11-24T08:50:00+00:00",
        differences=[
            dict(
                status="conflict",
                field="home",
                seq=1,
                official=row["away"],
                provider=row["home"],
                official_seq=1,
                official_side="away",
            ),
            dict(
                status="conflict",
                field="sale_end_utc",
                official="2026-11-24T13:30:00+00:00",
                provider=issue["sale_end_utc"],
            ),
        ],
    )


def _render(monkeypatch, states, collected):
    monkeypatch.setattr(sfc_digest, "read_reports", lambda *a, **kw: [])
    monkeypatch.setattr(sfc_digest, "resolve_publications", lambda *a, **kw: [])
    monkeypatch.setattr(sfc_digest, "read_settled", lambda *a, **kw: ([], None))
    monkeypatch.setattr(sfc_digest, "archived_sfc_issues", lambda *a, **kw: states)
    return "\n".join(sfc_digest.section(None, {"collection": {"sfc": collected}}, None, NOW, str))


def test_okooo_labels_conflict_and_unmapped_snapshot_without_frozen_report(issue):
    issue["official_verification"] = _check(issue)
    lines = sfc_digest._issue_lines(SNAPSHOT, issue, [], [], NOW, str)
    text = "\n".join(lines)
    assert "本期名单来自备用来源澳客" in text
    assert "第三方来源：澳客" in text
    assert "500" not in text
    assert "澳客写『博德』；公告里这支队在第 1 场客队" in text
    assert "澳客写『2026-11-24 22:00:00』" in text
    assert "官方公告和澳客的场序或主客队不一致" in text
    assert "未映射" in next(line for line in lines if "博德 VS 林 茨" in line)


@pytest.mark.parametrize("archived", [True, False])
def test_unavailable_fallback_distinguishes_retained_snapshot_from_no_list(
    monkeypatch, issue, archived
):
    # Saved 500 data remains available even when both current requests fail.
    issue["source_url"] = "https://trade.500.com/sfc/?expect=26169"
    notice = dict(
        observed_utc="2026-11-24T08:00:00+00:00",
        sale_start_utc="2026-11-22T12:00:00+00:00",
        sale_end_utc=issue["sale_end_utc"],
    )
    text = _render(
        monkeypatch,
        [(SNAPSHOT, issue)] if archived else [],
        dict(
            status="partial",
            official_schedule={ISSUE: notice},
            okooo=dict(status="unavailable", issue_no=ISSUE, reason="HTTP 405"),
        ),
    )
    assert "本班澳客不可用" in text and "HTTP 405" in text
    assert ("继续展示已归档名单" if archived else "第 26169 期未取得完整名单") in text
    assert ("博德 VS 林 茨" in text) is archived
    assert "本期名单来自备用来源澳客" not in text


def test_partial_run_does_not_erase_same_run_okooo_conflicts(monkeypatch, issue):
    issue["official_verification"] = _check(issue)
    text = _render(
        monkeypatch,
        [(SNAPSHOT, issue)],
        dict(
            status="partial",
            official_checked_at="2026-11-24T08:45:00+00:00",
            okooo=dict(status="collected", issue_no=ISSUE),
        ),
    )
    assert "官方核对：冲突" in text
    assert "澳客写『博德』" in text


def test_failed_refresh_keeps_a_known_lineup_conflict_visible(monkeypatch, issue):
    issue["official_verification"] = _check(issue)
    text = _render(
        monkeypatch,
        [(SNAPSHOT, issue)],
        dict(
            status="partial",
            official_checked_at="2026-11-24T08:55:00+00:00",
            okooo=dict(status="unavailable", issue_no=ISSUE, reason="HTTP 405"),
        ),
    )
    assert "官方核对：待核" in text and "已归档对阵冲突仍待重新核对" in text
    assert "澳客写『博德』" in text and "本期不展示单选、复式和任九" in text


@pytest.mark.parametrize("okooo_current", [True, False])
def test_switching_providers_keeps_old_frozen_report_only_as_history(issue, okooo_current):
    primary = "https://trade.500.com/sfc/?expect=26169"
    issue["source_url"] = OKOOO if okooo_current else primary
    report = dict(
        issue_no=ISSUE,
        source={"url": primary if okooo_current else OKOOO},
        # Deliberately same fixture IDs, labels and times across different providers.
        rows=issue["snapshot"],
        generated_utc="2026-11-24T08:00:00+00:00",
        published_utc="2026-11-24T08:01:00+00:00",
        sale_state="published_before_deadline",
        combo=None,
        minimum_lines=3**14,
        mode="prematch",
    )
    frozen_path = "sfc/26169/history/earlier"
    text = "\n".join(
        sfc_digest._issue_lines(SNAPSHOT, issue, [(frozen_path, report)], [], NOW, str)
    )
    assert f"历史冻结报告见{frozen_path}/sfc.json" in text
    assert "当前名单来源与此前冻结报告不同" in text
    assert "主胜 | 平 | 客胜" not in text
