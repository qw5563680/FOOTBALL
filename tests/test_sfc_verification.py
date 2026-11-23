"""Offline field comparisons, evidence boundaries and notice revisions."""

from copy import deepcopy

import pytest

from zucai.sfc_verification import (
    notice_for_issue,
    pending_amendments,
    resolve_notices,
    verify_issue,
)

NOW = "2026-11-22T16:00:00Z"


def notice():
    return {
        "issue_no": "26171",
        "published_at": "2026-11-17T00:00:00Z",
        "observed_utc": "2026-11-18T00:00:00Z",
        "raw_sha256": "a" * 64,
        "source_url": "https://www.gstc.org.cn/example",
        "sale_start_utc": "2026-11-22T12:00:00Z",
        "sale_end_utc": "2026-11-26T14:00:00Z",
        "fixtures": [
            {"seq": seq, "home": f"主队{seq}", "away": f"客队{seq}"} for seq in range(1, 15)
        ],
    }


def test_exact_comparison_uses_instants_and_sequence_numbers():
    plan = notice()
    rows = list(reversed(plan["fixtures"]))
    result = verify_issue("26171", "2026-11-26T22:00:00+08:00", rows, [plan], NOW)
    assert result["status"] == "verified"
    assert result["differences"] == []
    assert result["official"]["raw_sha256"] == plan["raw_sha256"]


def test_pending_url_requires_its_own_readable_body_by_the_cutoff():
    amendment = dict(notice(), title="调整通知", kind="amendment", observed_utc=NOW)
    failed = dict(
        source_url=amendment["source_url"],
        title="调整通知",
        first_observed_utc="2026-11-22T14:00:00Z",
    )
    repeated = dict(failed, first_observed_utc="2026-11-22T15:00:00Z")
    assert not pending_amendments([failed], [], "2026-11-22T13:59:59Z")
    observed_later = dict(failed, failed_at_utc="2026-11-22T14:01:00Z")
    assert not pending_amendments([observed_later], [], "2026-11-22T14:00:30Z")
    assert pending_amendments([repeated, failed], [amendment], "2026-11-22T15:59:59Z") == [failed]
    for unreadable in (
        dict(amendment, source_url=amendment["source_url"] + "-mirror"),
        dict(amendment, fixtures=[]),
        dict(amendment, published_at="2026-11-23T00:00:00Z"),
        dict(amendment, notice_date="2026-11-24"),
    ):
        assert pending_amendments([failed], [unreadable], NOW) == [failed]
    assert not pending_amendments([failed], [amendment], NOW)


def test_wrong_side_and_order_are_conflicts():
    plan = notice()
    rows = deepcopy(plan["fixtures"])
    rows[0]["home"], rows[0]["away"] = rows[0]["away"], rows[0]["home"]
    rows[2]["seq"], rows[3]["seq"] = rows[3]["seq"], rows[2]["seq"]
    result = verify_issue("26171", plan["sale_end_utc"], rows, [plan], NOW)
    assert result["status"] == "conflict"
    assert {d["seq"] for d in result["differences"]} == {1, 3, 4}
    assert all(d["status"] == "conflict" for d in result["differences"])


def test_unbound_short_name_is_pending_id_and_label_required_for_approved_alias():
    plan = notice()
    rows = deepcopy(plan["fixtures"])
    plan["fixtures"][0]["home"] = "海登海姆"
    rows[0].update(home="海登", league="德乙", provider_home_id="5315")
    result = verify_issue("26171", plan["sale_end_utc"], rows, [plan], NOW)
    assert result["status"] == "verified"
    assert result["aliases"][0]["provider_id"] == "5315"
    rows[0]["provider_home_id"] = "441"
    result = verify_issue("26171", plan["sale_end_utc"], rows, [plan], NOW)
    assert result["status"] == "pending"
    assert result["differences"][0]["field"] == "home"


def test_notice_adjustment_only_known_after_observation_and_replaces_deadline():
    plan = notice()
    revision = dict(
        plan,
        published_at="2026-11-22T10:00:00Z",
        observed_utc="2026-11-22T17:00:00Z",
        sale_end_utc="2026-11-26T12:00:00Z",
        raw_sha256="b" * 64,
    )
    notices = [revision, plan]
    assert notice_for_issue("26171", notices, NOW)["raw_sha256"] == plan["raw_sha256"]
    later = "2026-11-23T00:00:00Z"
    result = verify_issue("26171", plan["sale_end_utc"], plan["fixtures"], notices, later)
    assert result["status"] == "conflict"
    assert result["differences"][0]["field"] == "sale_end_utc"
    assert result["official"]["raw_sha256"] == revision["raw_sha256"]


def test_missing_unavailable_wrong_game_or_issue_cannot_verify():
    plan = notice()
    for notices in ([], [dict(plan, issue_no="26200")], [dict(plan, game="半全场")]):
        assert verify_issue("26171", None, [], notices, NOW)["status"] == "pending"
    result = verify_issue("26171", None, [], [plan], NOW, source_error="timeout")
    assert result["status"] == "pending"
    assert "timeout" in result["reason"]


def test_incomplete_or_simultaneous_disagreeing_revision_is_not_verified():
    plan = notice()
    revision = dict(plan, published_at=NOW, observed_utc=NOW, fixtures=[], sale_end_utc=None)
    assert verify_issue("26171", None, [], [plan, revision], NOW)["status"] == "pending"
    other = dict(plan, sale_end_utc="2026-11-26T12:00:00Z", raw_sha256="b" * 64)
    result = verify_issue("26171", plan["sale_end_utc"], plan["fixtures"], [other, plan], NOW)
    assert result["status"] == "pending"
    assert result["official"].get("sale_end_utc") is None


def test_bound_fixture_identity_and_printed_team_cannot_disagree_about_side():
    plan = notice()
    rows = deepcopy(plan["fixtures"])
    row = rows[0]
    row.update(
        fixture_id="900",
        match_id="jc:7",
        mapping="matched",
        provider_home_id="5",
        provider_away_id="6",
        mapping_evidence={
            "basis": "provider_fixture_and_team_ids",
            "fixture_id": "900",
            "jc_id": "jc:7",
            "home_id": "5",
            "away_id": "6",
            "home": row["home"],
            "away": row["away"],
            "provenance": {"url": "https://trade.500.com/jczq/", "raw_sha256": "f" * 64},
        },
    )
    row["home"] = "主一简称"
    result = verify_issue("26171", plan["sale_end_utc"], rows, [plan], NOW)
    assert result["status"] == "verified"
    assert result["aliases"][0]["match_id"] == "jc:7"
    row["home"] = row["away"]
    result = verify_issue("26171", plan["sale_end_utc"], rows, [plan], NOW)
    assert result["status"] == "conflict"
    assert result["differences"][0]["seq"] == 1


def test_late_republication_cannot_supersede_an_adjustment():
    base = dict(notice(), title="下发本周奖期安排", kind="schedule", notice_date="2026-11-17")
    revision = dict(
        base,
        kind="amendment",
        title="调整本期停售时间",
        notice_date="2026-11-19",
        sale_end_utc="2026-11-26T12:00:00Z",
        published_at="2026-11-19T01:00:00Z",
        observed_utc="2026-11-19T02:00:00Z",
        raw_sha256="b" * 64,
    )
    late_mirror = dict(
        base,
        published_at="2026-11-20T04:00:00Z",
        observed_utc="2026-11-20T04:00:00Z",
        fixtures=[],
        sale_start_utc=None,
        sale_end_utc=None,
    )
    effective = notice_for_issue("26171", [late_mirror, base], NOW)
    assert effective["fixtures"] == base["fixtures"]
    effective = notice_for_issue("26171", [late_mirror, revision, base], NOW)
    assert effective["sale_end_utc"] == revision["sale_end_utc"]
    unreadable = dict(revision, fixtures=[], sale_end_utc=None)
    result = verify_issue(
        "26171", base["sale_end_utc"], base["fixtures"], [late_mirror, base, unreadable], NOW
    )
    assert result["status"] == "pending"
    assert result["official"]["raw_sha256"] == unreadable["raw_sha256"]


@pytest.mark.parametrize("future_field", ["observed_utc", "published_at", "notice_date"])
def test_archived_schedule_receipt_cannot_supply_evidence_before_its_cutoff(future_field):
    old = dict(notice(), title="原周公告", kind="schedule", notice_date="2026-11-17")
    # The feed manifest stores the notice receipt even before a provider snapshot exists.
    newer = dict(
        old,
        title="调整通知",
        kind="amendment",
        notice_date="2026-11-22",
        published_at="2026-11-22T10:00:00Z",
        observed_utc="2026-11-22T14:30:00Z",
        sale_end_utc="2026-11-26T13:30:00Z",
        raw_sha256="b" * 64,
    )
    newer.pop("fixtures")
    newer[future_field] = "2026-11-24" if future_field == "notice_date" else "2026-11-23T01:00:00Z"
    archived = [("26171", newer)]
    assert resolve_notices([old], archived, NOW)["26171"] == old
    retained = resolve_notices([old], archived, "2026-11-24T00:00:00Z")["26171"]
    assert retained["sale_end_utc"] == newer["sale_end_utc"]
    assert retained["observed_utc"] == newer["observed_utc"]
    assert retained["from_archive"] is True
