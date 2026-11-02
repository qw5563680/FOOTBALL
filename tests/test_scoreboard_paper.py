"""Virtual tickets keep their original selection, price and stake through settlement."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from test_scoreboard_score import NOW, samples

DECISION = datetime(2026, 11, 1, 10, tzinfo=UTC)


def paper_samples():
    forecasts, results = samples()
    return [
        r
        for r in forecasts
        if r["batch_id"] == "late" and r["match_id"] in {"jc:one", "jc:two", "jc:three", "jc:nosp"}
    ], results


def tickets(ledger, account, threshold="1.05"):
    return [
        r
        for r in ledger
        if r["record_type"] == "ticket" and r["account"] == account and r["threshold"] == threshold
    ]


def test_five_accounts_three_independent_literal_thresholds_and_parlay_settlement():
    from zucai.scoreboard.paper import account_statistics, advance_ledger

    forecasts, results = paper_samples()
    pending = advance_ledger(forecasts, results, [], DECISION)
    assert len(tickets(pending, "A")) == len(tickets(pending, "B")) == 3
    assert [r["stake"] for r in tickets(pending, "B")] == [200, 196, 192.08]
    assert len(tickets(pending, "A", "1.00")) == 3
    assert len(tickets(pending, "A", "1.10")) == 2
    assert not tickets(pending, "E", "1.10")
    assert any(
        r.get("reason") == "insufficient_candidates" and r["account"] == "E" for r in pending
    )
    assert all(r["settlement"] is None for r in pending if r["record_type"] == "ticket")
    settled = advance_ledger(forecasts, results, pending, NOW)
    assert advance_ledger(forecasts, results, settled, NOW) == settled
    for before, after in zip(pending, settled, strict=True):
        assert {k: v for k, v in before.items() if k != "settlement"} == {
            k: v for k, v in after.items() if k != "settlement"
        }
    assert tickets(settled, "D")[0]["settlement"]["return"] == 756
    assert tickets(settled, "E")[0]["settlement"]["return"] == 0
    summary = account_statistics(settled, NOW)
    a = summary["1.05"]["A"]
    assert (a["bets"], a["hits"], a["invested"], a["returns"], a["current_funds"]) == (
        3,
        2,
        300,
        550,
        10250,
    )
    assert a["roi"] == pytest.approx(5 / 6)
    assert summary["1.05"]["B"]["current_funds"] == 10501.12
    assert summary["1.05"]["E"]["longest_losing_streak"] == 1
    assert summary["1.05"]["E"]["max_drawdown"] == pytest.approx(0.01)
    assert len(a["weekly_curve"]) >= 1


def test_favourite_ignores_model_and_missing_sp_never_gets_a_ticket():
    from zucai.scoreboard.paper import advance_ledger

    forecasts, results = paper_samples()
    forecasts[0]["market"]["prices"]["d"] = 1.2
    ledger = advance_ledger(forecasts, results, [], DECISION)
    c = next(r for r in tickets(ledger, "C") if r["legs"][0]["match_id"] == "jc:one")
    a = next(r for r in tickets(ledger, "A") if r["legs"][0]["match_id"] == "jc:one")
    assert c["legs"][0]["selection"] == "d" and a["legs"][0]["selection"] == "h"
    assert not any(leg["match_id"] == "jc:nosp" for r in ledger for leg in r.get("legs", []))


@pytest.mark.parametrize(
    ("balance", "p", "sp", "expected"),
    [
        (10000, 0.8, 2.8, 200),
        (10000, 0.0021, 1000, 10),
        (400, 0.8, 2.8, None),
        (10000, 0.2, 2, None),
    ],
)
def test_kelly_cap_floor_and_cash_constraint(balance, p, sp, expected):
    from zucai.scoreboard.paper import kelly_stake

    assert kelly_stake(balance, p, sp) == expected


def test_strict_edge_boundary_and_tie_breaking_are_not_return_multiplier_rules():
    from zucai.scoreboard.paper import advance_ledger

    forecasts, results = paper_samples()
    forecasts = forecasts[:1]
    forecasts[0]["market"]["prices"]["h"] = 2.5  # .8*2.5-1 == 1.00
    ledger = advance_ledger(forecasts, results, [], DECISION)
    assert not tickets(ledger, "A", "1.00")
    forecasts[0]["market"]["prices"]["h"] = 2.5625  # edge ==1.05, strict comparison
    ledger = advance_ledger(forecasts, results, [], DECISION)
    assert len(tickets(ledger, "A", "1.00")) == 1 and not tickets(ledger, "A", "1.05")


def test_later_forecast_does_not_replace_ticket_or_use_future_winnings_for_kelly():
    from zucai.scoreboard.paper import advance_ledger

    forecasts, results = paper_samples()
    forecasts = forecasts[:1]
    initial = advance_ledger(forecasts, results, [], DECISION)
    updated = deepcopy(forecasts[0])
    updated.update(
        batch_id="new", generated_utc="2026-11-01T11:00:00Z", as_of_utc="2026-11-01T11:00:00Z"
    )
    updated["market"]["prices"]["h"] = 9
    second = deepcopy(updated)
    second.update(match_id="jc:second", home="新主", away="新客")
    second["market"]["prices"]["h"] = 2.8
    ledger = advance_ledger([*forecasts, updated, second], results, initial, NOW)
    assert len(tickets(ledger, "A")) == 2
    assert tickets(ledger, "A")[0]["legs"][0]["sp"] == 2.8
    b = tickets(ledger, "B")
    assert b[0]["stake"] == 200 and b[1]["stake"] == 196  # first win only known at16:00
    assert b[1]["replayed"]  # created after kickoff, using frozen inputs, explicitly disclosed


def test_fixed_accounts_reserve_pending_cash_and_never_borrow():
    from zucai.scoreboard.paper import account_statistics, advance_ledger

    forecasts, results = paper_samples()
    template = forecasts[0]
    many = [{**deepcopy(template), "match_id": f"jc:cash-{i:03d}"} for i in range(101)]
    ledger = advance_ledger(many, {}, [], DECISION)
    assert len(tickets(ledger, "A")) == 100
    a = account_statistics(ledger, DECISION)["1.05"]["A"]
    assert a["available_cash"] == 0 and a["current_funds"] == 10000
    assert a["roi"] is None and a["pending"] == 100
    assert any(r.get("reason") == "insufficient_cash" and r["account"] == "A" for r in ledger)


def test_robustness_seed_trim_and_profit_gate():
    from zucai.scoreboard.paper import profit_evidence, robustness

    a = robustness([100] * 20, [110] * 19 + [10000])
    assert a == robustness([100] * 20, [110] * 19 + [10000])
    assert a["trimmed_roi"] == pytest.approx(0.1)
    assert a["bootstrap_samples"] == 1000
    assert a["roi_ci95"][0] == pytest.approx(0.1)
    stats = {"settled": 500, "roi": 0.1, **a}
    assert profit_evidence(stats)
    for field, value in [
        ("settled", 499),
        ("roi", 0),
        ("trimmed_roi", 0),
        ("roi_ci95", [-0.02, 0.3]),
    ]:
        assert not profit_evidence({**stats, field: value})
    assert not profit_evidence({"settled": 0, "roi": None, **robustness([], [])})


def test_immutable_ticket_corruption_is_rejected():
    from zucai.scoreboard.paper import advance_ledger

    forecasts, results = paper_samples()
    ledger = advance_ledger(forecasts, results, [], DECISION)
    tickets(ledger, "A")[0]["stake"] += 1
    with pytest.raises(ValueError, match="注单"):
        advance_ledger(forecasts, results, ledger, NOW)


@pytest.mark.parametrize("miss", [None, "jc:one"])
def test_both_parlays_pay_only_when_every_leg_wins(miss):
    from zucai.scoreboard.paper import advance_ledger

    forecasts, results = paper_samples()
    results["jc:three"].update(home_goals=1, away_goals=0)
    if miss:
        results[miss].update(home_goals=0, away_goals=1)
    ledger = advance_ledger(forecasts, results, [], NOW)
    assert tickets(ledger, "D")[0]["settlement"]["return"] == (0 if miss else 756)
    assert tickets(ledger, "E")[0]["settlement"]["return"] == (0 if miss else 1965.6)


def test_cent_amounts_sum_without_binary_float_truncation():
    from zucai.scoreboard.paper import account_statistics, advance_ledger

    forecasts, results = paper_samples()
    ledger = tickets(advance_ledger(forecasts, results, [], NOW), "B")[:2]
    for row, stake in zip(ledger, [13.37, 14.29], strict=True):
        row["stake"] = stake
        row["settlement"].update(status="lost", profit=-stake, **{"return": 0})
    summary = account_statistics(ledger, NOW)["1.05"]["B"]
    assert summary["invested"] == summary["settled_stakes"] == 27.66
    assert summary["current_funds"] == summary["available_cash"] == 9972.34
