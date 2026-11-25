"""Replay official and provider fixture conflicts through archived SFC daily reports."""

import hashlib
from copy import deepcopy
from datetime import timedelta

import pytest

from sfc_publication_support import confirm_report
from test_sfc_daily import files
from test_sfc_official_pipeline import BEIJING_DAY, NOW, SAMPLES, _notice
from zucai.cloud_feeds import export_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db, session_scope
from zucai.models import SfcIssue
from zucai.report.sfc import build_sfc_report, write_sfc_report
from zucai.sfc_publication import with_publication
from zucai.sfc_reports import FILES
from zucai.sfc_verification import compare_issue
from zucai.sources.c500 import C500Sfc
from zucai.sources.sfc import ingest_issue

ISSUE = "26169"
CONSEQUENCE = "因此本期不展示单选、复式和任九，等两边核对一致后恢复。"
LATE_REASON = "未确认报告在截止前发布，本期不展示任九方案。"
ROLES = {"home": "主队", "away": "客队"}


@pytest.fixture
def replay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "feed.db"
    engine = get_engine(database)
    init_db(engine)
    page = (SAMPLES / f"c500_{ISSUE}.html").read_bytes()
    (snapshot,) = C500Sfc().parse(page)
    (official,) = (notice for notice in _notice() if notice["issue_no"] == ISSUE)
    archive = tmp_path / "archive"
    with session_scope(engine) as session:
        issue = ingest_issue(
            session,
            snapshot,
            observed=NOW,
            url=C500Sfc.url,
            raw_sha256=hashlib.sha256(page).hexdigest(),
        )
        issue.official_verification = _compare(issue, official, NOW)
        assert issue.official_verification["status"] == "pending"
        report = build_sfc_report(
            session,
            issue,
            _fixture_batch(issue.snapshot),
            generated=NOW,
            forecast_sha256="a" * 64,
        )
        assert report["combo"] and all(row["probabilities"] for row in report["rows"])
    export_feeds(database, archive, BEIJING_DAY, "initial")
    history = archive / f"sfc/{ISSUE}/history/before"
    write_sfc_report(with_publication(report), history)
    for filename in FILES:
        (history.parent.parent / filename).write_bytes((history / filename).read_bytes())
    assert confirm_report(archive, at=(NOW + timedelta(seconds=30)).isoformat())
    yield engine, database, archive, official
    engine.dispose()


def _compare(issue, official, at):
    check = compare_issue(ISSUE, issue.sale_end_utc, issue.snapshot, official, at)
    return {**check, "provider_raw_sha256": issue.raw_sha256}


def _fixture_batch(rows):
    """Frozen probabilities are synthetic display fixtures, with source fixture identities."""
    models, predictions = {}, []
    for row in rows:
        league = row["league"]
        version = hashlib.sha256(league.encode()).hexdigest()
        models[league] = dict(
            model_name="dixon-coles",
            model_version=version,
            source="c500",
            league=league,
            availability_basis="strict",
            model_as_of_utc=(NOW - timedelta(hours=1)).isoformat(),
            last_available_utc=(NOW - timedelta(hours=2)).isoformat(),
            training_games=100,
            assumed_training_share=0,
        )
        predictions.append(
            dict(
                match_id=row["match_id"],
                **row["matched_identity"],
                status="predicted",
                model_name="dixon-coles",
                model_version=version,
                model_probabilities={"h": 0.5, "d": 0.3, "a": 0.2},
                score_matrix=[[0.3, 0.2], [0.5, 0]],
                top_scores=[],
                market=None,
                ev=None,
            )
        )
    return dict(
        schema_version=1,
        batch_id="synthetic-test-only",
        generated_utc=NOW.isoformat(),
        as_of_utc=NOW.isoformat(),
        mode="prematch",
        probability_type="P2",
        validation_status="experimental_unadopted",
        bet_allowed=False,
        input={},
        models=list(models.values()),
        predictions=predictions,
    )


def _render(replay, tmp_path, official, version, *, minute=1):
    engine, database, archive, _ = replay
    at = NOW + timedelta(minutes=minute)
    with session_scope(engine) as session:
        issue = session.get(SfcIssue, ISSUE)
        check = _compare(issue, official, at)
        issue.official_verification = check
    export_feeds(database, archive, BEIJING_DAY, version)
    rendered = generate(archive, tmp_path / "daily", now=at, run_id=version).read_text(
        encoding="utf-8"
    )
    section = rendered.split("## 14 场胜负彩", 1)[1].split("\n## ", 1)[0]
    return check, section


def _check_team_lines(check, text):
    conflicts = [d for d in check["differences"] if d["status"] == "conflict"]
    assert conflicts
    rendered = [line for line in text.splitlines() if "官方公告写" in line]
    assert len(rendered) == len(conflicts)
    for difference in conflicts:
        assert any(
            f"第 {difference['seq']} 场{ROLES[difference['field']]}" in line
            and f"官方公告写『{difference['official']}』" in line
            and f"500 网写『{difference['provider']}』" in line
            and f"第 {difference['official_seq']} 场{ROLES[difference['official_side']]}" in line
            for line in rendered
        ), difference
    assert text.count(CONSEQUENCE) == 2
    assert CONSEQUENCE in text.split("### 任九 · 模型单选", 1)[1]
    assert LATE_REASON not in text
    assert "各取模型最高项" not in text and "复式 64 注" not in text
    assert "| 原场号 | 对阵 | 单选 | 模型概率 |" not in text
    print("\n".join(rendered + [CONSEQUENCE]))


@pytest.mark.parametrize("change", ["sides", "order"])
def test_conflict_lists_every_real_mismatch_with_exact_position(replay, tmp_path, change):
    official = deepcopy(replay[3])
    sixth, seventh = official["fixtures"][5:7]
    assert seventh == {"seq": 7, "home": "莱比锡红牛", "away": "朗斯"}
    if change == "sides":
        seventh["home"], seventh["away"] = seventh["away"], seventh["home"]
    else:
        sixth["seq"], seventh["seq"] = seventh["seq"], sixth["seq"]
    check, text = _render(replay, tmp_path, official, change)
    assert check["status"] == "conflict" and "官方核对：冲突" in text
    _check_team_lines(check, text)


def test_sales_deadline_conflict_keeps_existing_eligibility(replay, tmp_path):
    official = dict(replay[3], sale_end_utc="2026-11-24T13:30:00+00:00")
    check, text = _render(replay, tmp_path, official, "deadline")
    assert [d["field"] for d in check["differences"] if d["status"] == "conflict"] == [
        "sale_end_utc"
    ]
    (difference,) = [line for line in text.splitlines() if "官方公告写" in line]
    assert "停售" in difference and "北京时间" in difference
    assert "官方公告写『2026-11-24 21:30:00』" in difference
    assert "500 网写『2026-11-24 22:00:00』" in difference
    # The frozen report really was published before either deadline.
    assert CONSEQUENCE not in text
    _assert_choices(text)
    print(difference)


def _assert_choices(text):
    assert "各取模型最高项" in text
    assert "复式 64 注" in text
    assert "| 原场号 | 对阵 | 单选 | 模型概率 |" in text


def test_pending_differences_keep_existing_display(replay, tmp_path):
    check, text = _render(replay, tmp_path, replay[3], "pending")
    assert check["status"] == "pending" and check["differences"]
    assert all(d["status"] == "pending" for d in check["differences"])
    assert "官方核对：待核" in text
    assert f"核对差异 {len(check['differences'])} 项，逐场记录见" in text
    assert "官方公告写" not in text and CONSEQUENCE not in text
    _assert_choices(text)


def test_next_digest_recovers_choices_without_overwriting_frozen_reports(replay, tmp_path):
    engine, _, archive, official = replay
    frozen = files(archive / "sfc")
    conflict = deepcopy(official)
    row = conflict["fixtures"][6]
    row["home"], row["away"] = row["away"], row["home"]
    _, blocked = _render(replay, tmp_path, conflict, "conflict")
    assert CONSEQUENCE in blocked
    with session_scope(engine) as session:
        issue = session.get(SfcIssue, ISSUE)
        corrected = dict(
            official,
            fixtures=[{k: row[k] for k in ("seq", "home", "away")} for row in issue.snapshot],
        )
    check, restored = _render(replay, tmp_path, corrected, "restored", minute=2)
    assert check["status"] == "verified" and not check["differences"]
    assert "官方核对：已核对" in restored and CONSEQUENCE not in restored
    _assert_choices(restored)
    assert len(frozen) == 12 and files(archive / "sfc") == frozen
    print("冲突解除，14 场单选、复式、任九恢复；原冻结报告 12 个文件逐字节一致")
