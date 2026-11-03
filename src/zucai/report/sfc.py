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

LOG = logging.getLogger(__name__)
OPTIONS = {"h": "3", "d": "1", "a": "0"}


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
    rows = sorted(copy.deepcopy(issue.snapshot), key=lambda r: r["seq"])
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
        row.update(probabilities=None, reason=None, top_outcome=None, selection=None)
        predicted = predictions.get(row["match_id"])
        match = session.get(Match, row["match_id"]) if row["match_id"] else None
        if not match:
            row["reason"] = "映射比赛不在数据库中"
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
        elif not predicted:
            row["reason"] = "该冻结批次没有此比赛的球队模型概率"
        elif predicted["status"] != "predicted":
            row["reason"] = predicted.get("reason", "原批次排除")
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
            row["probabilities"] = copy.deepcopy(predicted["model_probabilities"])
            row["model_version"] = predicted["model_version"]
            row["top_outcome"] = max("hda", key=lambda k: row["probabilities"][k])
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
                (r for r in rows if r["probabilities"]),
                key=lambda r: -max(r["probabilities"].values()),
            )
        ],
    )


def _markdown(report):
    source, batch = report["source"], report["forecast_batch"]
    title = "诊断" if report.get("diagnostic") else "实验参考"
    lines = [
        f"# 第{report['issue_no']}期 · 14场胜负彩{title}",
        "",
        "**P2 未验证模型，BET 禁用；不构成购买建议。**",
        "",
        f"报告生成：{_beijing(report['generated_utc'])}（北京时间）。",
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
    for r in report["rows"]:
        p = r["probabilities"] or {}
        columns = [
            r["seq"],
            f"{r['league']} {r['home']}—{r['away']}",
            _beijing(r["kickoff_utc"]),
            r["mapping"],
            *[_percent(p.get(k)) for k in "hda"],
            OPTIONS.get(r["top_outcome"], "—"),
            "+".join(OPTIONS[k] for k in r["selection"]) if r["selection"] else "—",
            r["reason"] or "P2 实验",
        ]
        lines.append("|" + "|".join(_md(c) for c in columns) + "|")
    lines += [
        "",
        "单场最高项置信度顺序（仅数值排序）："
        + ("、".join(map(str, report["confidence_order"])) or "无可用概率")
        + "。",
        "",
    ]
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
            f"无模型 {report['missing_models']} 场保留空概率并按三选处理；有模型场次才有单选首项。",
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


def _csv(report):
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
        ]
    )
    for r in report["rows"]:
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
                    r["reason"],
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
