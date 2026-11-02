"""Deterministic virtual-account returns, settled-equity drawdowns and bootstrap evidence."""

import math
from datetime import datetime, time, timedelta
from decimal import Decimal

import numpy as np

from zucai.scoreboard.score import instant
from zucai.sources.sporttery import BEIJING


def _sum_money(values):
    return sum((Decimal(str(v)) for v in values), Decimal(0))


def robustness(stakes, returns):
    n = len(stakes)
    result = {
        "trimmed_roi": None,
        "roi_ci95": [None, None],
        "bootstrap_samples": 1000,
        "bootstrap_seed": 20261103,
        "trimmed_count": math.ceil(n * 0.05),
    }
    if not n:
        return result
    stakes, returns = np.asarray(stakes, dtype=float), np.asarray(returns, dtype=float)
    keep = np.argsort(-returns, kind="stable")[result["trimmed_count"] :]
    if len(keep):
        result["trimmed_roi"] = float(
            (returns[keep].sum() - stakes[keep].sum()) / stakes[keep].sum()
        )
    random = np.random.default_rng(result["bootstrap_seed"])
    ratios = []
    for _ in range(1000):
        indices = random.integers(0, n, size=n)
        ratios.append(
            float((returns[indices].sum() - stakes[indices].sum()) / stakes[indices].sum())
        )
    result["roi_ci95"] = np.quantile(ratios, [0.025, 0.975]).tolist()
    return result


def profit_evidence(stats):
    return bool(
        stats["settled"] >= 500
        and stats["roi"] is not None
        and stats["roi"] > 0
        and stats["trimmed_roi"] is not None
        and stats["trimmed_roi"] > 0
        and stats["roi_ci95"][0] is not None
        and stats["roi_ci95"][0] > -0.02
    )


def account_statistics(ledger, as_of):
    from zucai.scoreboard.paper import ACCOUNTS, INITIAL, THRESHOLDS, cash_at, money

    report = {}
    for threshold in THRESHOLDS:
        report[threshold] = {}
        for account, name in ACCOUNTS.items():
            tickets = [
                r
                for r in ledger
                if r["record_type"] == "ticket"
                and (r["account"], r["threshold"]) == (account, threshold)
                and instant(r["decision_utc"]) <= instant(as_of)
            ]
            settled = sorted(
                [
                    r
                    for r in tickets
                    if r["settlement"] and instant(r["settlement"]["settled_utc"]) <= instant(as_of)
                ],
                key=lambda r: (instant(r["settlement"]["settled_utc"]), r["record_id"]),
            )
            stakes = [r["stake"] for r in settled]
            returns = [r["settlement"]["return"] for r in settled]
            equity, peak, drawdown, streak, longest = INITIAL, INITIAL, 0, 0, 0
            for row in settled:
                equity = money(_sum_money([equity, row["settlement"]["profit"]]))
                peak = max(peak, equity)
                drawdown = max(drawdown, (peak - equity) / peak)
                streak = streak + 1 if row["settlement"]["profit"] < 0 else 0
                longest = max(longest, streak)
            curve = []
            if tickets:
                start = min(instant(r["decision_utc"]).astimezone(BEIJING).date() for r in tickets)
                start -= timedelta(days=start.weekday())
                while start <= instant(as_of).astimezone(BEIJING).date():
                    end = min(
                        datetime.combine(start + timedelta(days=7), time(), BEIJING)
                        - timedelta(microseconds=1),
                        instant(as_of),
                    )
                    profits = _sum_money(
                        r["settlement"]["profit"]
                        for r in settled
                        if instant(r["settlement"]["settled_utc"]) <= end
                    )
                    curve.append(
                        {
                            "week": start.isoformat(),
                            "equity": money(INITIAL + profits),
                            "cash": cash_at(ledger, account, threshold, end),
                        }
                    )
                    start += timedelta(days=7)
            report[threshold][account] = {
                "name": name,
                "bets": len(tickets),
                "settled": len(settled),
                "pending": len(tickets) - len(settled),
                "replayed_bets": sum(r["replayed"] for r in tickets),
                "hits": sum(r["settlement"]["status"] == "won" for r in settled),
                "invested": money(_sum_money(r["stake"] for r in tickets)),
                "settled_stakes": money(_sum_money(stakes)),
                "returns": money(_sum_money(returns)),
                "roi": (sum(returns) - sum(stakes)) / sum(stakes) if stakes else None,
                "current_funds": equity,
                "available_cash": cash_at(ledger, account, threshold, as_of),
                "max_drawdown": drawdown,
                "longest_losing_streak": longest,
                "weekly_curve": curve,
                **robustness(stakes, returns),
            }
    return report
