"""Read-only SFC section for the daily digest."""

from datetime import timedelta

from zucai.digest_context import archived_sfc_issues
from zucai.identity_pending import pending_label
from zucai.prediction_disclosure import prior_footnote, prior_label
from zucai.report.render import BEIJING, _beijing, _md, _percent, _timestamp
from zucai.report.sfc import (
    OPTIONS,
    disclosed_rows,
    hit_line,
    own_fixture_note,
    pick_nine_lines,
    unconfirmed_mapping_status,
)
from zucai.scoreboard.sfc import prior_group_text, read_settled
from zucai.sfc_publication import deadline_message, deadline_status, resolve_publications
from zucai.sfc_reports import read_reports
from zucai.sfc_sequence import known_official, period_windows, sequence, verification_at


def section(archive, feed, summary, now, link, *, pending_history=None):
    reports = resolve_publications(
        archive,
        read_reports(archive, include_unconfirmed=True),
        now=now,
        pending_history=pending_history,
    )
    settled, _ = read_settled(archive, summary)
    states = archived_sfc_issues(archive, feed, now)
    lines = ["## 14 场胜负彩", ""]
    collected = feed.get("collection", {}).get("sfc")
    if collected:
        lines += [
            f"采集状态：{_md(collected.get('status', '未记录'))}；"
            + "；".join(_md(e) for e in collected.get("errors", [])),
            "",
        ]
    if collected and collected.get("official_error"):
        lines += [f"官方公告采集待核：{_md(collected['official_error'])}；第三方来源：500网。", ""]
    if (
        collected
        and collected.get("status") == "partial"
        and collected.get("official_checked_at")
        and _timestamp(collected["official_checked_at"]) <= now
    ):
        # A new announcement can arrive even when 500's root or bridge fails.
        # Keep its sale window, but never pass a stale comparison off as today's.
        states = [
            (name, {**issue, "official_verification": _incomplete_check(issue, collected, now)})
            for name, issue in states
        ]
    by_number = {issue["issue_no"]: (name, issue) for name, issue in states}
    schedule = (collected or {}).get("official_schedule", {})
    windows = period_windows([issue for _, issue in states], schedule, now)
    if not windows:
        announced = [
            number
            for number, notice in schedule.items()
            if notice.get("observed_utc") and _timestamp(notice["observed_utc"]) <= now
        ]
        if announced:
            return lines + [
                "官方已公布期号 "
                + "、".join(sorted(announced))
                + "。开售与停售时间待核，尚无法确定当前展示期。",
                "",
            ]
        return lines + ["尚无已归档期次。", ""]
    state = sequence(windows.values(), now)
    chosen = state["current"] or state["last_closed"]
    if chosen is None:
        if state["announced"]:
            first = state["announced"][0]
            return lines + [
                f"官方已公布第 {first} 期，尚未开售；北京时间 "
                f"{_beijing(windows[first]['sale_start_utc'])} 开售。",
                "",
            ]
        # Legacy snapshots can be missing the third-party deadline.
        chosen = max(by_number, key=int)
    if chosen not in by_number:
        reused = chosen in (collected or {}).get("official_reused_schedule", ())
        notice_label = (
            "上次官方公告停售"
            if reused or (collected or {}).get("official_unavailable")
            else "官方公告停售"
        )
        return lines + [
            f"期号：{chosen}；{notice_label}：{_beijing(windows[chosen]['sale_end_utc'])}"
            "（北京时间）。第三方场次快照待采集，官方核对：待核。",
            "",
        ]
    name, issue = by_number[chosen]
    if state["current"] is None and state["expected"]:
        next_no = state["expected"][0]
        if next_no in state["announced"]:
            lines += [
                f"下一期 {next_no} 官方已公布，北京时间 "
                f"{_beijing(windows[next_no]['sale_start_utc'])} 开售。",
                "",
            ]
        else:
            lines += [
                f"上一期 {chosen} 已截止。按顺序下一期是 {next_no}，还没有抓到"
                "（来源尚未公布，或本班采集未成功）；抓到后在这里展示。",
                "",
            ]
    lines.extend(_issue_lines(name, issue, reports, settled, now, link))
    return lines


def _incomplete_check(issue, collected, now):
    check = dict(
        status="pending",
        reason="本班第三方采集未完成，使用已归档快照。",
        checked_at=collected["official_checked_at"],
        official={},
    )
    number = issue["issue_no"]
    notice = collected.get("official_schedule", {}).get(number, {})
    if collected.get("official_unavailable") or number in collected.get(
        "official_reused_schedule", ()
    ):
        check["last_known_official"] = notice or known_official(
            verification_at(issue.get("official_verification"), now)
        )
    else:
        check["official"] = notice
    return check


def not_yet_note(rows, end, now):
    """Say plainly when a period on sale simply has not reached its forecast day.

    Forecasts are frozen for fixtures of the current and the next Beijing day only, so a
    period whose fixtures are further away shows no probability yet. That is a matter of
    timing, not a fault; without this line the empty table reads like one.
    """
    if not end or _timestamp(end) <= now or not rows or any(r.get("probabilities") for r in rows):
        return None
    first = min(_timestamp(r["kickoff_utc"]) for r in rows).astimezone(BEIJING)
    earliest = first.date() - timedelta(days=1)
    if now.astimezone(BEIJING).date() >= earliest:
        return None
    return (
        f"本期还没到出预测的时候：最早一场在北京时间 {first:%m-%d %H:%M} 开球，"
        f"预测只对当天和次日的比赛生成，最早在 {earliest:%m-%d} 的日报里给出。"
        "表中「未映射」「无模型」是在等竞彩开售这些比赛，开售后自动对上。"
    )


def _fixture_list(rows):
    return sorted(
        (
            r["seq"],
            r.get("fixture_id"),
            r.get("league"),
            r["home"],
            r["away"],
            r.get("provider_home_id"),
            r.get("provider_away_id"),
            _timestamp(r.get("source_kickoff_utc") or r["kickoff_utc"]),
            bool(r.get("source_ended")),
        )
        for r in rows
    )


def _issue_lines(name, issue, reports, settled, now, link):
    number = issue["issue_no"]
    check = verification_at(issue.get("official_verification"), now)
    official = known_official(check)
    provider_end = issue["sale_end_utc"]
    end = official.get("sale_end_utc") or provider_end
    choices = [
        (path, dict(r))
        for path, r in reports
        if r["issue_no"] == number and _timestamp(r["generated_utc"]) <= now
    ]
    latest_frozen = max(
        choices, key=lambda item: (_timestamp(item[1]["generated_utc"]), item[0]), default=None
    )
    if check or (
        end and _timestamp(end) > now and any(r["league"] == "欧国联" for r in issue["snapshot"])
    ):
        current = _fixture_list(issue["snapshot"])
        choices = [
            (path, report) for path, report in choices if _fixture_list(report["rows"]) == current
        ]
    # A stale third-party lineup cannot certify a current set of official selections.
    lineup_conflict = any(
        d.get("status") == "conflict" and d.get("field") in {"home", "away", "seq"}
        for d in check.get("differences", [])
    )
    if lineup_conflict:
        choices = []
    timely = [
        (path, report)
        for path, report in choices
        if report["sale_state"] == "published_before_deadline"
        and deadline_status(end, report.get("published_utc")) == "published_before_deadline"
    ]
    path, report = (
        max(timely or choices, key=lambda item: (_timestamp(item[1]["generated_utc"]), item[0]))
        if choices
        else (None, None)
    )
    state = "截止未知" if not end else "已截止" if _timestamp(end) <= now else "未截止"
    deadline = _beijing(end) if end else "未知"
    eligible = bool(timely)
    publication = report or dict(
        sale_state="deadline_unconfirmed", generated_utc=None, published_utc=None, sale_end_utc=end
    )
    if report and report["sale_state"] == "published_before_deadline" and not eligible:
        publication = {
            **report,
            "sale_end_utc": end,
            "sale_state": deadline_status(end, report.get("published_utc")),
        }
    published = _beijing(publication["published_utc"]) if publication["published_utc"] else "未知"
    deadline_source = "官方公告停售" if official.get("sale_end_utc") else "第三方所报截止"
    if check.get("last_known_official"):
        deadline_source = "上次官方公告停售" if official.get("sale_end_utc") else deadline_source
    official_label = {"verified": "已核对", "pending": "待核", "conflict": "冲突"}
    checked = _beijing(check["checked_at"]) if check else None
    frozen_status = (report or {}).get("source", {}).get("official_verification", {}).get("status")
    differences = check.get("differences", [])
    if report:
        report_line = f"报告生成：{_beijing(report['generated_utc'])}（北京时间）。"
    elif latest_frozen:
        report_line = (
            "场次或主客对阵存在差异，当前展示采用第三方场次快照；历史冻结报告见"
            f"{link(latest_frozen[0] + '/sfc.json')}。"
        )
    else:
        report_line = "尚无当日冻结报告。"
    lines = [
        f"期号：{number}；{deadline_source}：{deadline}（北京时间，{state}）。",
        f"官方核对：{official_label.get(check.get('status'), '待核')}。"
        + _md(check.get("reason") or "本期归档只有第三方来源，尚无官方核对记录。"),
        *(
            [f"公告开售：{_beijing(official['sale_start_utc'])}（北京时间）。"]
            if official.get("sale_start_utc")
            else []
        ),
        *([f"本次核对：{checked}（北京时间）；第三方来源：500网。"] if checked else []),
        *(
            [
                f"公告来源：{_md(official['source_url'])}；"
                f"公告版本：{_md(official.get('published_at'))}。"
            ]
            if official.get("source_url")
            else []
        ),
        *(
            [f"第三方所报截止：{_beijing(provider_end)}（北京时间）。"]
            if official.get("sale_end_utc")
            else []
        ),
        *([f"核对差异 {len(differences)} 项，逐场记录见{link(name)}。"] if differences else []),
        f"来源：{link(path + '/sfc.json') if report else link(name)}；仅作诊断，BET 禁用。",
        report_line,
        *(
            [f"报告冻结时的官方核对：{official_label.get(frozen_status, '待核')}。"]
            if report and check
            else []
        ),
        deadline_message(publication) + "。",
        f"存好时间：{published}（北京时间）。",
        "",
        *(
            [
                "| 序号 | 对阵 | 北京开球 | 主胜 | 平 | 客胜 | 单选首项 | 复式项 | 状态 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            if eligible
            else ["| 序号 | 对阵 | 北京开球 | 状态 |", "| --- | --- | --- | --- |"]
        ),
    ]
    rows = disclosed_rows(report) if report else issue["snapshot"]
    for row in sorted(rows, key=lambda r: r["seq"]):
        p = row.get("probabilities") or {}
        selection = row.get("selection") or (list("hda") if not p else [])
        reason = row.get("reason") or ("模型诊断" if p else "尚无冻结概率")
        note = prior_label(row)
        if note:
            reason += f"；{note}"
        if note := pending_label(row):
            reason += f"；{note}"
        if not p:
            if row.get("match_id") is None:
                if not reason.startswith("未映射："):
                    reason = "未映射：" + reason
            elif not reason.startswith("无模型"):
                reason = "无模型：" + reason
        kickoff = _beijing(row["kickoff_utc"])
        if row.get("kickoff_basis") == "placeholder":
            kickoff += "（占位开球时间，待核实）"
        values = [
            row["seq"],
            f"{row['home']} VS {row['away']}",
            kickoff,
            *(
                [
                    *[_percent(p.get(k)) for k in "hda"],
                    OPTIONS.get(row.get("top_outcome"), "—"),
                    "+".join(OPTIONS[k] for k in selection) or "—",
                    reason,
                ]
                if eligible
                else [unconfirmed_mapping_status(row)]
            ),
        ]
        lines.append("| " + " | ".join(_md(value) for value in values) + " |")
    combo = report["combo"] if eligible else None
    lines += [""]
    forecast = [row for row in rows if row.get("probabilities")] if eligible else []
    if forecast:
        scope = "14 场" if len(forecast) == 14 else f"有预测的 {len(forecast)} 场"
        lines += [
            f"{scope}各取模型最高项：" + hit_line(forecast, report, "14场单选", "全中"),
            "",
        ]
    if note := not_yet_note(rows, end, now):
        lines += [note, ""]
    if eligible and (note := prior_footnote(rows)):
        lines.extend([_md(note), ""])
    if eligible and (note := own_fixture_note(rows)):
        lines.extend([_md(note), ""])
    if combo:
        lines += [
            f"复式 {combo['lines']} 注 / 上限 64；"
            f"覆盖概率 {_percent(combo['coverage_probability'])}"
            "（跨场独立近似；无模型场次保留空概率、按三选覆盖）。"
        ]
    elif eligible:
        lines += [
            f"复式未生成：至少 {report['minimum_lines']} 注，超过 64 注上限；覆盖概率未生成。"
        ]
    lines += ["", *pick_nine_lines(publication if report else None, prior_explained=eligible)]
    previous = [
        r for r in settled if r["issue_no"] < number and _timestamp(r["settled_utc"]) <= now
    ]
    if previous:
        last = max(previous, key=lambda r: r["issue_no"])
        combo_hit = (
            "全中"
            if last["combo_all_hit"] is True
            else "未全中"
            if last["combo_all_hit"] is False
            else "未生成"
        )
        lines += [
            f"上一已结算期 {last['issue_no']}：单选命中 {last['single_hits']} / 14"
            f"（有模型 {last['single_evaluated']} 场）"
            + (f"；复式诊断：{combo_hit}。" if eligible else "。")
        ]
        lines.append(prior_group_text(last.get("prior_groups")))
    else:
        lines += ["上一期命中数：尚无已结算记录。"]
    return lines + [
        "不估奖金、不设资金账户；3+1+0 全包＝无预测，无模型三选不计作单选命中。"
        if eligible
        else "不估奖金、不设资金账户。",
        "",
    ]
