from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from sfc_publication_support import commit_report, confirm_report
from test_sfc_daily import files, freeze, install_results
from test_sfc_report import NOW
from test_sfc_report import complete_issue as complete_issue
from zucai.cloud_feeds import digest, export_feeds
from zucai.daily_digest import generate
from zucai.db import session_scope
from zucai.models import Result, SfcIssue
from zucai.scoreboard.command import run
from zucai.scoreboard.score import instant, read_results
from zucai.scoreboard.sfc import settle
from zucai.sfc_reports import confirm_publication, publish_reports

AS_OF = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _official_check(issue, *, field, official=None, checked_at=NOW):
    return {
        "issue_no": issue.issue_no,
        "provider_raw_sha256": issue.raw_sha256,
        "checked_at": checked_at.isoformat(),
        "status": "conflict",
        "differences": [{"status": "conflict", "field": field, "seq": 1}],
        **({"official": official} if official else {}),
    }


def _freeze_first_nine(complete_issue, tmp_path, *, conflict=False):
    engine, issue, batch = complete_issue
    number = issue.issue_no
    database = Path(engine.url.database)
    archive = tmp_path / "archive"
    batch["predictions"] = batch["predictions"][:9]
    batch["predictions"][0].update(
        model_probabilities={"h": 1.0, "d": 0.0, "a": 0.0},
        score_matrix=[[0.0, 0.0], [1.0, 0.0]],
    )
    if conflict:
        with session_scope(engine) as session:
            session.get(SfcIssue, number).official_verification = _official_check(
                issue, field="home"
            )
    export_feeds(database, archive, date(2026, 11, 1), "t87-frozen")
    freeze(archive, tmp_path / "stage", batch, "early", NOW)
    assert publish_reports(archive, now=NOW) == [number]
    return engine, issue, archive


@pytest.mark.parametrize("first_leg_hits", [True, False])
def test_selected_nine_settle_without_other_five_and_preserve_originals(
    complete_issue, tmp_path, first_leg_hits
):
    engine, issue, archive = _freeze_first_nine(complete_issue, tmp_path)
    (confirmed,) = confirm_report(archive)
    database = Path(engine.url.database)
    install_results(database, count=9)
    if not first_leg_hits:
        with session_scope(engine) as session:
            first = session.get(Result, issue.snapshot[0]["match_id"])
            first.home_goals, first.away_goals = 0, 2
            first.available_utc = "2026-11-03T10:01:00+00:00"
    before = files(archive / "sfc")
    results = read_results(database)
    added, summary = settle(archive, results, [], AS_OF)
    assert added == [] and summary["issues"] == 0
    ((number, record),) = summary["pick_nine_multi"].items()
    assert number == issue.issue_no
    assert record["all_hit"] is first_leg_hits
    assert instant(record["settled_utc"]) == datetime(
        2026, 11, 3, 10, 0 if first_leg_hits else 1, tzinfo=UTC
    )
    assert record["report_path"] == confirmed + "/sfc.json"
    assert record["report_sha256"] == digest(archive / record["report_path"])
    assert settle(archive, results, [], AS_OF) == (added, summary)
    assert files(archive / "sfc") == before
    if first_leg_hits:
        export_feeds(database, archive, date(2026, 11, 3), "t87-results")
        scoreboard = run(archive, as_of=AS_OF)
        assert scoreboard["sfc"]["pick_nine_multi"] == summary["pick_nine_multi"]
        assert (archive / "sfc/settled.jsonl").read_bytes() == b""
        text = generate(archive, tmp_path / "daily", now=AS_OF, run_id="t87-settled").read_text()
        assert f"任九复式复盘 第 {issue.issue_no} 期 9 场全中" in text
        assert record["report_path"] in text


@pytest.mark.parametrize("block", ["unknown_publication", "late", "conflict", "incomplete"])
def test_unavailable_or_incomplete_plan_has_no_result(complete_issue, tmp_path, block):
    engine, issue, archive = _freeze_first_nine(
        complete_issue, tmp_path, conflict=block == "conflict"
    )
    if block != "unknown_publication":
        published = (
            issue.sale_end_utc if block == "late" else (NOW + timedelta(minutes=1)).isoformat()
        )
        confirm_report(archive, at=published)
    database = Path(engine.url.database)
    install_results(database, count=8 if block == "incomplete" else 9)
    added, summary = settle(archive, read_results(database), [], AS_OF)
    assert not added
    assert not summary.get("pick_nine_multi")


@pytest.mark.parametrize("changed", ["home", "sale_end_utc"])
def test_newly_observed_official_conflict_or_earlier_deadline_blocks_review(
    complete_issue, tmp_path, changed
):
    engine, issue, archive = _freeze_first_nine(complete_issue, tmp_path)
    confirm_report(archive)
    database = Path(engine.url.database)
    install_results(database, count=9)
    official = None
    if changed == "sale_end_utc":
        official = {
            "sale_start_utc": (NOW - timedelta(hours=2)).isoformat(),
            "sale_end_utc": (NOW + timedelta(seconds=30)).isoformat(),
        }
    with session_scope(engine) as session:
        session.get(SfcIssue, issue.issue_no).official_verification = _official_check(
            issue, field=changed, official=official, checked_at=NOW + timedelta(minutes=2)
        )
    manifest = export_feeds(database, archive, date(2026, 11, 3), "t87-updated")
    added, summary = settle(archive, read_results(database), [], AS_OF, feed=manifest)
    assert not added
    assert not summary.get("pick_nine_multi")


def test_latest_confirmed_report_without_nine_legs_does_not_grade_an_older_one(
    complete_issue, tmp_path
):
    engine, issue, archive = _freeze_first_nine(complete_issue, tmp_path)
    confirm_report(archive)
    later = NOW + timedelta(minutes=2)
    batch = complete_issue[2]
    batch["predictions"] = batch["predictions"][:8]
    freeze(archive, tmp_path / "later-stage", batch, "later", later)
    assert publish_reports(archive, now=later) == [issue.issue_no]
    commit_report(archive, (later + timedelta(minutes=1)).isoformat())
    assert len(confirm_publication(archive)) == 1

    database = Path(engine.url.database)
    install_results(database, count=9)
    added, summary = settle(archive, read_results(database), [], AS_OF)
    assert not added
    assert not summary.get("pick_nine_multi")


@pytest.mark.parametrize("changed", ["home", "source_ended"])
def test_current_lineup_changes_are_distinguished_from_the_normal_finished_flag(
    complete_issue, tmp_path, changed
):
    engine, issue, archive = _freeze_first_nine(complete_issue, tmp_path)
    confirm_report(archive)
    database = Path(engine.url.database)
    install_results(database, count=9)
    with session_scope(engine) as session:
        current = session.get(SfcIssue, issue.issue_no)
        check = _official_check(issue, field=changed, checked_at=AS_OF)
        current.official_verification = dict(check, status="pending", differences=[])
        current.snapshot = [
            {**current.snapshot[0], changed: "待核新主队" if changed == "home" else True},
            *current.snapshot[1:],
        ]
    manifest = export_feeds(database, archive, date(2026, 11, 3), "t87-lineup")
    _, summary = settle(archive, read_results(database), [], AS_OF, feed=manifest)
    assert bool(summary.get("pick_nine_multi")) is (changed == "source_ended")
    run(archive, as_of=AS_OF)
    text = generate(archive, tmp_path / "daily", now=AS_OF, run_id="t87-lineup").read_text()
    assert (f"任九复式复盘 第 {issue.issue_no} 期 9 场全中" in text) is (changed == "source_ended")
