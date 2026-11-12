"""Opportunity counts are a read-only view of immutable paper groups."""

from copy import deepcopy

from test_parlay_paper import AFTER, BEFORE, results
from test_parlay_paper import document as document
from zucai.parlay.archive import raw_json
from zucai.parlay.paper import advance_ledger, render, summarize


def test_cross_batch_duplicates_keep_cash_and_count_first_group_once(document):
    second = deepcopy(document)
    second["batch_id"] += "-repeat"
    second["generated_utc"] = "2026-11-06T02:01:00+00:00"
    ledger = advance_ledger([document, second], results(), [], AFTER)
    before = raw_json(ledger)
    summary = summarize(ledger, results(), AFTER)
    assert summary["totals"]["groups"] == 2
    assert summary["totals"]["covered_groups"] == 2
    assert summary["totals"]["paper_invested"] == 8
    assert summary["totals"]["profit"] == -0.8
    dedup = summary["deduplication"]
    assert dedup["unique_combinations"] == 1
    assert dedup["first_occurrence"]["covered_groups"] == 1
    assert dedup["first_occurrence"]["won_tickets"] == 1
    repeated = dedup["repeated_combinations"][0]
    assert repeated["occurrences"] == 2 and repeated["repeat_count"] == 1
    assert len(repeated["batch_ids"]) == 2
    assert all(row["ticket_appearances"] == 4 for row in dedup["most_repeated_matches"])
    text = render(summary)
    assert "2 组 / 1 种不同组合" in text
    assert "按首次出现去重" in text and "出现 2 次（重复 1 次）" in text
    single, double = document["groups"][0]["single"], document["groups"][0]["double"]
    assert f"单选 {single['home']} VS {single['away']}（{single['match_id']}）主胜" in text
    assert f"双选 {double['home']} VS {double['away']}（{double['match_id']}）主胜+平" in text
    most = next(line for line in text.splitlines() if line.startswith("被重复押注最多"))
    assert f"{single['home']} VS {single['away']}（{single['match_id']}）4 注 / 2 组" in most
    assert f"{double['home']} VS {double['away']}（{double['match_id']}）4 注 / 2 组" in most
    assert raw_json(ledger) == before


def test_names_are_display_only_and_come_from_the_first_frozen_group(document):
    ledger = advance_ledger([document], results(), [], AFTER)
    second = deepcopy(document)
    second["batch_id"] += "-repeat"
    second["generated_utc"] = "2026-11-06T02:04:00+00:00"
    second["published_utc"] = "2026-11-06T02:04:30+00:00"
    later = advance_ledger([second], results(), [], AFTER)
    for row in later:
        if row["record_type"] == "group":
            row["frozen"]["single"]["home"] = "后批改名"
    before = raw_json([*later, *ledger])
    summary = summarize([*later, *ledger], results(), AFTER)
    assert summary["deduplication"]["unique_combinations"] == 1
    assert "后批改名" not in render(summary)
    assert (
        summary["deduplication"]["repeated_combinations"][0]["single"]["home"]
        == (document["groups"][0]["single"]["home"])
    )
    assert raw_json([*later, *ledger]) == before


def test_missing_names_and_markup_are_explicitly_safe_in_the_readable_list(document):
    second = deepcopy(document)
    second["batch_id"] += "-repeat"
    ledger = advance_ledger([document, second], {}, [], BEFORE)
    for row in ledger:
        if row["record_type"] == "group":
            row["frozen"]["single"]["home"] = "甲|[队]"
            row["frozen"]["single"].pop("away", None)
            row["frozen"]["double"].pop("home", None)
            row["frozen"]["double"].pop("away", None)
            row["frozen"]["double_selections"] = ["a", "d"]
    before = raw_json(ledger)
    text = render(summarize(ledger, {}, BEFORE))
    assert r"甲\|\[队\] VS 客队名未记录（jc:t45-1）主胜" in text
    assert "主队名未记录 VS 客队名未记录（jc:t45-2）平+客胜" in text
    assert raw_json(ledger) == before


def test_key_keeps_leg_roles_and_options_but_ignores_price_and_double_order(document):
    second = deepcopy(document)
    second["batch_id"] += "-other"
    frozen = second["groups"][0]
    frozen["double_selections"].reverse()
    frozen["single"]["market"]["prices"]["h"] = 99
    ledger = advance_ledger([document, second], {}, [], BEFORE)
    assert summarize(ledger, {}, BEFORE)["deduplication"]["unique_combinations"] == 1
    group = next(
        row
        for row in ledger
        if row.get("batch_id") == second["batch_id"] and row["record_type"] == "group"
    )
    group["frozen"]["single_selection"] = "a"
    assert summarize(ledger, {}, BEFORE)["deduplication"]["unique_combinations"] == 2
    group["frozen"]["single_selection"] = document["groups"][0]["single_selection"]
    group["frozen"]["single"], group["frozen"]["double"] = (
        group["frozen"]["double"],
        group["frozen"]["single"],
    )
    assert summarize(ledger, {}, BEFORE)["deduplication"]["unique_combinations"] == 2


def test_first_occurrence_does_not_borrow_a_later_settlement(document):
    ledger = advance_ledger([document], {}, [], BEFORE)
    second = deepcopy(document)
    second["batch_id"] += "-repeat"
    second["generated_utc"] = "2026-11-06T02:04:00+00:00"
    later = advance_ledger([second], results(), [], AFTER)
    summary = summarize([*later, *ledger], {}, AFTER)
    assert summary["totals"]["settled_groups"] == 1
    assert summary["deduplication"]["first_occurrence"]["settled_groups"] == 0
    assert summary["deduplication"]["first_occurrence"]["pending_groups"] == 1
    assert summary["deduplication"]["first_occurrence"]["coverage_rate"] is None


def test_empty_ledger_has_no_repeated_opportunities():
    dedup = summarize([], {}, BEFORE)["deduplication"]
    assert dedup["unique_combinations"] == 0
    assert not dedup["most_repeated_matches"]
    assert not dedup["repeated_combinations"]
