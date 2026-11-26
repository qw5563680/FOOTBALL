"""Prospective paired scores for the isolated T72 forecast attachment."""

import json
import logging
import math
from collections import Counter
from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from zucai.db import get_engine
from zucai.forecast_archive import _path
from zucai.market_blend import MODEL_NAME as MODEL
from zucai.market_blend import WEIGHT_REVIEW_STEP
from zucai.models import ClosingOdds
from zucai.predict.experiments.metrics import paired, summarize
from zucai.result_source_groups import known_result_groups
from zucai.result_source_links import load_result_links
from zucai.scoreboard.score import instant, outcome, publication_reason, record_key, result_reason

LOG = logging.getLogger(__name__)
SCORES = "scoreboard/team_market_blend.jsonl"
COLUMNS = ("team", "market", "fusion")
METRICS = ("log_loss", "brier", "top_accuracy")
LABELS = {
    "team": "球队模型",
    "market": "市场参考",
    "fusion": "球队＋市场（实验）",
    "closing": "收盘价",
}
MIN_COMMON = WEIGHT_REVIEW_STEP
STOP_COMMON = 300


def marker(row):
    blend = row.get("experimental_blend")
    return isinstance(blend, dict) and blend.get("model_name") == MODEL


def read_scored(archive, summary):
    path = _path(archive, SCORES)
    expected = (summary or {}).get("team_market_blend", {}).get("scored_sha256")
    if not path.exists() and expected is None:
        return [], b""
    if not path.is_file() or expected is None:
        raise ValueError("融合评分 SHA256 缺少证据")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected:
        raise ValueError("融合评分 SHA256 不符")
    if raw and not raw.endswith(b"\n"):
        raise ValueError("融合评分 JSONL 缺少行终止符")
    records = [json.loads(line) for line in raw.splitlines()]
    if len({row["record_id"] for row in records}) != len(records):
        raise ValueError("融合评分含重复比赛/批次")
    return records, raw


def _market_probabilities(prices):
    if any(type(p) not in (int, float) or not math.isfinite(p) or p <= 1 for p in prices):
        return None
    inverse = [1 / p for p in prices]
    return {k: p / sum(inverse) for k, p in zip("hda", inverse, strict=True)}


def read_closing(database, results, matches, at):
    """Publisher prices for the post-match comparison only, joined by verified IDs."""
    if not matches:
        return {}
    engine = get_engine(database)
    try:
        with Session(engine) as session:
            links = load_result_links(session)
            identity = {
                key: {
                    **results[key],
                    "home_team_id": results[key]["home"],
                    "away_team_id": results[key]["away"],
                }
                for left, right, _ in links
                for key in (left, right)
                if key in results
            }
            groups = known_result_groups(identity, links, at)
            targets = {key: groups.get(key, {key}) for key in matches}
            candidates = sorted(set().union(*targets.values()))
            prices = {}
            priority = {"B365": 0, "PS": 1, "Avg": 2}
            for offset in range(0, len(candidates), 750):
                query = select(ClosingOdds).where(
                    ClosingOdds.match_id.in_(candidates[offset : offset + 750]),
                    ClosingOdds.pool == "had",
                    ClosingOdds.basis == "publisher_closing",
                    ClosingOdds.bookmaker.in_(priority),
                )
                for row in session.scalars(query):
                    probabilities = _market_probabilities((row.h, row.d, row.a))
                    current = prices.get(row.match_id)
                    if probabilities and (not current or priority[row.bookmaker] < current[0]):
                        prices[row.match_id] = (
                            priority[row.bookmaker],
                            probabilities,
                            row.bookmaker,
                        )
    finally:
        engine.dispose()
    found = {}
    for key, group in targets.items():
        choices = sorted(group & prices.keys())
        if len(choices) == 1:
            match = choices[0]
            _, values, bookmaker = prices[match]
            found[key] = dict(probabilities=values, source_match_id=match, bookmaker=bookmaker)
    return found


def _latest(forecasts, at):
    found, omitted = {}, Counter()
    for row in forecasts:
        if not marker(row) or row.get("mode") != "prematch" or row.get("sfc_only"):
            continue
        if row.get("source") != "jc":
            omitted["fixture_identity_unverified"] += 1
            continue
        try:
            cutoff, generated, kickoff = map(
                instant, (row["as_of_utc"], row["generated_utc"], row["kickoff_utc"])
            )
            if not cutoff <= generated < kickoff:
                omitted["invalid_forecast_clock"] += 1
                continue
            reason = publication_reason(row, at)
            if not reason and instant(row["published_utc"]) < generated:
                reason = "publication_before_generation"
            if reason:
                omitted[reason] += 1
                continue
        except (TypeError, KeyError, ValueError, OverflowError):
            omitted["invalid_forecast_clock"] += 1
            continue
        order = (generated, row["batch_id"])
        mid = row["match_id"]
        if mid not in found or found[mid][0] < order:
            found[mid] = (order, row)
    return (found[mid][1] for mid in sorted(found)), omitted


def _views(row):
    from zucai.market_blend import eligible_market, validate_frozen_blend

    team = (
        row.get("model_probabilities")
        if row.get("status") == "predicted" and row.get("model_name") == "dixon-coles"
        else None
    )
    market = eligible_market(row, row["as_of_utc"])
    blend = row["experimental_blend"]
    try:
        validate_frozen_blend(row, row["as_of_utc"])
        fusion = blend.get("probabilities") if blend["status"] == "generated" else None
        reason = blend.get("reason") or "融合未生成"
    except (ValueError, TypeError, KeyError, OverflowError):
        fusion, reason = None, "融合冻结证据不符"
    return dict(
        team=(team, row.get("reason") or "球队模型未生成"),
        market=(
            market and market["probabilities"],
            row.get("market_missing_reason") or "市场快照不合格",
        ),
        fusion=(fusion, reason),
    )


def _pair_row(row, closing=None):
    variants = {k: dict(probabilities=[row[k][outcome] for outcome in "hda"]) for k in COLUMNS}
    if closing:
        variants["closing"] = dict(probabilities=[closing[outcome] for outcome in "hda"])
    return dict(
        kickoff_utc=row["kickoff_utc"],
        actual="hda".index(row["actual"]),
        variants=variants,
    )


def _metrics(rows, column):
    values = summarize(rows, column)
    result = {key: values[key] for key in ("n", *METRICS)}
    if result["log_loss"] is not None and not math.isfinite(result["log_loss"]):
        result["log_loss"] = "Infinity"
    return result


def _common(rows):
    return [row for row in rows if all(row[k] is not None for k in COLUMNS)]


def comparison(rows, *, closing=None):
    common = _common(rows)
    paired_rows = [_pair_row(row) for row in common]
    with_closing = []
    closing_evidence = []
    for row in common:
        ref = (closing or {}).get(row["match_id"])
        if ref:
            with_closing.append(_pair_row(row, ref.get("probabilities", ref)))
            closing_evidence.append({"match_id": row["match_id"], **ref})
    return dict(
        common_n=len(common),
        common={key: _metrics(paired_rows, key) for key in COLUMNS},
        paired={key: paired(paired_rows, "fusion", key) for key in ("team", "market")},
        closing=dict(
            label="赛后对照",
            common_n=len(with_closing),
            common={key: _metrics(with_closing, key) for key in (*COLUMNS, "closing")},
            evidence=closing_evidence,
        ),
    )


def decision(n, differences):
    result = dict(team=None, market=None, market_note=None, stop_required=False, stop_reasons=[])
    if n < MIN_COMMON:
        return result
    team, market = [
        (differences[key].get("log_loss") or {}).get("interval") for key in ("team", "market")
    ]
    result.update(
        team="市场信息帮到了球队模型" if team and team[1] < 0 else "尚无证据",
        market="融合比直接看市场有额外好处" if market and market[1] < 0 else "尚无证据",
        market_note="与市场参考相当，无额外好处"
        if market and market[0] <= 0 <= market[1]
        else None,
    )
    if n >= STOP_COMMON:
        if team and team[1] >= 0:
            result["team"] = "市场信息没有帮到球队模型"
            result["stop_reasons"].append(result["team"])
        if market and market[0] > 0:
            result["stop_reasons"].append("融合明显不如市场")
        result["stop_required"] = bool(result["stop_reasons"])
    return result


def _checkpoint_document(rows):
    paired_rows = [_pair_row(row) for row in rows]
    paired_view = {key: paired(paired_rows, "fusion", key) for key in ("team", "market")}
    return dict(
        record_ids=[row["record_id"] for row in rows],
        paired=paired_view,
        decision=decision(len(rows), paired_view),
    )


def checkpoint(rows, previous, *, active=None):
    """Leave the first evaluated 300-match point unchanged on later runs."""
    if previous is not None:
        index = {row["record_id"]: row for row in rows}
        ids = previous.get("record_ids", [])
        if len(ids) != STOP_COMMON or len(set(ids)) != STOP_COMMON or not set(ids) <= index.keys():
            raise ValueError("融合 300 场检查点的评分证据不完整")
        evidence = [index[key] for key in ids]
        if (
            len(_common(evidence)) != STOP_COMMON
            or len({row["match_id"] for row in evidence}) != STOP_COMMON
            or _checkpoint_document(evidence) != previous
        ):
            raise ValueError("融合 300 场检查点与评分证据不符")
        return previous
    common = _common(rows if active is None else active)
    if len(common) < STOP_COMMON:
        return None
    first = sorted(
        common,
        key=lambda row: (
            instant(row["available_utc"]),
            instant(row["kickoff_utc"]),
            row["match_id"],
        ),
    )[:STOP_COMMON]
    return _checkpoint_document(first)


def read_stop(archive, cutoff, model_version):
    """Verify the previous published score proof before stopping all versions of the line."""
    if not isinstance(model_version, str) or not model_version:
        raise ValueError("融合权重版本无效")
    path = _path(archive, "scoreboard/summary.json")
    previous = json.loads(path.read_bytes()) if path.is_file() else {}
    rows, _ = read_scored(archive, previous)
    score_state = previous.get("team_market_blend", {})
    if score_state.get("status") == "unavailable":
        raise ValueError("融合计分摘要暂不可用")
    frozen = score_state.get("checkpoint_300")
    if frozen is None:
        if score_state.get("common_n", 0) >= STOP_COMMON:
            raise ValueError("融合 300 场检查点尚未核实")
        return False
    if not instant(previous["as_of_utc"]) <= instant(previous["generated_utc"]) <= instant(cutoff):
        raise ValueError("融合记分摘要晚于预测截点")
    return checkpoint(rows, frozen)["decision"]["stop_required"]


def score(forecasts, results, database, previous, existing, at, *, replay_days=None):
    latest, publication_issues = _latest(forecasts, at)
    issues = Counter()
    missing = {key: Counter() for key in COLUMNS}
    saved = {row["record_id"]: row for row in existing}
    added, active, corrections = [], [], []
    total = 0
    for row in latest:
        total += 1
        mid = row["match_id"]
        views = _views(row)
        for key, (probability, reason) in views.items():
            if probability is None:
                missing[key][reason] += 1
        result = results.get(mid)
        why = result_reason(row, result, at)
        if not why and instant(result["available_utc"]) <= instant(row["kickoff_utc"]):
            why = "result_not_after_kickoff"
        if why:
            issues[why] += 1
            continue
        key = record_key(MODEL, mid, row["batch_id"])
        item = saved.get(key)
        goals = {k: result[k] for k in ("home_goals", "away_goals")}
        if item is not None:
            if item["forecast_sha256"] != row["forecast_sha256"]:
                raise ValueError("融合评分与预测原件 SHA256 不符")
            if any(item[k] != v for k, v in goals.items()):
                corrections.append(mid)
                continue
        else:
            if replay_days is not None and instant(row["generated_utc"]) < at - timedelta(
                days=replay_days
            ):
                issues["outside_replay_days"] += 1
                continue
            blend = row["experimental_blend"]
            item = {
                **{
                    key: row[key]
                    for key in (
                        "match_id",
                        "batch_id",
                        "as_of_utc",
                        "generated_utc",
                        "published_utc",
                        "kickoff_utc",
                        "forecast_path",
                        "forecast_sha256",
                        "league",
                        "home",
                        "away",
                    )
                },
                **goals,
                **{name: value[0] for name, value in views.items()},
                "record_id": key,
                "model_version": blend.get("model_version") if views["fusion"][0] else None,
                "protocol_sha256": blend.get("protocol_sha256") if views["fusion"][0] else None,
                "market_snapshot_id": blend.get("market_snapshot_id")
                if views["fusion"][0]
                else None,
                "market_collected_utc": blend.get("market_collected_utc")
                if views["fusion"][0]
                else None,
                "available_utc": result["available_utc"],
                "available_source": result["available_source"],
                "actual": outcome(result),
            }
            added.append(item)
        active.append(item)
    common = _common(active)
    try:
        closing = read_closing(database, results, {row["match_id"] for row in common}, at)
        closing_unavailable = None
    except (ValueError, OSError, SQLAlchemyError):
        LOG.warning("融合赛后收盘价证据不可用", exc_info=True)
        closing, closing_unavailable = {}, "赛后收盘价证据核验失败"
    report = comparison(active, closing=closing)
    report["closing"]["unavailable_reason"] = closing_unavailable
    point = checkpoint(existing + added, (previous or {}).get("checkpoint_300"), active=active)
    verdict = decision(len(common), report["paired"])
    # The terminal decision stays latched even as the current cohort grows or changes.
    verdict["stop_required"] = bool(point and point["decision"]["stop_required"])
    verdict["stop_reasons"] = point["decision"]["stop_reasons"] if point else []
    if verdict["stop_required"]:
        verdict["team"] = point["decision"]["team"]
    report.update(
        coverage={
            key: dict(
                total=total,
                present=total - sum(why.values()),
                missing=sum(why.values()),
                reasons=dict(sorted(why.items())),
            )
            for key, why in missing.items()
        },
        publication_issues=dict(publication_issues),
        result_issues=dict(issues),
        result_corrections=corrections,
        active_keys=[row["record_id"] for row in active],
        common_keys=[row["record_id"] for row in common],
        weight_versions=dict(Counter(row["model_version"] for row in common)),
        checkpoint_300=point,
        last_weight_review_checkpoint=(len(common) // MIN_COMMON) * MIN_COMMON or None,
        next_weight_review_at=(len(common) // MIN_COMMON + 1) * MIN_COMMON,
        decision=verdict,
    )
    return added, report


def _number(value):
    if value is None:
        return "—"
    return f"{value:.4f}" if isinstance(value, (int, float)) else str(value)


def _table(common, columns):
    lines = [
        "| 口径 | 场数 | Log Loss | 三类 Brier | 首选命中率 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for key in columns:
        stats = common[key]
        lines.append(
            f"| {LABELS[key]} | {stats['n']} | "
            + " | ".join(_number(stats[k]) for k in METRICS)
            + " |"
        )
    return lines


def render(summary):
    if summary is None:
        return ""
    lines = ["\n## 球队＋市场（实验）同批比较", ""]
    if summary.get("status") == "unavailable":
        return "\n".join(lines + ["融合计分证据未通过核验，等待修复。", ""])
    lines.extend(["| 口径 | 已发布 | 有结果 | 缺失 | 原因 |", "| --- | ---: | ---: | ---: | --- |"])
    for key in COLUMNS:
        cov = summary["coverage"][key]
        reasons = "、".join(f"{name} {n}" for name, n in cov["reasons"].items()) or "—"
        reasons = reasons.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {LABELS[key]} | {cov['total']} | {cov['present']} | {cov['missing']} | {reasons} |"
        )
    lines.extend(["", f"三栏共同赛后样本 {summary['common_n']} 场。", ""])
    lines.extend(_table(summary["common"], COLUMNS))
    lines.extend(
        [
            "",
            "UTC 周块配对差值和 95% 区间",
            "",
            "| 配对 | 指标 | 差值 | 95% 区间 |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for key in ("team", "market"):
        for metric, label in zip(METRICS, ("Log Loss", "Brier", "首选命中率"), strict=True):
            data = summary["paired"][key][metric] or {}
            interval = data.get("interval")
            bounds = " / ".join(map(_number, interval)) if interval else "—"
            lines.append(
                f"| 融合减{LABELS[key]} | {label} | {_number(data.get('difference'))} | {bounds} |"
            )
    verdict = summary["decision"]
    notes = [verdict[key] for key in ("team", "market", "market_note") if verdict[key]]
    if notes:
        lines.extend(["", "；".join(notes) + "。"])
    if verdict["stop_required"]:
        lines.extend(
            ["", "300 场共同样本检查点触发关停。" + "；".join(verdict["stop_reasons"]) + "。"]
        )
    lines.extend(["", "### 收盘价（赛后对照）", ""])
    lines.extend(_table(summary["closing"]["common"], (*COLUMNS, "closing")))
    return "\n".join([*lines, ""])
