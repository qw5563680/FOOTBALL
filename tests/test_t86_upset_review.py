from datetime import UTC, date, datetime

import pytest

from sfc_publication_support import confirm_report, prepare_remote
from test_sfc_daily import complete_issue as complete_issue
from test_sfc_daily import files, install_results
from test_sfc_daily import sfc_archive as sfc_archive
from test_sfc_report import NOW as SFC_FREEZE
from zucai.cloud_feeds import export_feeds
from zucai.daily_digest import generate
from zucai.digest_context import frozen_rows
from zucai.digest_views import review_sections
from zucai.sfc_reports import publish_reports
from zucai.upset_review import sfc_prematch_rows, table

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
KICKOFF = "2026-11-02T17:00:00+00:00"  # Beijing Nov 3, after midnight.
RESULT_AT = "2026-11-02T19:15:00+00:00"


def _prediction(key, values, **evidence):
    return dict(
        match_id=key,
        model_probabilities=dict(zip("hda", values, strict=True)),
        model_version="synthetic-model",
        status="predicted",
        kickoff_utc=KICKOFF,
        home="主队",
        away="客队",
        **evidence,
    )


def _batch(name, generated, predictions):
    return dict(
        batch_id=name,
        generated_utc=generated,
        as_of_utc=generated,
        mode="prematch",
        predictions=predictions,
    )


def _score(name, forecast, actual, when=RESULT_AT):
    return {
        **forecast,
        "record_id": name + ":" + forecast["match_id"],
        "batch_id": name,
        "actual": actual,
        "available_utc": when,
        "home_goals": int(actual == "h"),
        "away_goals": int(actual == "a"),
    }


def test_weekly_tiers_use_latest_published_batch_once_across_midnight():
    high = _prediction("jc:high", (0.44, 0.30, 0.26))
    middle = _prediction("jc:middle", (0.50, 0.30, 0.20))
    low = _prediction("jc:low", (0.60, 0.25, 0.15))
    default = _prediction("jc:default", (0.50, 0.30, 0.20), metadata={"unknown_team_prior": True})
    late = _prediction("jc:late", (0.44, 0.30, 0.26))
    not_observed = _prediction("jc:future-result", (0.50, 0.30, 0.20))
    earlier_middle = _prediction("jc:middle", (0.65, 0.20, 0.15))
    first = _batch("first", "2026-11-02T14:00:00+00:00", [earlier_middle])
    current = _batch(
        "current", "2026-11-02T16:00:00+00:00", [high, middle, low, default, not_observed]
    )
    after = _batch("late", "2026-11-02T16:30:00+00:00", [late])
    latest, observed = frozen_rows(
        [
            ("2026-11-02", "first", first),
            ("2026-11-03", "current", current),
            ("2026-11-03", "late", after),
        ],
        {},
        NOW,
        publications={
            name: {"published_utc": stamp}
            for name, stamp in (
                ("first", "2026-11-02T14:01:00+00:00"),
                ("current", "2026-11-02T16:01:00+00:00"),
                ("late", KICKOFF),
            )
        },
    )
    scored = [
        _score("first", earlier_middle, "a"),
        _score("current", high, "d"),
        _score("current", middle, "h"),
        _score("current", low, "a"),
        _score("current", default, "d"),
        _score("late", late, "d"),
        _score("current", not_observed, "a", "2026-11-04T00:00:00+00:00"),
    ]
    summary = {"audit": {"active_keys": [r["record_id"] for r in scored]}}
    lines = review_sections(latest, observed, {}, scored, summary, None, None, NOW)
    assert "**爆冷预测成绩**" in lines
    assert "| 高 | 1 | 1 | 100.00% | 56.00% |" in lines
    assert "| 中 | 1 | 0 | 0.00% | 50.00% |" in lines
    assert "| 低 | 1 | 1 | 100.00% | 40.00% |" in lines


@pytest.mark.parametrize("published", [True, False])
def test_weekly_review_reads_available_sfc_results_without_waiting_for_entire_issue(
    sfc_archive, tmp_path, published
):
    archive, database, _, issue = sfc_archive
    if not published:
        prepare_remote(archive)  # The remote exists, but never receives this report.
    assert publish_reports(archive, now=SFC_FREEZE) == [issue.issue_no]
    if published:
        confirm_report(archive)
    install_results(database, count=13)
    export_feeds(database, archive, date(2026, 11, 3), "partial-sfc-results")
    before = files(archive / "sfc")
    first = generate(archive, tmp_path / "daily", now=NOW, run_id="t86").read_text()
    second = generate(archive, tmp_path / "daily-replay", now=NOW, run_id="t86").read_text()
    assert first == second
    assert files(archive / "sfc") == before
    if published:
        assert "**爆冷预测成绩**" in first
        assert "| 中 | 13 | 3 | 23.08% | 50.00% |" in first
    else:
        assert "爆冷预测成绩" not in first


@pytest.mark.parametrize("same_sides", [True, False])
def test_only_confirmed_same_match_counterparts_share_one_review_sample(same_sides):
    jc = dict(
        _prediction("jc:1", (0.50, 0.30, 0.20)),
        generated_utc="2026-11-02T14:00:00+00:00",
        published_utc="2026-11-02T14:01:00+00:00",
    )
    sfc = dict(
        match_id="fdorg:2",
        probabilities=dict(h=0.60, d=0.25, a=0.15),
        generated_utc="2026-11-02T16:00:00+00:00",
        published_utc="2026-11-02T16:01:00+00:00",
    )
    same = dict(league="Example", kickoff_utc=KICKOFF, home_team_id=1, away_team_id=2)
    links = {
        "jc:1": dict(same, counterpart_id="fdorg:2"),
        "fdorg:2": same if same_sides else dict(same, home_team_id=2, away_team_id=1),
    }
    lines = table([(jc, "d")], [(sfc, "d")], links, NOW)
    assert "| 低 | 1 | 1 | 100.00% | 40.00% |" in lines
    assert ("| 中 | 1 | 1 | 100.00% | 50.00% |" in lines) is not same_sides


def test_sfc_report_published_at_kickoff_never_replaces_earlier_frozen_row():
    row = dict(match_id="fdorg:2", matched_identity={"kickoff_utc": KICKOFF}, kickoff_utc=KICKOFF)
    timely = dict(
        mode="prematch",
        sale_state="published_before_deadline",
        generated_utc="2026-11-02T14:00:00+00:00",
        published_utc="2026-11-02T14:01:00+00:00",
        rows=[row],
    )
    late = dict(timely, generated_utc="2026-11-02T16:30:00+00:00", published_utc=KICKOFF)
    chosen = sfc_prematch_rows([("first", timely), ("late", late)], NOW)
    assert chosen["fdorg:2"]["forecast_path"] == "first"
