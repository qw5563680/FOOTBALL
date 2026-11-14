"""Daily and review views of archived facts, with explicit observation times."""

import re
from datetime import datetime, time, timedelta

from zucai.digest_context import (
    catalog_gaps,
    kickoff_day,
    observed_result,
    prediction_window,
    settled_since,
    week_start,
)
from zucai.digest_display import BEIJING, _instant, _link, _match, _number, _probabilities, _text
from zucai.digest_odds import batch_odds_lines, collection_lines, odds_time, same_collection_attempt
from zucai.prediction_disclosure import prior_footnote, prior_label
from zucai.scoreboard.score import enrich_prior_disclosures

SKIP_REASONS = {
    "match_status_not_selling": "比赛状态非 Selling",
    "sell_status_not_1": "销售状态非 1",
    "no_fixed_odds": "未提供固定奖金玩法",
    "no_selling_fixed_pool": "没有在售的固定奖金玩法",
    "no_valid_fixed_quotes": "没有有效的固定奖金报价",
}


def _order(row):
    # The weekday is a display sort hint only, never evidence of catalog completeness.
    day = row.get("business_date")
    number = row.get("jc_num") or "~"
    if not day and row.get("kickoff_utc"):
        local = _instant(row["kickoff_utc"]).astimezone(BEIJING).date()
        weekday = next(
            (i for i, name in enumerate("一二三四五六日") if number.startswith("周" + name)),
            local.weekday(),
        )
        day = (local - timedelta(days=(local.weekday() - weekday) % 7)).isoformat()
    return (
        day or "",
        number,
        row["match_id"],
    )


def _local(value):
    return (
        _instant(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        if value
        else "未知（旧归档未记录成功时刻）"
    )


def prediction_source_lines(run):
    snapshot = run.get("feed_snapshot")
    lines = []
    if run.get("sporttery_publish_status") == "failure":
        lines.append("本批预测前竞彩数据发布失败，本次采集结果未确认发布；预测使用已发布快照。")
    elif run.get("sporttery_stale"):
        lines.append("本批预测已标记竞彩数据未确认更新，不能视为本次最新采集数据。")
    if snapshot:
        lines.append(
            f"预测所用数据：提交 {_text(snapshot.get('commit'))}；"
            f"归档 run {_text(snapshot.get('run_id'))}；"
            f"归档时间 {_local(snapshot.get('created_utc'))}（北京时间，不代表竞彩采集时间）。"
        )
        lines.append(
            f"该预测快照的竞彩接口最近成功检查：{_local(snapshot.get('schedule_updated_utc'))}；"
            f"赛果更新至：{_local(snapshot.get('results_updated_utc'))}（北京时间）。"
        )
    elif run.get("sporttery_stale"):
        lines.append(
            "旧冻结未记录数据提交与竞彩更新时间；"
            f"输入清单 SHA256：{_text(run.get('feed_manifest_sha256'))}。"
        )
    if status := run.get("source_version_status"):
        label = {"complete": "成功", "partial": "部分失败", "failed": "失败"}[status["status"]]
        lines.append(
            f"新来源候选：{label}；阶段 {_text(status['stage'])}；"
            f"记录时间 {_local(status['recorded_utc'])}（北京时间）。"
        )
        if status["reason"]:
            lines.append(
                f"候选失败原因：`{status['reason']}`；"
                f"异常类别 {_text(status['error_type'])}；"
                f"受影响联赛：{'、'.join(status['failed_leagues'])}。"
            )
        lines.append(
            "候选附件已冻结；训练失败的联赛保留排除行。"
            if status["artifacts_frozen"]
            else "本次未冻结新来源候选附件，缺少对应的候选预测；已冻结主版本保持原样。"
        )
    return lines + ([""] if lines else [])


def _known_unstarted_lines(day, now, repository, catalog, sfc_issues, *, shown_ids=()):
    def upcoming(row):
        return (
            not row.get("source_ended")
            and row.get("kickoff_basis") != "placeholder"
            and kickoff_day(row) is not None
            and kickoff_day(row).isoformat() == day
            and _instant(row["kickoff_utc"]) > now
        )

    lines = []
    jc_ids = {
        row["match_id"]
        for row in catalog or []
        if row.get("captured_utc")
        and _instant(row["captured_utc"]) <= now
        and row["match_id"] not in shown_ids
        and upcoming(row)
    }
    if jc_ids:
        lines.append(f"竞彩目录有 {len(jc_ids)} 场未开球比赛，但本批未提供对应竞彩预测。")
    for path, issue in sfc_issues or []:
        if not issue.get("observed_utc") or _instant(issue["observed_utc"]) > now:
            continue
        rows = [
            row
            for row in issue["snapshot"]
            if row.get("match_id") not in shown_ids and upcoming(row)
        ]
        if not rows:
            continue
        lines.append(
            f"胜负彩 {_text(issue['issue_no'])} 期有 {len(rows)} 场未开球比赛未在本表展示"
            f"（来源：{_link(path, repository)}）。"
            "最新映射与胜负彩专用冻结预测见下方 14 场胜负彩。"
        )
    return lines + ([""] if lines else [])


def forecast_sections(
    batches,
    today,
    repository,
    *,
    now=None,
    matches=None,
    displayed_ids=None,
    runs=None,
    catalog=None,
    sfc_issues=None,
):
    now = now or datetime.combine(today, time.min, BEIJING)
    matches = matches or {}
    lines = ["## 今日与明日预测（仅发报时未开球）", ""]
    for offset, label in ((0, "今日"), (1, "明日")):
        day = (today + timedelta(days=offset)).isoformat()
        choices = [entry for entry in batches if entry[0] == day]
        lines.extend([f"### {label} {day}", ""])
        if not choices:
            known = _known_unstarted_lines(day, now, repository, catalog, sfc_issues)
            lines.extend(["尚无竞彩冻结预测。" if known else "尚无冻结预测。", "", *known])
            continue
        _, path, batch = max(
            choices, key=lambda e: (_instant(e[2]["generated_utc"]), e[2]["batch_id"])
        )
        lines.extend(
            [
                f"来源：{_link(path, repository)}；批次 {_text(batch['batch_id'])}；"
                f"生成 UTC：{_text(batch['generated_utc'])}。",
                "",
            ]
        )
        source_run = next(
            (
                run
                for run in (runs or {}).values()
                if any(
                    entry["path"] + "/forecast.json" == path for entry in run["forecasts"].values()
                )
            ),
            {},
        )
        lines.extend(prediction_source_lines(source_run))
        lines.extend(batch_odds_lines(batch, source_run))
        checked_at = (source_run.get("feed_snapshot") or {}).get("schedule_updated_utc")
        rows = [
            {**matches.get(row["match_id"], {}), **row}
            for row in batch["predictions"]
            if row.get("kickoff_utc") and _instant(row["kickoff_utc"]) > now
        ]
        if not rows:
            lines.extend(
                _known_unstarted_lines(day, now, repository, catalog, sfc_issues)
                or ["该批次无未开球比赛；已开球场次见复盘。", ""]
            )
            continue
        lines.extend(
            [
                "| 竞彩编号 | 北京开球 | 联赛 / 对阵 | 胜 / 平 / 负 | 模型版本 | P2 | 比分前五 | "
                "冻结赔率采集时间（北京时间） |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        versions = {}
        for row in sorted(rows, key=_order):
            head = (
                f"| {_text(row.get('jc_num'))} | {_local(row['kickoff_utc'])} | "
                f"{_text(row.get('league'))} / {_match(row)}"
            )
            if displayed_ids is not None:
                displayed_ids.add(row["match_id"])
            if row["status"] != "predicted":
                lines.append(
                    f"{head} | 未预测：{_text(row.get('reason'))} | — | — | — | 未冻结赔率 |"
                )
                continue
            scores = (
                "；".join(
                    f"{_text(s['score'])} {_number(s['probability'], 2, percent=True)}"
                    for s in row.get("top_scores", [])[:5]
                )
                or "未提供"
            )
            status = "可用" if row.get("p2_ready") is True else "实验"
            disclosure = prior_label(row)
            if disclosure:
                status += "；" + _text(disclosure)
            version = row["model_version"]
            reference = _text(version)
            if re.fullmatch(r"[0-9a-fA-F]{64}", version):
                reference = versions.setdefault(version, f"M{len(versions) + 1}")
            lines.append(
                f"{head} | {_probabilities(row)} | {_text(row['model_name'])} / {reference} | "
                f"{status} | {scores} | "
                f"{odds_time(row, checked_at=checked_at)} |"
            )
        lines.append("")
        if note := prior_footnote(rows):
            lines.extend([_text(note), ""])
        lines.extend(
            f"模型版本 {reference}：{_text(version)}。" for version, reference in versions.items()
        )
        if versions:
            lines.append("")
        lines.extend(
            _known_unstarted_lines(
                day,
                now,
                repository,
                catalog,
                sfc_issues,
                shown_ids={row["match_id"] for row in rows},
            )
        )
    return lines


def review_sections(
    latest,
    observations,
    matches,
    scored,
    summary,
    catalog,
    skipped,
    now,
    *,
    displayed_ids=None,
    window=None,
):
    start = week_start(now)
    active = set((summary or {}).get("audit", {}).get("active_keys", []))
    scores = {
        (r["match_id"], r["batch_id"]): r
        for r in scored
        if r["record_id"] in active and _instant(r["available_utc"]) <= now
    }
    rows = {
        key: row
        for key, row in observations.items()
        if row.get("kickoff_utc") and start <= _instant(row["kickoff_utc"]) <= now
    }
    rows.update(
        {key: row for key, row in latest.items() if start <= _instant(row["kickoff_utc"]) <= now}
    )
    for key, row in matches.items():
        if row.get("jc_num") and kickoff_day(row) and start <= _instant(row["kickoff_utc"]) <= now:
            rows.setdefault(key, row)
    for row in catalog or []:
        if kickoff_day(row) and start <= _instant(row["kickoff_utc"]) <= now:
            rows.setdefault(row["match_id"], {**row, **matches.get(row["match_id"], {})})
    settled = []
    pending = 0
    for key in rows:
        frozen = latest.get(key)
        score = scores.get((key, frozen["batch_id"])) if frozen else None
        if score:
            settled.append(score)
        elif frozen:
            pending += 1
    # Count existing predictions against existing labels; never recompute LL/Brier.
    hits = sum(max("hda", key=r["model_probabilities"].get) == r["actual"] for r in settled)
    lines = [
        "## 复盘（本周已开球）",
        "",
        f"已结 {len(settled)} 场、猜对 {hits} 场、猜错 {len(settled) - hits} 场、"
        f"待核对 {pending} 场。",
        "以记分板 active_keys 对应的最后一次有效赛前预测统计；缺失预测不计命中率。"
        "并列首项沿用 h/d/a 顺序。",
        "",
        "| 竞彩编号 | 北京开球 / 对阵 | 最后有效赛前胜 / 平 / 负 | "
        "实际比分 | 首项命中 | 冻结批次 | 冻结赔率采集时间（北京时间） |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key, row in sorted(rows.items(), key=lambda pair: _order(pair[1])):
        frozen = latest.get(key)
        score = scores.get((key, frozen["batch_id"])) if frozen else None
        result = score or observed_result(matches.get(key, {}), now)
        actual = f"{result['home_goals']}:{result['away_goals']}" if result else "待核对"
        hit = (
            (
                "命中"
                if max("hda", key=score["model_probabilities"].get) == score["actual"]
                else "未中"
            )
            if score
            else "未计分"
        )
        probability = _probabilities(frozen) if frozen else "无可查的赛前预测"
        disclosure = prior_label(frozen) if frozen else ""
        if disclosure:
            probability += "；" + _text(disclosure)
        lines.append(
            f"| {_text(row.get('jc_num'))} | {_local(row['kickoff_utc'])} / {_match(row)} | "
            f"{probability} | {actual} | {hit} | {_text(frozen['batch_id']) if frozen else '—'} | "
            f"{odds_time(frozen) if frozen else '未冻结赔率'} |"
        )
    if not rows:
        lines.append("| 尚无本周已开球记录 | — | — | — | — | — | — |")
    if note := prior_footnote(latest[key] for key in rows if key in latest):
        lines.extend(["", _text(note)])
    enriched = enrich_prior_disclosures(settled, latest.values(), as_of=now)
    lines.extend(
        [
            "",
            "### 默认实力分层",
            "",
            "沿用上表相同 active_keys 与原赛果；旧记录只关联同一冻结批次和模型版本。",
            "",
            "| 预测依据 | 已结 | 猜对 | 猜错 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for label, used in (
        ("使用默认实力", True),
        ("双方均已知", False),
        ("是否使用默认实力未记录", None),
    ):
        group = [r for r in enriched if r["prior_disclosure"]["used"] is used]
        correct = sum(max("hda", key=r["model_probabilities"].get) == r["actual"] for r in group)
        lines.append(f"| {label} | {len(group)} | {correct} | {len(group) - correct} |")
    lines.extend(["", "### 竞彩完整清单逐号核对", ""])
    displayed = set(latest) if displayed_ids is None else displayed_ids | (set(rows) & set(latest))
    window = window or prediction_window([], now)
    gaps = catalog_gaps(catalog, skipped, displayed, observations, window=window, known_ids=matches)
    lines.append(
        "计划覆盖北京开球日："
        + "、".join(window["planned_dates"])
        + "；已有成功冻结批次："
        + ("、".join(window["successful_dates"]) or "无")
        + "。"
    )
    if window["missing_batch_dates"]:
        lines.append(
            "计划日期尚无成功批次："
            + "、".join(window["missing_batch_dates"])
            + "；可能尚未生成或整批失败，不能据此缩小预测窗口。"
        )
    if catalog:
        days = sorted({row["business_date"] for row in catalog})
        lines.append(
            "核对范围仅限已归档营业日：" + "、".join(map(_text, days)) + "；其他营业日无法判定。"
        )
    if gaps is None:
        lines.append("截点前没有可用清单，无法核对是否漏预测。旧归档不凭编号连续性猜缺号或原因。")
    elif not catalog:
        lines.append("已有归档清单，本周开球范围内及近期未知时间的条目为空。")
    elif not gaps:
        lines.append("归档完整清单逐号核对：编号均已在相应表内说明（含未预测原因）。")
    if gaps is not None:
        counts = {
            kind: sum(r["category"] == kind for r in gaps)
            for kind in ("missing", "unexplained", "outside_window", "unknown_window")
        }
        lines.append(
            f"确定漏了（原因已知）{counts['missing']} 场；原因未查明 {counts['unexplained']} 场；"
            f"尚未进入预测窗口 {counts['outside_window']} 场；"
            f"开球时间缺失，无法判断窗口 {counts['unknown_window']} 场。"
            f"漏预测合计 {counts['missing'] + counts['unexplained']} 场，后两类不计入漏预测。"
        )
        for row in gaps:
            label = f"{_text(row['jc_num'])}（{_text(row['business_date'])}，{_match(row)}）"
            if row["category"] == "outside_window":
                lines.append(
                    f"- {label}：尚未进入预测窗口（北京时间 {_local(row['kickoff_utc'])} 开球）；"
                    f"预计自 {row['expected_window_date']} 的预测范围开始覆盖，"
                    "以实际批次生成情况为准，不承诺准点。"
                )
                continue
            if row["category"] == "unknown_window":
                lines.append(f"- {label}：开球时间缺失，无法判断窗口；不使用编号日或营业日猜日期。")
                continue
            reason = row["reason"]
            evidence = row["skip_evidence"]
            if evidence:
                detail = (
                    f"同步时跳过：{SKIP_REASONS.get(reason, _text(reason))}"
                    f"（{_local(evidence['captured_utc'])}）"
                )
            elif row["match_id"] in latest:
                detail = "有旧赛前预测，但本版最新批次未列入未开球预测表"
            elif row["match_id"] in matches:
                detail = "入库但无赛前预测（晚班或后续同步已入库，但无可查的赛前预测）"
            else:
                detail = "原因未查明（无可核验的同期跳过记录；旧早班原始页面未归档时不能推断原因）"
            if not row["reason_known"] and "原因未查明" not in detail:
                detail += "；原因未查明（无可追踪的同期原因证据）"
            lines.append(
                f"- 缺 {_text(row['jc_num'])}（{_text(row['business_date'])}，{_match(row)}）："
                f"确定漏了；{detail}。"
                + (
                    (
                        "该北京开球日尚无成功冻结批次；以上仅为该场同期跳过原因，不推断整批原因。"
                        if row["reason_known"]
                        else "该北京开球日属于计划范围，但尚无成功冻结批次；整批缺失原因待查。"
                    )
                    if row["batch_missing"]
                    else ""
                )
            )
    return lines + [""]


def header_lines(
    feed,
    latest,
    matches,
    now,
    *,
    run_id=None,
    jc_sync_status=None,
    jc_sync_run_id=None,
    jc_publish_status=None,
):
    jc = feed.get("collection", {}).get("jc", {})
    lines = [
        f"竞彩接口最近成功检查：{_local(jc.get('schedule_updated_utc'))}（北京时间）。",
        f"赛果更新至：{_local(jc.get('results_updated_utc'))}（北京时间）。",
    ]
    lines.extend(
        collection_lines(
            feed,
            now,
            run_id=run_id,
            jc_sync_status=jc_sync_status,
            jc_sync_run_id=jc_sync_run_id,
            jc_publish_status=jc_publish_status,
        )
    )
    failed_stages = jc.get("failed_stages") or []
    schedule_failed = jc.get("schedule_status") == "failed" or "schedule" in failed_stages
    results_failed = jc.get("results_status") == "failed" or "results" in failed_stages
    run = jc.get("run_id")
    pending = sum(
        week_start(now) <= _instant(row["kickoff_utc"]) <= now
        and not observed_result(matches.get(key, {}), now)
        for key, row in latest.items()
    )
    if schedule_failed:
        lines.append(
            f"赛程更新失败（run {_text(run)}）。成功时刻保留上次值，现有赛程与赔率未确认更新。"
        )
    if results_failed or jc.get("results_status") == "partial":
        if results_failed:
            lines.append(
                f"赛果更新失败（run {_text(run)}），还有 {pending} 场已开球未核对。"
                "成功时刻保留上次值，本次未取得的结果不猜测。"
            )
        else:
            lines.append(
                f"赛果部分未到，还有 {pending} 场待核对（run {_text(run)}）。"
                "成功时刻保留上次值，本次未取得的结果不猜测。"
            )
    if jc_publish_status == "failure":
        lines.append(
            f"竞彩数据发布失败（run {_text(jc_sync_run_id)}）：本次采集结果未确认发布；"
            "本版使用远端已发布归档，不能视为本次最新数据。各批预测的数据版本与时间见下方。"
        )
    elif jc_sync_status in {"failure", "cancelled"}:
        same_run = same_collection_attempt(run, run_id, jc_sync_run_id)
        if not same_run or not (schedule_failed or results_failed):
            lines.append(
                f"竞彩同步失败（run {_text(jc_sync_run_id)}），还有 {pending} 场已开球未核对；"
                "失败阶段未记录。"
                "成功时刻保留上次值，不推断为赛程或赛果失败。"
            )
    return lines


def pending_sections(pending, ledger, previous, now):
    lines = ["### 待结注单（按本版发报时刻）", ""]
    records = {r["record_id"]: r for r in ledger if r.get("record_type") == "ticket"}
    for account, rows in pending.items():
        lines.append(
            f"- 账户 {account}：发报时未开球 {sum(r['phase'] == 'upcoming' for r in rows)} 注；"
            f"发报时已开球、赛果未核对 {sum(r['phase'] == 'started' for r in rows)} 注；"
            f"开球时间缺失、无法分类 {sum(r['phase'] == 'unknown' for r in rows)} 注。"
        )
        for row in rows:
            ticket = records[row["record_id"]]
            phase = {
                "upcoming": "发报时未开球",
                "started": "发报时已开球、赛果未核对",
                "unknown": "开球时间缺失、无法分类",
            }[row["phase"]]
            legs = " × ".join(f"{_match(leg)} SP={_text(leg['sp'])}" for leg in ticket["legs"])
            lines.append(f"  - {legs}；注额 {_number(ticket['stake'], 2)}；{phase}。")
    lines.extend(["", "### 较上一版", ""])
    if previous is None:
        lines.append("本日首版。")
    else:
        lines.append(
            f"上一版：{_local(previous['generated_utc'])}，run {_text(previous['run_id'])}；"
            "比较同一天已归档日报的待结集合。"
        )
        for account, diff in settled_since(previous, ledger, now).items():
            if not diff["previous"]:
                lines.append(f"- 账户 {account}：上一版无待结注单。")
                continue
            unknown = f"、{diff['unknown']} 注开球时间未知" if diff["unknown"] else ""
            lines.append(
                f"- 账户 {account}：上一版待结 {diff['previous']} 注（{diff['upcoming']} 注未开球、"
                f"{diff['started']} 注已开球未核对{unknown}）→ 本版已结 {diff['settled']} 注："
                f"{diff['won']} 中 {diff['lost']} 错，盈亏 {diff['profit']:+.2f}；"
                f"仍待结 {diff['previous'] - diff['settled']} 注。"
            )
    return lines + [""]
