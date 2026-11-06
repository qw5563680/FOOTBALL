"""Separate F cashflows and immutable paper decisions, entirely synthetic fixtures."""

import copy
import json

import pytest

from test_parlay_single_double import FIX
from zucai.parlay.archive import raw_json
from zucai.parlay.paper import advance_ledger, summarize
from zucai.parlay.single_double import Config, build

BEFORE = "2026-11-06T02:05:00+00:00"
AFTER = "2026-11-06T18:05:00+00:00"


@pytest.fixture
def document():
    sample = json.loads((FIX / "input.json").read_bytes())
    sample["batch"]["predictions"] = sample["batch"]["predictions"][:2]
    doc = build(**sample, config=Config(max_groups=1))
    return dict(
        doc,
        published_utc="2026-11-06T02:02:00+00:00",
        path="forecasts/2026-11-06/offline/parlay_sd.json",
        sha256="a" * 64,
    )


def results():
    return json.loads((FIX / "results.json").read_bytes())


def group_rows(ledger):
    return [r for r in ledger if r["record_type"] == "group"]


def test_owner_hit_but_group_loss_is_loss_with_two_separate_ticket_receipts(document):
    ledger = advance_ledger([document], {}, [], BEFORE)
    assert len(ledger) == 4  # two tickets, one group, one immutable batch marker
    assert all(r["p2_ready"] is False for r in ledger if r["record_type"] != "batch")
    assert all(r["p2_label"] == "P2 实验" for r in ledger if r["record_type"] != "batch")
    frozen = copy.deepcopy(ledger)
    ledger = advance_ledger([document], results(), ledger, AFTER)
    tickets = [r for r in ledger if r["record_type"] == "ticket"]
    assert [t["settlement"]["status"] for t in tickets] == ["won", "lost"]
    assert [t["settlement"]["profit"] for t in tickets] == [1.6, -2.0]
    group = group_rows(ledger)[0]
    assert group["settlement"]["covered"] is True
    assert group["settlement"]["status"] == "loss"
    assert group["settlement"]["profit"] == -0.4
    summary = summarize(ledger, results(), AFTER)
    assert summary["totals"]["coverage_rate"] == 1
    assert summary["totals"]["profit_rate"] == 0
    assert summary["totals"]["single_hit_rate"] == 1
    assert summary["totals"]["roi"] == -0.1
    assert summary["evidence"]["p2_experimental"]["prospective"]["settled_groups"] == 1
    for before, after in zip(frozen, ledger, strict=True):
        assert {k: v for k, v in before.items() if k != "settlement"} == {
            k: v for k, v in after.items() if k != "settlement"
        }
    assert raw_json(advance_ledger([document], results(), ledger, AFTER)) == raw_json(ledger)


def test_second_ticket_win_and_cancel_stays_pending(document):
    finished = results()
    finished["jc:t45-2"].update(home_goals=0, away_goals=0)
    ledger = advance_ledger([document], finished, [], AFTER)
    group = group_rows(ledger)[0]
    assert group["settlement"]["profit"] == 4.0
    assert group["replayed"] is True
    assert (
        summarize(ledger, finished, AFTER)["evidence"]["p2_experimental"]["replayed"][
            "settled_groups"
        ]
        == 1
    )
    finished["jc:t45-2"]["status"] = "canceled"
    pending = advance_ledger([document], finished, [], AFTER)
    assert all(r["settlement"] is None for r in pending if r["record_type"] != "batch")
    assert summarize(pending, finished, AFTER)["totals"]["pending_groups"] == 1


def test_existing_batch_cannot_reprice_reselect_or_open_extra_tickets(document):
    ledger = advance_ledger([document], {}, [], BEFORE)
    changed = copy.deepcopy(document)
    changed["groups"][0]["single"]["market"]["prices"]["h"] = 12
    changed["groups"] *= 2
    assert raw_json(advance_ledger([changed], {}, ledger, BEFORE)) == raw_json(ledger)
    assert len(advance_ledger([changed], results(), ledger, AFTER)) == len(ledger)
    ledger[0]["stake"] = 100
    with pytest.raises(ValueError, match="注单被修改"):
        advance_ledger([document], {}, ledger, BEFORE)


def test_repeated_teams_in_same_batch_rejected_but_new_batch_allowed(document):
    invalid = copy.deepcopy(document)
    invalid["groups"] *= 2
    with pytest.raises(ValueError, match="重复使用"):
        advance_ledger([invalid], {}, [], BEFORE)
    ledger = advance_ledger([document], {}, [], BEFORE)
    other = copy.deepcopy(document)
    other["batch_id"] += "-next-day"
    assert len(group_rows(advance_ledger([other], {}, ledger, BEFORE))) == 2


def test_late_freeze_cannot_be_scored_as_prematch(document):
    document["published_utc"] = "2026-11-06T15:00:00+00:00"
    ledger = advance_ledger([document], results(), [], AFTER)
    assert not group_rows(ledger)


def test_result_correction_is_audit_not_a_rewrite(document):
    ledger = advance_ledger([document], results(), [], AFTER)
    changed = results()
    changed["jc:t45-2"].update(home_goals=0, away_goals=0)
    assert raw_json(advance_ledger([document], changed, ledger, AFTER)) == raw_json(ledger)
    assert summarize(ledger, changed, AFTER)["settlement_corrections"] == ["jc:t45-2"]
