"""SFC coverage calculations from a hash-verified T06 batch, never page odds."""

import copy
import csv
import hashlib
import io
import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, SfcIssue, Team
from zucai.predict.base import model_sources
from zucai.predict.pooled import model_targets
from zucai.prediction_disclosure import (
    prior_disclosure,
    prior_export_label,
    prior_footnote,
    prior_label,
    uses_unknown_team_prior,
)
from zucai.report.render import (
    _beijing,
    _csv_cell,
    _json_bytes,
    _md,
    _percent,
    _timestamp,
    _triplet,
    _validate_batch,
    load_batch,
)
from zucai.scoreboard.paper import (
    PAPER_VERSION_FIELDS,
    paper_context_allowed,
    paper_version_allowed,
    paper_version_matches,
)
from zucai.sfc_publication import PUBLICATION_FIELDS, deadline_message
from zucai.sources.sfc import refresh_snapshot

LOG = logging.getLogger(__name__)
OPTIONS = {"h": "3", "d": "1", "a": "0"}


def _unmapped_reason(row, mapping_reason):
    """Describe mapping evidence only; missing a binding does not prove sale status."""
    basis = (row.get("mapping_evidence") or {}).get("basis")
    if basis == "conflicting_identity" or mapping_reason == "conflicting_identity":
        return "比赛身份依据冲突，待核实"
    if mapping_reason == "ambiguous":
        return "存在多个候选比赛，待核实"
    reasons = []
    if row.get("league") == "友谊赛":
        reasons.append("赛事不在采集范围（友谊赛）")
    if row.get("kickoff_basis") == "placeholder":
        reasons.append("开球时间为占位，待核实")
    if basis == "national_team_name_missing":
        reasons.append("队名对照表缺该队")
    if reasons:
        return "；".join(reasons)
    if basis == "national_team_ids_and_exact_kickoff":
        return "国家队编号与开球时间未找到唯一比赛，待核实"
    if basis == "provider_fixture_and_team_ids":
        return "已有来源编号，待场次桥接（竞彩开售状态未核实）"
    if basis == "exact_team_names":
        return "队名精确匹配未找到对应比赛，待场次桥接（竞彩开售状态未核实）"
    if mapping_reason not in {"unmatched", "matched", None}:
        return mapping_reason
    return "尚无已核验的比赛对应关系"


def _budget(max_lines):
    if type(max_lines) is not int or not 1 <= max_lines <= 3**14:
        raise ValueError("注数上限须为1至4782969的整数")


def optimize_cover(probabilities: list[dict], max_lines: int = 64) -> dict:
    """Exact optimum over rectangular 1/2/3-option tickets, under independence."""
    _budget(max_lines)
    if not 1 <= len(probabilities) <= 14:
        raise ValueError("须提供1至14场概率")
    states = {1: (0.0, [])}
    for probs in probabilities:
        _triplet(probs, "球队模型", probabilities=True)
        order = sorted("hda", key=lambda key: -probs[key])
        updated = {}
        for count, (logp, picks) in states.items():
            for size in (1, 2, 3):
                total = count * size
                if total > max_lines:
                    continue
                options = order[:size]
                covered = min(1.0, sum(probs[k] for k in options))
                candidate = logp + math.log(covered)
                if total not in updated or candidate > updated[total][0]:
                    updated[total] = (candidate, [*picks, options])
        states = updated
    lines, (logp, picks) = max(states.items(), key=lambda item: (item[1][0], -item[0]))
    return {
        "lines": lines,
        "max_lines": max_lines,
        "coverage_probability": math.exp(logp),
        "selections": picks,
        "assumption": "cross_match_independence",
        "scope": "rectangular_full_cover",
        "covers": "all_14_results",
    }


def build_sfc_report(
    session, issue, batch, *, forecast_sha256, generated, max_lines=64, allow_missing=False
):
    _budget(max_lines)
    _validate_batch(batch)
    if not issue.snapshot or len(issue.snapshot) != 14 or not issue.observed_utc:
        raise ValueError("期次缺少完整来源快照，请重新抓取")
    rows = sorted(
        refresh_snapshot(session, issue) if allow_missing else copy.deepcopy(issue.snapshot),
        key=lambda r: r["seq"],
    )
    keys = [r["match_id"] for r in rows if r["match_id"] is not None]
    if (
        [r["seq"] for r in rows] != list(range(1, 15))
        or len(set(keys)) != len(keys)
        or (not allow_missing and len(keys) != 14)
    ):
        raise ValueError("期次序号或比赛ID重复")
    if generated.utcoffset() is None or _timestamp(issue.observed_utc) > generated:
        raise ValueError("报告时间早于期次采集时间或没有时区")
    predictions = {p["match_id"]: p for p in batch["predictions"]}
    if len(predictions) != len(batch["predictions"]):
        raise ValueError("冻结批次包含重复比赛ID")
    as_of, forecast_time = _timestamp(batch["as_of_utc"]), _timestamp(batch["generated_utc"])
    models = {
        (m["model_version"], source, league): m
        for m in batch["models"]
        for source in model_sources(m)
        for league in model_targets(m)
        if not m.get("pooled") or source == "jc"
    }
    for row in rows:
        mapping_reason = row.get("reason") or row.get("mapping", "unmatched")
        row.update(probabilities=None, reason=None, top_outcome=None, selection=None)
        predicted = predictions.get(row["match_id"])
        if predicted:
            row.update(
                {
                    key: copy.deepcopy(predicted[key])
                    for key in PAPER_VERSION_FIELDS
                    if key in predicted
                }
            )
        match = session.get(Match, row["match_id"]) if row["match_id"] else None
        if not match:
            row["reason"] = "未映射：" + (
                "映射比赛不在数据库中" if row["match_id"] else _unmapped_reason(row, mapping_reason)
            )
            continue
        identity = row["matched_identity"]
        current = dict(
            home=session.get(Team, match.home_team_id).name,
            away=session.get(Team, match.away_team_id).name,
            source=match.source,
            league=match.league,
            kickoff_utc=match.kickoff_utc,
        )
        if any(current[k] != identity[k] for k in ("home", "away", "source", "league")) or (
            _timestamp(current["kickoff_utc"]) != _timestamp(identity["kickoff_utc"])
        ):
            row["reason"] = "数据库身份与期次映射快照冲突，需重新核对"
        elif match.status == "cancelled":
            row["reason"] = "比赛已取消，不能按正常14场计算覆盖"
        elif (
            match.league == "欧国联"
            and batch["mode"] == "prematch"
            and (
                row.get("source_ended")
                or match.status != "scheduled"
                or generated >= _timestamp(row["kickoff_utc"])
            )
        ):
            row["reason"] = "场次已结束、已开赛或状态变化，本次赛前预测已失效"
        elif not predicted:
            row["reason"] = "该冻结批次没有此比赛的球队模型概率"
        elif predicted["status"] != "predicted":
            row["reason"] = predicted.get("reason", "原批次排除")
        elif not paper_context_allowed(batch) or not paper_version_allowed(
            predicted, required="model_definition_schema" in batch
        ):
            row["reason"] = "该模型版本不具备纸面使用资格，不能用于14场组合"
        elif any(predicted[k] != identity[k] for k in ("home", "away", "source", "league")) or (
            _timestamp(predicted["kickoff_utc"]) != _timestamp(identity["kickoff_utc"])
        ):
            row["reason"] = "冻结预测与期次身份或开球时间冲突"
        elif predicted["model_name"] != "dixon-coles":
            row["reason"] = "只接受独立球队模型，不能使用市场或混合模型"
        elif (
            as_of > forecast_time
            or forecast_time > generated
            or as_of >= _timestamp(row["kickoff_utc"])
        ):
            row["reason"] = "冻结批次时间不合法或预测截点已开赛"
        elif batch["mode"] == "prematch" and forecast_time >= _timestamp(row["kickoff_utc"]):
            row["reason"] = "该预测在开赛后生成，不能算赛前实录"
        else:
            model = models.get((predicted["model_version"], match.source, match.league))
            if (
                not model
                or _timestamp(model["model_as_of_utc"]) > as_of
                or (_timestamp(model["last_available_utc"]) >= _timestamp(model["model_as_of_utc"]))
            ):
                row["reason"] = "模型版本或训练可知时间依据缺失/冲突"
                continue
            if not paper_version_matches(
                predicted, model, required="model_definition_schema" in batch
            ):
                row["reason"] = "模型版本资格与冻结预测不一致，不能用于14场组合"
                continue
            if "model_definition_schema" in batch or any(
                key in predicted for key in PAPER_VERSION_FIELDS
            ):
                row.update(
                    {
                        key: copy.deepcopy(predicted[key])
                        for key in ("p2_label", "p2_ready")
                        if key in predicted
                    }
                )
            row["probabilities"] = copy.deepcopy(predicted["model_probabilities"])
            row["model_version"] = predicted["model_version"]
            row["top_outcome"] = max("hda", key=lambda k: row["probabilities"][k])
            row["prior_disclosure"] = prior_disclosure(predicted)
    combo = None
    if all(r["probabilities"] is not None for r in rows):
        combo = optimize_cover([r["probabilities"] for r in rows], max_lines)
        for row, selection in zip(rows, combo["selections"], strict=True):
            row["selection"] = selection
    missing = sum(r["probabilities"] is None for r in rows)
    minimum_lines = 3**missing
    if allow_missing and missing:
        for row in rows:
            if row["probabilities"] is None:
                row["selection"] = list("hda")
                if not row["reason"].startswith("未映射："):
                    row["reason"] = "无模型：" + (row["reason"] or "概率缺失")
        if minimum_lines <= max_lines:
            known = [r for r in rows if r["probabilities"] is not None]
            combo = optimize_cover([r["probabilities"] for r in known], max_lines // minimum_lines)
            for row, selection in zip(known, combo["selections"], strict=True):
                row["selection"] = selection
            combo.update(
                lines=combo["lines"] * minimum_lines,
                max_lines=max_lines,
                selections=[r["selection"] for r in rows],
                missing_forced_three=missing,
            )
    end = _timestamp(issue.sale_end_utc) if issue.sale_end_utc else None
    sale_state = (
        "deadline_unknown"
        if end is None
        else "past_reported_deadline"
        if end <= generated
        else "third_party_deadline_pending_verification"
    )
    return dict(
        schema_version=1,
        kind="sfc14",
        issue_no=issue.issue_no,
        generated_utc=generated.isoformat(),
        probability_type="P2",
        bet_allowed=False,
        execution_verified=False,
        mode=batch["mode"],
        sale_state=sale_state,
        source=dict(
            url=issue.source_url,
            observed_utc=issue.observed_utc,
            raw_sha256=issue.raw_sha256,
            date_basis=issue.date_basis,
            sale_end_utc=issue.sale_end_utc,
            level="third_party",
        ),
        forecast_sha256=forecast_sha256,
        forecast_batch=copy.deepcopy(batch),
        rows=rows,
        combo=combo,
        max_lines=max_lines,
        **(
            {"diagnostic": True, "missing_models": missing, "minimum_lines": minimum_lines}
            if allow_missing
            else {}
        ),
        confidence_order=[
            r["seq"]
            for r in sorted(
                (r for r in rows if r["probabilities"] and not uses_unknown_team_prior(r)),
                key=lambda r: -max(r["probabilities"].values()),
            )
        ],
    )


def disclosed_rows(report):
    """Read prior evidence from the original forecast without changing frozen SFC rows."""
    predictions = {
        (p.get("match_id"), p.get("model_version")): p
        for p in report.get("forecast_batch", {}).get("predictions", [])
    }
    rows = []
    for row in report["rows"]:
        original = predictions.get((row.get("match_id"), row.get("model_version")))
        evidence = prior_disclosure(row)
        identity = row.get("matched_identity") or row
        if (
            original is not None
            and row.get("probabilities") is not None
            and original.get("model_probabilities") == row["probabilities"]
            and all(
                original.get(key) == identity.get(key)
                for key in ("home", "away", "source", "league")
            )
            and original.get("kickoff_utc")
            and identity.get("kickoff_utc")
            and _timestamp(original["kickoff_utc"]) == _timestamp(identity["kickoff_utc"])
        ):
            source = prior_disclosure(original)
            # A conflicting derived record must not clear affirmative prior use.
            if evidence["used"] is not True or source["used"] is True:
                evidence = source
        rows.append({**row, "prior_disclosure": evidence})
    return rows


def _prior_diagnostics(rows, *, explain=True):
    affected = [row for row in rows if uses_unknown_team_prior(row)]
    if not affected:
        return []
    lines = ["### 默认实力诊断", ""]
    for row in affected:
        lines.append(f"- 第 {row['seq']} 场：{_md(prior_label(row))}。")
    if explain:
        lines.extend(["", _md(prior_footnote(affected))])
    return lines + [""]


def _row_reason(row, default, *, export=False):
    reason = row.get("reason") or default
    note = prior_export_label(row) if export else prior_label(row)
    return "；".join(part for part in (reason, note) if part)


def unconfirmed_mapping_status(row):
    """Keep a disclosed mapping gap visible without exposing unpublished probabilities."""
    reason = row.get("reason") or ""
    return "无冻结概率" + ("；" + reason if reason.startswith("未映射：") else "")


def pick_nine_lines(report, *, prior_explained=False):
    """Render one single-choice ticket from verified, frozen match probabilities."""
    lines = ["### 任九 · 模型单选", ""]
    rows = disclosed_rows(report) if report else []
    lines += _prior_diagnostics(rows, explain=not prior_explained)
    if not report or report.get("sale_state") != "published_before_deadline":
        return lines + ["未确认报告在截止前发布，本期不展示任九方案。", ""]
    if report.get("mode") != "prematch":
        return lines + ["没有赛前冻结预测，本期不展示任九方案。", ""]
    eligible = [
        row
        for row in rows
        if row.get("probabilities")
        and not uses_unknown_team_prior(row)
        and row.get("kickoff_basis") != "placeholder"
        and _timestamp(report["published_utc"]) < _timestamp(row["kickoff_utc"])
    ]
    if len(eligible) < 9:
        return lines + [f"只有 {len(eligible)} 场有可用赛前概率，不足 9 场。", ""]
    ranked = sorted(eligible, key=lambda r: (-max(r["probabilities"].values()), r["seq"]))
    chosen = sorted(ranked[:9], key=lambda r: r["seq"])
    lines += ["| 原场号 | 对阵 | 单选 | 模型概率 |", "|---|---|---|---|"]
    outcomes = {"h": "3 主胜", "d": "1 平", "a": "0 客胜"}
    combined = 1.0
    for row in chosen:
        probs = row["probabilities"]
        pick = max("hda", key=probs.__getitem__)
        combined *= probs[pick]
        lines.append(
            f"| {row['seq']} | {_md(row['home'])} VS {_md(row['away'])} | "
            f"{outcomes[pick]} | {_percent(probs[pick])} |"
        )
    return lines + [
        "",
        f"按各场相互独立估算，九场全中概率 {_percent(combined)}。"
        "这是模型估算，实际命中率待赛后检验。",
        "",
    ]


def _model_notice(report):
    if report.get("forecast_batch", {}).get("model_definition_schema") == 1:
        return "**球队模型，BET 禁用；各版本状态见逐场记录，不构成购买建议。**"
    return "**P2 未验证模型，BET 禁用；不构成购买建议。**"


def _markdown(report):
    if report.get("publication_schema") and report["sale_state"] != "published_before_deadline":
        return _unconfirmed_markdown(report)
    source, batch = report["source"], report["forecast_batch"]
    rows = disclosed_rows(report)
    title = "诊断" if report.get("diagnostic") else "实验参考"
    lines = [
        f"# 第{report['issue_no']}期 · 14场胜负彩{title}",
        "",
        _model_notice(report),
        "",
        f"报告生成：{_beijing(report['generated_utc'])}（北京时间）。",
        *(
            [
                deadline_message(report) + "。",
                f"存好时间：{_beijing(report['published_utc'])}（北京时间）；"
                f"发布提交：`{report['publication_commit']}`。",
            ]
            if report.get("publication_schema")
            else []
        ),
        f"原预测生成：{_beijing(batch['generated_utc'])}；数据截点：{_beijing(batch['as_of_utc'])}。",
        f"模式：{_md(report['mode'])}；historical_replay 为事后重建，不能当作未见验证。",
        "",
        f"期次来源：{_md(source['url'])}（第三方）；读取：{_beijing(source['observed_utc'])}。",
        f"来源所报截止：{_beijing(source['sale_end_utc']) if source['sale_end_utc'] else '未知'}；"
        f"状态：{_md(report['sale_state'])}。官方资格、网点出票窗口均未核实。",
        f"日期依据：{_md(source['date_basis'])}；issue_year_assumed 表示使用期号年份解释短日期。",
        "",
        "普通胜负彩：3=主胜、1=平、0=客胜；90分钟及伤停补时。",
        "",
        "|序号|赛事/对阵|北京时间|匹配|主胜|平|客胜|最高项|复式覆盖项|状态/缺口|",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        p = r["probabilities"] or {}
        columns = [
            r["seq"],
            f"{r['league']} {r['home']}—{r['away']}",
            _beijing(r["kickoff_utc"])
            + ("（占位开球时间，待核实）" if r.get("kickoff_basis") == "placeholder" else ""),
            r["mapping"],
            *[_percent(p.get(k)) for k in "hda"],
            OPTIONS.get(r["top_outcome"], "—"),
            "+".join(OPTIONS[k] for k in r["selection"]) if r["selection"] else "—",
            _row_reason(r, r.get("p2_label") or "P2 实验"),
        ]
        lines.append("|" + "|".join(_md(c) for c in columns) + "|")
    if note := prior_footnote(rows):
        lines.extend(["", _md(note)])
    eligible_sequences = {r["seq"] for r in rows if not uses_unknown_team_prior(r)}
    confidence_order = [seq for seq in report["confidence_order"] if seq in eligible_sequences]
    lines += [
        "",
        "单场最高项置信度顺序（仅数值排序）："
        + ("、".join(map(str, confidence_order)) or "无可用概率")
        + "。",
        "",
    ]
    lines += pick_nine_lines(report, prior_explained=True)
    combo = report["combo"]
    if combo:
        lines += [
            f"在不超过 {report['max_lines']} 注的完整复式范围内，最大覆盖方案为 "
            f"{combo['lines']} 注；14/14赛果覆盖概率 {_percent(combo['coverage_probability'])}。",
            "使用跨场独立近似，动态规划精确比较注数可行状态；不是10万次模拟。",
            "这是全部14场赛果落在所选项内的概率，不是含13场奖的中奖概率、净盈利概率或正EV。",
            "共同模型误差、选择偏差及比赛间依赖未获验证；奖金、税费和真实可购买价值未计算。",
        ]
    else:
        if report.get("diagnostic"):
            lines += [
                f"无模型场次按三选处理，至少 {report['minimum_lines']} 注，超过 "
                f"{report['max_lines']} 注上限；无法生成可行的完整复式，覆盖概率未生成。"
            ]
        else:
            lines += ["**14场模型输入不完整或存在冲突：无法可靠计算整期覆盖，不生成复式方案。**"]
    if report.get("diagnostic"):
        lines += [
            f"无模型 {report['missing_models']} 场保留空概率并按三选处理；"
            "全包＝无预测；有模型场次才有单选首项。",
            "本报告仅作诊断，不设虚拟资金、不估算奖金。"
            "forecast_batch 是当日冻结批次的引用合并视图，",
            "不是新预测；原件路径、哈希与完整批次保存在 forecast_sources，不改写任何原预测。",
        ]
    lines += [
        "",
        "冻结预测完整依据保存在 sfc.json 的 forecast_batch，原精度保留。",
        f"原预测 SHA256：`{report['forecast_sha256']}`；"
        f"期次原件 SHA256：`{source['raw_sha256']}`。",
        "后续报价、赛果、队名映射或模型更新不会改写本报告。",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _unconfirmed_markdown(report):
    """Keep evidence in JSON while exposing only fixtures for an ineligible issue."""
    source = report["source"]
    published = _beijing(report["published_utc"]) if report["published_utc"] else "未知"
    deadline = _beijing(report["sale_end_utc"]) if report["sale_end_utc"] else "未知"
    lines = [
        f"# 第{report['issue_no']}期 · 14场胜负彩诊断",
        "",
        _model_notice(report),
        "",
        deadline_message(report) + "。",
        f"算完时间：{_beijing(report['generated_utc'])}；存好时间：{published}；"
        f"停售时间：{deadline}（北京时间）。",
        f"发布提交：{report['publication_commit'] or '未知'}；状态：{report['sale_state']}。",
        f"期次来源：{_md(source['url'])}（第三方）；读取：{_beijing(source['observed_utc'])}。",
        "",
        "|序号|赛事/对阵|北京时间|状态|",
        "|---|---|---|---|",
    ]
    rows = disclosed_rows(report)
    for row in rows:
        kickoff = _beijing(row["kickoff_utc"])
        if row.get("kickoff_basis") == "placeholder":
            kickoff += "（占位开球时间，待核实）"
        values = [
            row["seq"],
            f"{row['league']} {row['home']}—{row['away']}",
            kickoff,
            _row_reason({**row, "reason": unconfirmed_mapping_status(row)}, ""),
        ]
        lines.append("|" + "|".join(_md(value) for value in values) + "|")
    if note := prior_footnote(rows):
        lines.extend(["", _md(note)])
    lines += [
        "",
        "原始计算与引用预测证据保存在 sfc.json；未确认停售前存好，不作冻结概率展示。",
        f"原预测 SHA256：`{report['forecast_sha256']}`；"
        f"期次原件 SHA256：`{source['raw_sha256']}`。",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _csv(report):
    if report.get("publication_schema") and report["sale_state"] != "published_before_deadline":
        return _unconfirmed_csv(report)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "issue",
            "seq",
            "match_id",
            "league",
            "home",
            "away",
            "kickoff_beijing",
            "mapping",
            "p_home",
            "p_draw",
            "p_away",
            "top_outcome",
            "selection",
            "reason",
            "model_version",
            "mode",
            "probability_type",
            "bet_allowed",
            "execution_verified",
            "forecast_batch_id",
            "forecast_generated_utc",
            "forecast_as_of_utc",
            "report_generated_utc",
            "sale_state",
            "sale_end_utc",
            "source_observed_utc",
            "date_basis",
            *(["published_utc", "publication_commit"] if report.get("publication_schema") else []),
        ]
    )
    for r in disclosed_rows(report):
        p = r["probabilities"] or {}
        reference = r.get("forecast_reference") or report["forecast_batch"]
        writer.writerow(
            [
                _csv_cell(v)
                for v in [
                    report["issue_no"],
                    r["seq"],
                    r["match_id"],
                    r["league"],
                    r["home"],
                    r["away"],
                    _beijing(r["kickoff_utc"]),
                    r["mapping"],
                    *[p.get(k) for k in "hda"],
                    OPTIONS.get(r["top_outcome"]),
                    "+".join(OPTIONS[k] for k in r["selection"]) if r["selection"] else None,
                    _row_reason(r, "", export=True) or None,
                    r.get("model_version"),
                    report["mode"],
                    report["probability_type"],
                    report["bet_allowed"],
                    report["execution_verified"],
                    reference["batch_id"],
                    reference["generated_utc"],
                    reference["as_of_utc"],
                    report["generated_utc"],
                    report["sale_state"],
                    report["source"]["sale_end_utc"],
                    report["source"]["observed_utc"],
                    report["source"]["date_basis"],
                    *(
                        [report["published_utc"], report["publication_commit"]]
                        if report.get("publication_schema")
                        else []
                    ),
                ]
            ]
        )
    return stream.getvalue().encode("utf-8-sig")


def _unconfirmed_csv(report):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "issue",
            "seq",
            "league",
            "home",
            "away",
            "kickoff_beijing",
            "status",
            "generated_utc",
            "published_utc",
            "sale_end_utc",
            "sale_state",
            "publication_commit",
        ]
    )
    for row in disclosed_rows(report):
        writer.writerow(
            [
                _csv_cell(value)
                for value in [
                    report["issue_no"],
                    row["seq"],
                    row["league"],
                    row["home"],
                    row["away"],
                    _beijing(row["kickoff_utc"]),
                    _row_reason(
                        {**row, "reason": unconfirmed_mapping_status(row)}, "", export=True
                    ),
                    report["generated_utc"],
                    report["published_utc"],
                    report["sale_end_utc"],
                    report["sale_state"],
                    report["publication_commit"],
                ]
            ]
        )
    return stream.getvalue().encode("utf-8-sig")


def write_sfc_report(report, output: Path):
    frozen = _json_bytes(report)
    # Render the exact frozen value, not mutable caller state.
    frozen_report = json.loads(frozen)
    contents = {
        "sfc.json": frozen,
        "report.md": _markdown(frozen_report),
        "report.csv": _csv(frozen_report),
    }
    contents["manifest.json"] = _json_bytes(
        dict(
            schema_version=1,
            algorithm="sha256",
            files={name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
            **(
                {key: frozen_report[key] for key in PUBLICATION_FIELDS}
                if frozen_report.get("publication_schema")
                else {}
            ),
        )
    )
    output.mkdir(parents=True, exist_ok=False)
    written = []
    try:
        for name, data in contents.items():
            file = output / name
            with file.open("xb"):
                written.append(file)
            file.write_bytes(data)
    except BaseException:
        for file in written:
            file.unlink(missing_ok=True)
        if not any(output.iterdir()):
            output.rmdir()
        raise
    return output / "sfc.json"


def run_sfc_report(args):
    path = Path(args.batch)
    before = path.read_bytes()
    batch = load_batch(path)
    if path.read_bytes() != before or json.loads(before) != batch:
        raise ValueError("冻结预测在读取期间发生变化，请重试")
    digest = hashlib.sha256(before).hexdigest()
    engine = get_engine(args.db)
    try:
        init_db(engine)
        with session_scope(engine) as session:
            if args.issue:
                issue = session.get(SfcIssue, args.issue)
            else:
                issues = list(session.scalars(select(SfcIssue)))
                if len(issues) != 1:
                    raise ValueError("数据库无期次或有多个期次，请明确 --issue")
                issue = issues[0]
            if not issue:
                raise ValueError("未找到期次，请先 fetch sfc --issue")
            report = build_sfc_report(
                session,
                issue,
                batch,
                forecast_sha256=digest,
                generated=datetime.now(UTC),
                max_lines=args.max_lines,
            )
        write_sfc_report(report, Path(args.out))
        LOG.info(
            "已冻结第%s期14场报告 %s；有模型概率%d/14",
            report["issue_no"],
            args.out,
            sum(r["probabilities"] is not None for r in report["rows"]),
        )
        return report
    finally:
        engine.dispose()
