"""Causal virtual decisions from frozen SP, with immutable stakes and separate settlements."""

import hashlib
import json
from collections import defaultdict
from copy import deepcopy
from decimal import ROUND_DOWN, Decimal

from zucai.prediction_disclosure import uses_unknown_team_prior
from zucai.scoreboard.score import (
    OUTCOMES,
    frozen_market,
    instant,
    outcome,
    prematch_rows,
    record_key,
    result_reason,
)
from zucai.sources.sporttery import BEIJING

THRESHOLDS = ("1.00", "1.05", "1.10")  # Thresholds apply to p * SP (return ratio).
ACCOUNTS = {
    "A": "单场固定额",
    "B": "单场四分之一凯利",
    "C": "热门对照组",
    "D": "二串一",
    "E": "三串一",
}
INITIAL = 10000
PAPER_VERSION_FIELDS = (
    "model_definition_id",
    "model_definition",
    "source_variant",
    "paper_eligible",
)


def paper_version_allowed(row, *, required=False):
    """Old freezes remain compatible; any new marker requires complete qualification."""
    if "model_definition_schema" in row:
        if type(row["model_definition_schema"]) is not int or row["model_definition_schema"] != 1:
            return False
        required = True
    if not required and not any(key in row for key in PAPER_VERSION_FIELDS):
        return True
    return (
        all(key in row for key in PAPER_VERSION_FIELDS)
        and isinstance(row.get("model_definition_id"), str)
        and bool(row["model_definition_id"].strip())
        and isinstance(row.get("model_definition"), dict)
        and bool(row["model_definition"])
        and row.get("source_variant") in {"legacy", "current"}
        and row.get("paper_eligible") is True
    )


def paper_context_allowed(context):
    """A whole batch may contain many definitions but cannot declare itself a candidate."""
    return (
        context.get("source_variant") != "expanded"
        and ("paper_eligible" not in context or context["paper_eligible"] is True)
        and (
            "model_definition_schema" not in context
            or (
                type(context["model_definition_schema"]) is int
                and context["model_definition_schema"] == 1
            )
        )
    )


def paper_version_matches(row, model, *, required=False):
    """An eligible row cannot borrow qualification from another frozen definition."""
    return (
        paper_version_allowed(row, required=required)
        and paper_version_allowed(model, required=required)
        and all(row.get(key) == model.get(key) for key in PAPER_VERSION_FIELDS)
    )


def money(value):
    return float(Decimal(str(value)).quantize(Decimal(".01"), rounding=ROUND_DOWN))


def kelly_stake(balance, probability, sp):
    cash, p, odds = map(lambda x: Decimal(str(x)), (balance, probability, sp))
    if odds <= 1 or cash * Decimal(".02") < 10 or p * odds <= 1:
        return None
    fraction = (p * odds - 1) / (odds - 1)
    return money(max(Decimal(10), min(cash * fraction / 4, cash * Decimal(".02"))))


def cash_at(ledger, account, threshold, at):
    balance = Decimal(INITIAL)
    for row in ledger:
        if row["record_type"] != "ticket" or (row["account"], row["threshold"]) != (
            account,
            threshold,
        ):
            continue
        if instant(row["decision_utc"]) <= instant(at):
            balance -= Decimal(str(row["stake"]))
            settlement = row["settlement"]
            if settlement and instant(settlement["settled_utc"]) <= instant(at):
                balance += Decimal(str(settlement["return"]))
    return money(balance)


def decision_digest(row):
    frozen = {k: v for k, v in row.items() if k not in {"settlement", "decision_sha256"}}
    return hashlib.sha256(
        json.dumps(frozen, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _settle(ticket, results, as_of):
    if ticket["settlement"]:
        return
    evidence = []
    for leg in ticket["legs"]:
        result = results.get(leg["match_id"])
        if result_reason(leg, result, as_of):
            return
        evidence.append(
            {
                k: result[k]
                for k in (
                    "match_id",
                    "home_goals",
                    "away_goals",
                    "available_utc",
                    "available_source",
                )
            }
        )
    won = all(
        leg["selection"] == outcome(result)
        for leg, result in zip(ticket["legs"], evidence, strict=True)
    )
    returned = Decimal(str(ticket["stake"])) if won else Decimal(0)
    if won:
        for leg in ticket["legs"]:
            returned *= Decimal(str(leg["sp"]))
    ticket["settlement"] = {
        "status": "won" if won else "lost",
        "return": money(returned),
        "profit": money(returned - Decimal(str(ticket["stake"]))),
        "settled_utc": max(instant(r["available_utc"]) for r in evidence).isoformat(),
        "results": evidence,
    }


def _leg(row, selection, market):
    p, sp = row["model_probabilities"][selection], market["prices"][selection]
    edge = Decimal(str(p)) * Decimal(str(sp)) - 1
    return {
        **{
            k: row[k]
            for k in (
                "match_id",
                "batch_id",
                "source",
                "league",
                "home",
                "away",
                "kickoff_utc",
                "generated_utc",
                "as_of_utc",
                "model_name",
                "model_version",
            )
        },
        **{key: deepcopy(row[key]) for key in PAPER_VERSION_FIELDS if key in row},
        **(
            {"model_definition_schema": row["model_definition_schema"]}
            if "model_definition_schema" in row
            else {}
        ),
        "forecast_path": row.get("forecast_path"),
        "forecast_sha256": row.get("forecast_sha256"),
        "selection": selection,
        "sp": sp,
        "model_probability": p,
        "edge": float(edge),
        "snapshot_utc": market["snapshot_utc"],
    }


def advance_ledger(forecasts, results, existing, as_of):
    ledger = deepcopy(existing)
    keys = {r["record_id"] for r in ledger}
    if len(keys) != len(ledger):
        raise ValueError("虚拟账本含重复记录")
    for row in ledger:
        if row["record_type"] == "ticket":
            if row.get("decision_sha256") != decision_digest(row):
                raise ValueError("注单的冻结字段已被修改")
            _settle(row, results, as_of)
    batches = defaultdict(list)
    for row in prematch_rows([r for r in forecasts if not r.get("sfc_only")], as_of):
        batches[instant(row["generated_utc"]), row["batch_id"]].append(row)
    watermark = max(
        (
            (instant(r["decision_utc"]), r["batch_id"])
            for r in ledger
            if r["record_type"] == "batch"
        ),
        default=None,
    )

    def skip(account, threshold, batch, scope, reason):
        key = record_key("skip", account, threshold, batch, scope, reason)
        if key not in keys:
            ledger.append(
                {
                    "record_type": "skip",
                    "record_id": key,
                    "account": account,
                    "threshold": threshold,
                    "batch_id": batch,
                    "scope": scope,
                    "reason": reason,
                }
            )
            keys.add(key)

    def place(account, threshold, batch, when, scope, legs):
        key = record_key("ticket", account, threshold, scope)
        if key in keys:
            return
        balance = cash_at(ledger, account, threshold, when)
        stake = (
            kelly_stake(balance, legs[0]["model_probability"], legs[0]["sp"])
            if account == "B"
            else 100
        )
        if stake is None or stake > balance:
            skip(account, threshold, batch, scope, "insufficient_cash")
            return
        ticket = {
            "record_type": "ticket",
            "record_id": key,
            "account": account,
            "threshold": threshold,
            "batch_id": batch,
            "scope": scope,
            "decision_utc": when.isoformat(),
            "created_utc": instant(as_of).isoformat(),
            "replayed": any(instant(leg["kickoff_utc"]) <= instant(as_of) for leg in legs),
            "rule": {
                "A": "fixed_100_max_edge",
                "B": "quarter_kelly_cap_2pct_floor_10",
                "C": "lowest_frozen_sp_fixed_100",
                "D": "top_two_edge_fixed_100",
                "E": "top_three_edge_fixed_100",
            }[account],
            "edge_comparison": "model_probability * frozen_sp > threshold",
            "cash_before": balance,
            "stake": stake,
            "legs": legs,
            "settlement": None,
        }
        ticket["decision_sha256"] = decision_digest(ticket)
        _settle(ticket, results, as_of)
        ledger.append(ticket)
        keys.add(key)

    for (when, batch), rows in sorted(batches.items()):
        marker = record_key("batch", batch)
        if marker in keys:
            continue
        late = watermark is not None and (when, batch) < watermark
        if not late:
            candidates, favourites = [], []
            prior_ids = {r["match_id"] for r in rows if uses_unknown_team_prior(r)}
            for row in rows:
                not_ready = row.get("p2_ready") is not True
                version_blocked = not paper_version_allowed(row)
                market = frozen_market(row)
                # Result data never ranks candidates. A known-before-generation result
                # invalidates that forecast, rather than supplying a selection advantage.
                result = results.get(row["match_id"])
                known_result = (
                    result
                    and result.get("available_utc")
                    and instant(result["available_utc"]) <= when
                )
                if version_blocked or not_ready or not market or known_result:
                    reason = "result_already_known" if known_result else "no_frozen_sp"
                    if not_ready:
                        reason = "model_not_p2_ready"
                    if version_blocked:
                        reason = "model_version_not_paper_eligible"
                    for threshold in THRESHOLDS:
                        for account in ("A", "B", "C"):
                            skip(
                                account,
                                threshold,
                                batch,
                                row["match_id"],
                                reason,
                            )
                    continue
                legs = [_leg(row, k, market) for k in OUTCOMES]
                candidates.append(
                    max(
                        legs,
                        key=lambda leg: (
                            Decimal(str(leg["edge"])),
                            -OUTCOMES.index(leg["selection"]),
                        ),
                    )
                )
                favourites.append(
                    min(legs, key=lambda leg: (leg["sp"], OUTCOMES.index(leg["selection"])))
                )
            candidates.sort(key=lambda leg: (-leg["edge"], leg["match_id"]))
            days = sorted(
                {instant(r["kickoff_utc"]).astimezone(BEIJING).date().isoformat() for r in rows}
            )
            for threshold in THRESHOLDS:
                selected = [
                    leg
                    for leg in candidates
                    if Decimal(str(leg["model_probability"])) * Decimal(str(leg["sp"]))
                    > Decimal(threshold)
                ]
                for account in ("A", "B"):
                    for leg in selected:
                        place(account, threshold, batch, when, leg["match_id"], [leg])
                    if not selected:
                        skip(account, threshold, batch, batch, "no_edge_above_threshold")
                for leg in favourites:
                    place("C", threshold, batch, when, leg["match_id"], [leg])
                for leg in selected:
                    if leg["match_id"] in prior_ids:
                        for account in ("D", "E"):
                            skip(account, threshold, batch, leg["match_id"], "unknown_team_prior")
                for day in days:
                    day_legs = [
                        leg
                        for leg in selected
                        if leg["match_id"] not in prior_ids
                        and instant(leg["kickoff_utc"]).astimezone(BEIJING).date().isoformat()
                        == day
                    ]
                    for account, size in (("D", 2), ("E", 3)):
                        if len(day_legs) >= size:
                            place(account, threshold, batch, when, day, day_legs[:size])
                        elif record_key("ticket", account, threshold, day) not in keys:
                            skip(account, threshold, batch, day, "insufficient_candidates")
        ledger.append(
            {
                "record_type": "batch",
                "record_id": marker,
                "batch_id": batch,
                "decision_utc": when.isoformat(),
                "reason": "late_batch_ignored" if late else "processed",
            }
        )
        keys.add(marker)
    return ledger


def publication_view(forecasts, results, ledger, as_of):
    """Exclude only ineligible frozen tickets; never recreate decisions or settlements."""
    eligible = {(r["match_id"], r["batch_id"]) for r in prematch_rows(forecasts, as_of)}
    excluded = {
        r["record_id"]
        for r in ledger
        if r["record_type"] == "ticket"
        and any((leg.get("match_id"), leg.get("batch_id")) not in eligible for leg in r["legs"])
    }
    return (
        [row for row in ledger if row["record_id"] not in excluded],
        {"excluded_tickets": len(excluded), "filtered_in_memory": bool(excluded)},
    )


# Public statistics entry points live separately from the decision/settlement engine.
from zucai.scoreboard.statistics import account_statistics as account_statistics  # noqa: E402
from zucai.scoreboard.statistics import profit_evidence as profit_evidence  # noqa: E402
from zucai.scoreboard.statistics import robustness as robustness  # noqa: E402
