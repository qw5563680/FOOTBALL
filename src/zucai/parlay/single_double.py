"""Single + double paper groups; probabilities never depend on price or results."""

import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from itertools import combinations, permutations

from zucai.scoreboard.score import OUTCOMES, frozen_market, instant, record_key

RULE = "single_plus_double_v1"
LEVELS = ("prospective_verified", "historical_gate_passed", "p2_experimental")


@dataclass(frozen=True)
class Config:
    single_direction_gap: float = 0.10
    double_fragile_gap: float = 0.05
    max_groups: int = 3
    rounding: str = "ROUND_DOWN"

    def __post_init__(self):
        if type(self.max_groups) is not int or not 1 <= self.max_groups <= 5:
            raise ValueError("max_groups 须为 1–5 的整数")
        for value in (self.single_direction_gap, self.double_fragile_gap):
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("方向差值须在 0–1 之间")
        if self.rounding not in {"ROUND_DOWN", "ROUND_HALF_UP"}:
            raise ValueError("不支持的纸面取位假设")


def decimal(value):
    return Decimal(str(value))


def probabilities(value):
    return (
        isinstance(value, dict)
        and set(value) == set(OUTCOMES)
        and all(
            type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in value.values()
        )
        and abs(sum(value.values()) - 1) <= 1e-8
    )


def ranking(value):
    return sorted(OUTCOMES, key=lambda key: (-value[key], OUTCOMES.index(key)))


def cashflow(single_probability, double_probabilities, single_sp, double_sp, *, config=None):
    config = config or Config()
    if len(double_probabilities) != 2 or len(double_sp) != 2:
        raise ValueError("双选必须有两项")
    ps = [decimal(single_probability), *map(decimal, double_probabilities)]
    prices = [decimal(single_sp), *map(decimal, double_sp)]
    if any(not p.is_finite() or not 0 <= p <= 1 for p in ps) or sum(ps[1:]) > 1:
        raise ValueError("分支概率无效")
    if any(not p.is_finite() or p <= 1 for p in prices):
        raise ValueError("SP 无效")
    q = [ps[0] * p for p in ps[1:]]
    rounding = {"ROUND_DOWN": ROUND_DOWN, "ROUND_HALF_UP": ROUND_HALF_UP}[config.rounding]
    returns = [
        (2 * prices[0] * price).quantize(Decimal(".01"), rounding=rounding) for price in prices[1:]
    ]
    branches = []
    for name, probability, returned in zip(
        ("first_wins", "second_wins", "neither_wins"),
        [*q, 1 - sum(q)],
        [*returns, Decimal(0)],
        strict=True,
    ):
        profit = returned - 4
        branches.append(
            dict(
                branch=name,
                probability=float(probability),
                probability_exact=str(probability),
                cost=4,
                **{"return": float(returned)},
                profit=float(profit),
                profit_status="profit" if profit > 0 else "loss" if profit < 0 else "breakeven",
            )
        )
    profitable = sum((p for p, g in zip(q, returns, strict=True) if g > 4), Decimal(0))
    even = sum((p for p, g in zip(q, returns, strict=True) if g == 4), Decimal(0))
    ev = sum((p * g for p, g in zip(q, returns, strict=True)), Decimal(0)) - 4
    return dict(
        branches=branches,
        stake_per_ticket=2,
        cost=4,
        coverage_probability=float(sum(q)),
        coverage_probability_exact=str(sum(q)),
        profit_probability=float(profitable),
        profit_probability_exact=str(profitable),
        breakeven_probability=float(even),
        expected_profit=float(ev),
        roi=float(ev / 4),
        probability_basis="跨场独立近似",
        rounding=config.rounding,
        rounding_status="未核实",
        tax_basis="税前纸面测算；未计税费、封顶等现实规则，未核实",
    )


def double_options(p, *, gap=0.05):
    if not probabilities(p):
        return []
    rows = []
    for selected in combinations(OUTCOMES, 2):
        omitted = next(k for k in OUTCOMES if k not in selected)
        coverage = sum((decimal(p[k]) for k in selected), Decimal(0))
        rows.append(
            dict(
                selections=list(selected),
                omitted=omitted,
                omitted_probability=p[omitted],
                coverage_probability=float(coverage),
                coverage_probability_exact=str(coverage),
                fragile=decimal(p[omitted]) >= min(decimal(p[k]) for k in selected) - decimal(gap),
            )
        )
    return rows


def eligibility(row, identity, catalog, at):
    reasons = []
    try:
        same_kickoff = bool(identity) and instant(identity.get("kickoff_utc")) == instant(
            row.get("kickoff_utc")
        )
    except (ValueError, TypeError):
        same_kickoff = False
    if row.get("source") != "jc" or not row.get("match_id", "").startswith("jc:"):
        reasons.append("not_jc")
    if (
        not identity
        or any(identity.get(key) != row.get(key) for key in ("source", "league", "home", "away"))
        or not same_kickoff
        or any(type(identity.get(s + "_team_id")) is not int for s in ("home", "away"))
    ):
        reasons.append("identity_conflict")
    elif identity["home_team_id"] == identity["away_team_id"]:
        reasons.append("identity_conflict")
    if identity and identity.get("status") != "scheduled":
        reasons.append("match_not_scheduled")
    try:
        started = instant(row.get("kickoff_utc")) <= instant(at)
    except (ValueError, TypeError):
        started = True
    if started:
        reasons.append("already_started")
    if reasons:
        return dict(status="excluded", reasons=reasons)
    if catalog is None:
        return dict(status="reference", reasons=["sale_qualification_unknown"])
    if catalog.get("matchStatus") != "Selling" or str(catalog.get("sellStatus")) != "1":
        return dict(status="excluded", reasons=["not_selling"])
    pools = catalog.get("poolList")
    try:
        pools = json.loads(pools) if isinstance(pools, str) else pools
    except (ValueError, TypeError):
        pools = None
    had = (
        [p for p in pools if isinstance(p, dict) and str(p.get("poolCode", "")).lower() == "had"]
        if isinstance(pools, list)
        else []
    )
    if len(had) != 1 or any(k not in had[0] for k in ("poolOddsType", "poolStatus", "allUp")):
        return dict(status="reference", reasons=["sale_qualification_unknown"])
    if (
        had[0]["poolOddsType"] != "F"
        or had[0]["poolStatus"] != "Selling"
        or str(had[0]["allUp"]) != "1"
    ):
        return dict(status="excluded", reasons=["had_parlay_not_supported"])
    return dict(status="eligible", reasons=[])


def injury_reasons(injury, cutoff):
    reasons = []
    for side in ("home", "away"):
        cell = (injury or {}).get(side, {})
        value = cell.get("confirmed_injured_out")
        if type(value) is not int or value < 0 or cell.get("missing_reason"):
            reasons.append(f"{side}:injuries_unknown")
        elif not cell.get("captured_utc") or instant(cell["captured_utc"]) >= instant(cutoff):
            reasons.append(f"{side}:injuries_not_known_before_cutoff")
    return reasons


def _market(row, batch):
    market = row.get("market") or {}
    if (
        market.get("source") != "stored_had_snapshot"
        or market.get("pool", "had") != "had"
        or market.get("line", 0) != 0
    ):
        return None
    try:
        return frozen_market({**row, **{k: batch[k] for k in ("as_of_utc", "generated_utc")}})
    except (KeyError, TypeError, ValueError):
        return None


def _level(row):
    return (
        LEVELS[0]
        if row.get("prospective_verified") is True
        else LEVELS[1]
        if row.get("p2_ready") is True
        else LEVELS[2]
    )


def _prepare(row, p, batch, identity, injury, catalog, at, config, *, independent=(), kind="TEAM"):
    order = ranking(p)
    gap = decimal(p[order[0]]) - decimal(p[order[1]])
    single_reasons = []
    if gap == 0 or gap < decimal(config.single_direction_gap):
        single_reasons.append("single_direction_fragile")
    if any(
        ranking(other)[0] != order[0] or other[ranking(other)[0]] == other[ranking(other)[1]]
        for other in independent
    ):
        single_reasons.append("independent_direction_disagrees")
    if kind != "MARKET":
        single_reasons.extend(injury_reasons(injury, batch["as_of_utc"]))
    market = _market(row, batch)
    if market is None:
        single_reasons.append("no_frozen_had_sp")
    elif decimal(market["prices"][order[0]]) < Decimal("1.20"):
        single_reasons.append("single_sp_below_1_20")
    options = double_options(p, gap=config.double_fragile_gap)
    best = max(options, key=lambda d: decimal(d["coverage_probability_exact"]))
    best = dict(best, eligible=not best["fragile"] and market is not None)
    return dict(
        **{
            k: row.get(k)
            for k in (
                "match_id",
                "source",
                "league",
                "home",
                "away",
                "kickoff_utc",
                "model_name",
                "model_version",
                "p2_label",
                "p2_ready",
                "prospective_verified",
            )
        },
        team_ids=[(identity or {}).get(s + "_team_id") for s in ("home", "away")],
        probabilities=dict(p),
        probability_source=kind,
        original_direction=order[0],
        model_probabilities=dict(p) if kind != "MARKET" else None,
        market=market,
        catalog=catalog,
        injuries=injury if kind != "MARKET" else None,
        eligibility=eligibility(row, identity, catalog, at),
        single=dict(
            selection=order[0],
            eligible=not single_reasons,
            reasons=single_reasons,
            direction_gap=float(gap),
            price_status="继续审核"
            if market and market["prices"][order[0]] == 1.2
            else "价格下限通过"
            if market and market["prices"][order[0]] > 1.2
            else "未通过",
        ),
        double_options=options,
        double=best,
        support=["最高项来自该版本冻结概率", f"第一减第二 {float(gap):.8g}"],
        counterevidence=single_reasons,
        direction_change_conditions=["阵容、伤停或赛程新证据可能改变判断；本版本不主观改概率"],
        independent_models_available=len(independent),
    )


def _group(a, b, alpha, pair, batch, board, config):
    flow = cashflow(
        a["probabilities"][alpha],
        [b["probabilities"][k] for k in pair],
        a["market"]["prices"][alpha],
        [b["market"]["prices"][k] for k in pair],
        config=config,
    )
    return dict(
        group_id=record_key(RULE, batch, board, a["match_id"], b["match_id"], alpha, pair),
        batch_id=batch,
        rule=RULE,
        board=board,
        evidence_level=board,
        p2_label=a.get("p2_label") or "P2 实验",
        p2_ready=a.get("p2_ready") is True,
        match_ids=[a["match_id"], b["match_id"]],
        team_ids=a["team_ids"] + b["team_ids"],
        single=a,
        double=b,
        single_selection=alpha,
        double_selections=list(pair),
        omitted=b["double"]["omitted"],
        omitted_probability=b["double"]["omitted_probability"],
        execution_status="未核实",
        action="PASS（仅纸面记录）",
        bet_allowed=False,
        gaps=[
            "官方停售与共同出票窗口未核实",
            "四队正式首发未核实",
            "取位、税费与奖金封顶未核实",
            "跨场独立仅为近似",
        ],
        shared_risks=["共同模型误差或未覆盖的阵容变化可能同时击穿；未建立跨组联合概率"],
        **flow,
    )


def _order(group, metric="coverage_probability_exact"):
    return (
        -decimal(group[metric]),
        *group["match_ids"],
        OUTCOMES.index(group["single_selection"]),
        tuple(OUTCOMES.index(k) for k in group["double_selections"]),
    )


def greedy(candidates, limit, *, occupied_matches=None, occupied_teams=None):
    matches = occupied_matches if occupied_matches is not None else set()
    teams = occupied_teams if occupied_teams is not None else set()
    selected = []
    for group in sorted(candidates, key=_order):
        if len(selected) >= limit:
            break
        if matches.intersection(group["match_ids"]) or teams.intersection(group["team_ids"]):
            continue
        selected.append(group)
        matches.update(group["match_ids"])
        teams.update(group["team_ids"])
    return selected


def _board(matches, batch, name, config):
    candidates, references, rejected = [], [], Counter()
    for a, b in permutations(matches, 2):
        for alpha in OUTCOMES:
            for pair in combinations(OUTCOMES, 2):
                reason = None
                if alpha != a["single"]["selection"] or not a["single"]["eligible"]:
                    reason = "single_ineligible"
                elif list(pair) != b["double"]["selections"] or b["market"] is None:
                    reason = "double_ineligible"
                elif any(r["eligibility"]["status"] == "excluded" for r in (a, b)):
                    reason = "match_ineligible"
                elif len(set(a["team_ids"] + b["team_ids"])) != 4:
                    reason = "repeated_team"
                if reason:
                    rejected[reason] += 1
                    continue
                group = _group(a, b, alpha, pair, batch, name, config)
                if b["double"]["fragile"] or any(
                    r["eligibility"]["status"] == "reference" for r in (a, b)
                ):
                    group["reference_reasons"] = (
                        ["double_direction_fragile"] if b["double"]["fragile"] else []
                    ) + [reason for r in (a, b) for reason in r["eligibility"]["reasons"]]
                    references.append(group)
                else:
                    candidates.append(group)
    candidates.sort(key=_order)
    scope = dict(
        matches=len(matches),
        enumerated=9 * len(matches) * (len(matches) - 1),
        eligible=len(candidates),
        reference_only=len(references),
        rejected=dict(rejected),
    )
    for group in candidates + references:
        group["scope"] = scope
    return dict(
        name=name,
        matches=matches,
        candidates=candidates,
        groups=greedy(candidates, config.max_groups),
        reference_groups=greedy(references, config.max_groups),
        scope=scope,
        highest_profit=min(candidates, key=lambda g: _order(g, "profit_probability_exact"))
        if candidates
        else None,
    )


def build(batch, identities, injuries, catalog, frozen_utc, *, experimental=None, config=None):
    config = config or Config()
    at = instant(frozen_utc)
    if (
        batch.get("mode") != "prematch"
        or not instant(batch["as_of_utc"]) <= instant(batch["generated_utc"]) <= at
    ):
        raise ValueError("候选须来自已经冻结的同一赛前批次")
    source_rows = batch["predictions"]
    if len({r["match_id"] for r in source_rows}) != len(source_rows):
        raise ValueError("同批比赛重复或身份冲突")
    catalog_by_id = {}
    for row in sorted(catalog or [], key=lambda r: instant(r["captured_utc"])):
        if instant(row["captured_utc"]) <= instant(batch["as_of_utc"]):
            catalog_by_id[row["match_id"]] = row
    extras = {}
    originals = {row["match_id"]: row for row in source_rows}
    if (
        experimental
        and experimental.get("batch_id") == batch["batch_id"]
        and experimental.get("generated_utc")
        and experimental.get("as_of_utc")
        and instant(experimental["generated_utc"]) <= at
        and instant(experimental["as_of_utc"]) <= instant(batch["as_of_utc"])
    ):
        for row in experimental.get("predictions", []):
            if (
                row.get("status") == "predicted"
                and row.get("model_name")
                and row.get("model_version")
                and probabilities(row.get("model_probabilities"))
                and all(
                    row.get(k) == originals.get(row.get("match_id"), {}).get(k)
                    for k in ("source", "league", "home", "away", "kickoff_utc")
                )
            ):
                same = extras.setdefault(row["match_id"], [])
                if any(r["model_name"] == row["model_name"] for r in same):
                    raise ValueError("同场独立模型重复，不能选择有利版本")
                same.append(row)
    tiers, experiment_matches, market_matches, diagnostics = {k: [] for k in LEVELS}, {}, [], []
    for row in sorted(source_rows, key=lambda r: r["match_id"]):
        mid = row["match_id"]
        if row.get("source") != "jc" or not row.get("kickoff_utc"):
            diagnostics.append(dict(match_id=mid, reasons=["not_jc_or_missing_kickoff"]))
            continue
        arguments = (
            batch,
            identities.get(mid),
            injuries.get(mid),
            catalog_by_id.get(mid),
            at,
            config,
        )
        p = row.get("model_probabilities")
        valid = (
            row.get("status") == "predicted"
            and probabilities(p)
            and batch.get("input", {}).get("market_weight", 0) == 0
            and row.get("model_version")
        )
        if valid:
            prepared = _prepare(
                row,
                p,
                *arguments,
                independent=[r["model_probabilities"] for r in extras.get(mid, [])],
            )
            tiers[_level(row)].append(prepared)
            reasons = prepared["single"]["reasons"] + prepared["eligibility"]["reasons"]
            if prepared["double"]["fragile"]:
                reasons.append("double_direction_fragile")
            if reasons:
                diagnostics.append(dict(match_id=mid, reasons=reasons))
        else:
            diagnostics.append(dict(match_id=mid, reasons=["model_probabilities_unavailable"]))
        market = _market(row, batch)
        if market:
            market_row = {
                **row,
                "model_name": "market_inverse_sp",
                "model_version": "inverse_sp_v1",
                "p2_label": "市场参考",
                "p2_ready": False,
                "prospective_verified": False,
            }
            market_matches.append(
                _prepare(market_row, market["probabilities"], *arguments, kind="MARKET")
            )
        for extra in extras.get(mid, []):
            name = extra["model_name"]
            item = {
                **row,
                **{k: extra.get(k) for k in ("model_name", "model_version", "model_probabilities")},
                "p2_ready": False,
                "p2_label": "P2 实验",
            }
            experiment_matches.setdefault(name, []).append(
                _prepare(
                    item,
                    extra["model_probabilities"],
                    *arguments,
                    independent=[p] if valid else [],
                    kind="EXPERIMENTAL",
                )
            )
    boards = [_board(rows, batch["batch_id"], name, config) for name, rows in tiers.items() if rows]
    groups, matches, teams = [], set(), set()
    for board in boards:
        groups.extend(
            greedy(
                board["candidates"],
                config.max_groups - len(groups),
                occupied_matches=matches,
                occupied_teams=teams,
            )
        )
        board["groups"] = [g for g in groups if g["board"] == board["name"]]
    experiments = [
        dict(_board(rows, batch["batch_id"], name, config), model_name=name)
        for name, rows in sorted(experiment_matches.items())
    ]
    market = _board(market_matches, batch["batch_id"], "market_reference", config)
    for board in [*boards, *experiments, market]:
        board["highest_coverage"] = board["candidates"][0] if board["candidates"] else None
        del board["candidates"]
    return dict(
        schema_version=1,
        rule=RULE,
        batch_id=batch["batch_id"],
        generated_utc=at.isoformat(),
        as_of_utc=batch["as_of_utc"],
        config=asdict(config),
        bet_allowed=False,
        execution_status="未核实",
        grouping_method="同证据等级按原精度覆盖排序、贪心选取；跨等级按前瞻/历史/P2实验顺序共用占用，不称全局最优",
        groups=groups,
        boards=boards,
        experimental_boards=experiments,
        market_reference=market,
        diagnostics=diagnostics,
        cross_board_notice="实验与市场榜仅供对照，重合不算新增分散机会；不计算至少中一组概率",
    )
