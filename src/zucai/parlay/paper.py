"""Independent F ledger: immutable two-ticket decisions and group-level profit accounting."""

import json
from copy import deepcopy
from decimal import Decimal

from zucai.cloud_feeds import digest
from zucai.forecast_archive import _path
from zucai.parlay.single_double import RULE
from zucai.scoreboard.paper import decision_digest
from zucai.scoreboard.score import instant, outcome, record_key, result_reason

LEDGER = "scoreboard/parlay_sd_ledger.jsonl"


def read_ledger(archive, previous=None):
    path = _path(archive, LEDGER)
    if previous is None and (archive / "scoreboard/summary.json").is_file():
        previous = json.loads((archive / "scoreboard/summary.json").read_bytes())
    sha = (previous or {}).get("parlay_sd", {}).get("ledger_sha256")
    if not sha:
        if path.exists():
            raise ValueError("F 账本缺少哈希清单")
        return []
    if not path.is_file() or digest(path) != sha:
        raise ValueError("F 账本 SHA256 不符")
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def _settle(ticket, results, at):
    if ticket["settlement"] is not None:
        return
    evidence = []
    for leg in ticket["legs"]:
        result = results.get(leg["match_id"])
        if result_reason(leg, result, at):
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
    hit = all(
        leg["selection"] == outcome(result)
        for leg, result in zip(ticket["legs"], evidence, strict=True)
    )
    returned = Decimal(str(ticket["winning_return"])) if hit else Decimal(0)
    ticket["settlement"] = dict(
        status="won" if hit else "lost",
        **{"return": float(returned)},
        profit=float(returned - 2),
        results=evidence,
        settled_utc=max(instant(r["available_utc"]) for r in evidence).isoformat(),
    )


def _settle_groups(ledger):
    tickets = {row["record_id"]: row for row in ledger if row["record_type"] == "ticket"}
    for row in ledger:
        if row["record_type"] != "group" or row["settlement"] is not None:
            continue
        parts = [tickets[key] for key in row["ticket_ids"]]
        if any(p["settlement"] is None for p in parts):
            continue
        returned = sum((Decimal(str(p["settlement"]["return"])) for p in parts), Decimal(0))
        profit = returned - 4
        row["settlement"] = dict(
            covered=any(p["settlement"]["status"] == "won" for p in parts),
            single_hit=outcome(parts[0]["settlement"]["results"][0])
            == row["frozen"]["single_selection"],
            status="profit" if profit > 0 else "loss" if profit < 0 else "breakeven",
            **{"return": float(returned)},
            profit=float(profit),
            cost=4,
            settled_utc=max(instant(p["settlement"]["settled_utc"]) for p in parts).isoformat(),
        )


def advance_ledger(documents, results, existing, at):
    ledger = deepcopy(existing)
    keys = {r["record_id"] for r in ledger}
    if len(keys) != len(ledger):
        raise ValueError("F 账本有重复记录")
    for row in ledger:
        if row.get("rule") != RULE or row.get("account") != "F":
            raise ValueError("F 账本混入其他策略")
        if row["record_type"] in {"group", "ticket"}:
            if row.get("decision_sha256") != decision_digest(row):
                raise ValueError("F 冻结注单被修改")
            if row["record_type"] == "ticket":
                _settle(row, results, at)
    for doc in sorted(documents, key=lambda d: (instant(d["generated_utc"]), d["batch_id"])):
        marker = record_key(RULE, "batch", doc["batch_id"])
        if marker in keys or instant(doc["published_utc"]) > instant(at):
            continue
        used_matches, used_teams = set(), set()
        for frozen in doc["groups"]:
            if (
                used_matches.intersection(frozen["match_ids"])
                or used_teams.intersection(frozen["team_ids"])
                or len(set(frozen["team_ids"])) != 4
            ):
                raise ValueError("F 同批组合重复使用比赛或球队")
            used_matches.update(frozen["match_ids"])
            used_teams.update(frozen["team_ids"])
            legs = [frozen["single"], frozen["double"]]
            if any(instant(leg["kickoff_utc"]) <= instant(doc["published_utc"]) for leg in legs):
                continue
            key = record_key(RULE, "group", doc["batch_id"], frozen["group_id"])
            common = dict(
                account="F",
                rule=RULE,
                batch_id=doc["batch_id"],
                group_id=frozen["group_id"],
                decision_utc=doc["generated_utc"],
                created_utc=instant(at).isoformat(),
                parlay_path=doc["path"],
                parlay_sha256=doc["sha256"],
                replayed=any(instant(leg["kickoff_utc"]) <= instant(at) for leg in legs),
                p2_label=frozen["p2_label"],
                p2_ready=frozen["p2_ready"],
                evidence_level=frozen["evidence_level"],
            )
            tickets = []
            for index, beta in enumerate(frozen["double_selections"]):
                ticket_id = record_key(key, "ticket", index)
                selections = [frozen["single_selection"], beta]
                ticket = dict(
                    **common,
                    record_type="ticket",
                    record_id=ticket_id,
                    stake=2,
                    match_ids=frozen["match_ids"],
                    team_ids=frozen["team_ids"],
                    scope=frozen["scope"],
                    probability=frozen["branches"][index]["probability"],
                    probability_exact=frozen["branches"][index]["probability_exact"],
                    winning_return=frozen["branches"][index]["return"],
                    rounding=doc["config"]["rounding"],
                    execution_status="未核实",
                    settlement=None,
                    legs=[
                        {
                            **leg,
                            "generated_utc": doc["generated_utc"],
                            "batch_id": doc["batch_id"],
                            "selection": selection,
                            "sp": leg["market"]["prices"][selection],
                            "snapshot_utc": leg["market"]["snapshot_utc"],
                        }
                        for leg, selection in zip(legs, selections, strict=True)
                    ],
                )
                ticket["decision_sha256"] = decision_digest(ticket)
                _settle(ticket, results, at)
                ledger.append(ticket)
                keys.add(ticket_id)
                tickets.append(ticket_id)
            group = dict(
                **common,
                record_type="group",
                record_id=key,
                frozen=deepcopy(frozen),
                ticket_ids=tickets,
                settlement=None,
            )
            group["decision_sha256"] = decision_digest(group)
            ledger.append(group)
            keys.add(key)
        ledger.append(
            dict(
                account="F",
                rule=RULE,
                record_type="batch",
                record_id=marker,
                batch_id=doc["batch_id"],
                decision_utc=doc["generated_utc"],
                parlay_sha256=doc["sha256"],
            )
        )
        keys.add(marker)
    _settle_groups(ledger)
    return ledger


def _totals(groups, tickets):
    settled = [g for g in groups if g["settlement"]]
    cost = 4 * len(settled)
    profit = sum((Decimal(str(g["settlement"]["profit"])) for g in settled), Decimal(0))
    n = len(settled)
    return dict(
        groups=len(groups),
        settled_groups=n,
        pending_groups=len(groups) - n,
        paper_invested=4 * len(groups),
        settled_cost=cost,
        profit=float(profit),
        roi=float(profit / cost) if cost else None,
        covered_groups=sum(g["settlement"]["covered"] for g in settled),
        coverage_rate=sum(g["settlement"]["covered"] for g in settled) / n if n else None,
        profitable_groups=sum(g["settlement"]["status"] == "profit" for g in settled),
        profit_rate=sum(g["settlement"]["status"] == "profit" for g in settled) / n if n else None,
        breakeven_groups=sum(g["settlement"]["status"] == "breakeven" for g in settled),
        single_hits=sum(g["settlement"]["single_hit"] for g in settled),
        single_hit_rate=sum(g["settlement"]["single_hit"] for g in settled) / n if n else None,
        tickets=len(tickets),
        settled_tickets=sum(t["settlement"] is not None for t in tickets),
        won_tickets=sum(
            bool(t["settlement"] and t["settlement"]["status"] == "won") for t in tickets
        ),
    )


def summarize(ledger, results, at):
    groups = [r for r in ledger if r["record_type"] == "group"]
    tickets = [r for r in ledger if r["record_type"] == "ticket"]
    tiers = {}
    for level in sorted({g["evidence_level"] for g in groups}):
        tiers[level] = {}
        for replayed in (False, True):
            scope = [
                g for g in groups if g["evidence_level"] == level and g["replayed"] == replayed
            ]
            ids = {g["group_id"] for g in scope}
            tiers[level]["replayed" if replayed else "prospective"] = _totals(
                scope, [t for t in tickets if t["group_id"] in ids and t["replayed"] == replayed]
            )
    corrections = sorted(
        {
            r["match_id"]
            for t in tickets
            if t["settlement"]
            for r in t["settlement"]["results"]
            if r["match_id"] in results
            and any(r[k] != results[r["match_id"]][k] for k in ("home_goals", "away_goals"))
        }
    )
    pending = []
    for group in groups:
        if group["settlement"]:
            continue
        legs = [group["frozen"]["single"], group["frozen"]["double"]]
        reasons = [
            result_reason(
                {**leg, "generated_utc": group["decision_utc"]}, results.get(leg["match_id"]), at
            )
            for leg in legs
        ]
        pending.append(
            dict(
                group_id=group["group_id"],
                reasons=reasons,
                phase="upcoming"
                if all(instant(leg["kickoff_utc"]) > instant(at) for leg in legs)
                else "started",
            )
        )
    return dict(
        account="F",
        rule=RULE,
        bet_allowed=False,
        evidence=tiers,
        totals=_totals(groups, tickets),
        pending=pending,
        settlement_corrections=corrections,
        statement="独立纸面账；与 A–E、真实出票业绩分开，盈利严格指整组净收益大于零",
    )


def render(summary):
    if not summary:
        return ""
    levels = {
        "prospective_verified": "前瞻已验证",
        "historical_gate_passed": "历史门槛通过",
        "p2_experimental": "P2 实验",
    }

    def rate(value):
        return "—" if value is None else f"{value:.2%}"

    lines = [
        "\n### F 单选＋双选二串一（独立纸面账）\n",
        "按证据等级与事后重建分列；BET 禁用、可执行性未核实。\n",
        "| 证据 / 口径 | 已结组 | 两注命中数 | 原单选命中率 | 组覆盖率 | 组盈利率 |"
        " 已结投入 | 盈亏 | ROI | 待结组 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for level, modes in summary["evidence"].items():
        for mode, row in modes.items():
            label = "事后重建" if mode == "replayed" else "赛前记账"
            lines.append(
                f"| {levels.get(level, level)} / {label} | {row['settled_groups']} | "
                f"{row['won_tickets']} | {rate(row['single_hit_rate'])} | "
                f"{rate(row['coverage_rate'])} | {rate(row['profit_rate'])} | "
                f"{row['settled_cost']:.2f} | {row['profit']:.2f} | {rate(row['roi'])} | "
                f"{row['pending_groups']} |"
            )
    if not summary["evidence"]:
        lines.append("| 尚无合格纸面组 | 0 | 0 | — | — | — | 0.00 | 0.00 | — | 0 |")
    if summary["settlement_corrections"]:
        lines.append("已结赛果后来变化：保留原结算，需人工核对。")
    return "\n".join(lines) + "\n"
